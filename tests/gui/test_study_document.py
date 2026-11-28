from gui_support import ieee_model, ieee_root
"""Aceptación dirigida de documentos, migración y transacciones study."""
from copy import deepcopy
from dataclasses import asdict
import json
from uuid import uuid4

import pytest

from power_machine.gui.document import Document
from power_machine.gui.history import NetworkSession
from power_machine.gui import study_document as v2
from power_machine.gui.workspace_links import file_sha256, calculation_sha256, relative_path
from power_machine.gui.type_library import copy_type, application_commands
from power_machine.study import StudyCase
from power_machine.netedit import edit_command
from power_machine.netedit.schema import value_sha256
from power_machine.loadflow import run_loadflow
from gui_support import analytic_history


@pytest.fixture
def sample():
    _, h, ids = analytic_history()
    doc = Document(StudyCase(name='Estudio'))
    doc.set_base(h.model)
    return doc, h.model, ids


def edit(identity, value, unit='1', field='load_scale', **kwargs):
    return dict(object_id=identity, field=field, value=value, unit=unit, **kwargs)


def test_operations_atomic_units_expected_and_empty(sample):
    doc, base, ids = sample
    original = deepcopy(doc.to_data())
    for invalid in ('', None, 'abc', float('nan'), True):
        result = doc.apply_operations([edit(ids['l1'], 1.2), edit(ids['l1'], invalid)], doc.revision)
        assert not result['accepted'] and doc.to_data() == original
    assert not doc.apply_operations([edit(ids['l1'], 1, unit='MW')], doc.revision)['accepted']
    assert not doc.apply_operations([edit(ids['l1'], 1, expected_value=99)], doc.revision)['accepted']
    result = doc.apply_operations([edit(ids['l1'], 1.1)], doc.revision)
    assert result['accepted']
    model, study, deps = doc.materialize_case(doc.active_case_id)
    assert study.scenario == {}
    assert next(x for x in model['loads'] if x['id'] == ids['l1'])['scale'] == 1.1
    assert run_loadflow(model, study.scenario)['converged']
    assert base != model
    assert doc.undo()
    assert doc.materialize_case(doc.active_case_id)[0] == base
    assert doc.redo()
    assert not doc.apply_operations([edit(ids['l1'], 1.2)], 0)['accepted']


def test_v2_cases_stages_and_roundtrip(sample, tmp_path):
    doc, base, ids = sample
    alternative = doc.create_variant('Expansión A', doc.revision)
    first_command = asdict(edit_command(base, 'update', 'loads', ids['l1'], {'p_mw': 60.0}))
    first = doc.add_stage(alternative, '2027', '2027-01-01T00:00:00-03:00', [first_command], doc.revision)
    doc.configure_case(doc.active_case_id, doc.revision, variant_id=alternative, stage_id=first)
    first_model = doc.materialize_case(doc.active_case_id)[0]
    second = doc.add_stage(alternative, '2028', '2028-01-01T00:00:00-03:00',
        [asdict(edit_command(first_model, 'update', 'loads', ids['l1'], {'p_mw': 70.0}))], doc.revision, first)
    assert doc.materialize_case(doc.active_case_id)[0] == first_model
    duplicate = doc.duplicate_case(doc.active_case_id, 'Alternativa', doc.revision)
    scenario = doc.create_scenario('Demanda', {'load_scale': {ids['l1']: 1.2}}, doc.revision)
    doc.configure_case(duplicate, doc.revision, scenario_id=scenario, stage_id=second)
    doc.activate_case(duplicate, doc.revision)
    assert doc.study.scenario == {'load_scale': {ids['l1']: 1.2}}
    model, study, hashes = doc.materialize_case(duplicate)
    load = next(x for x in model['loads'] if x['id'] == ids['l1'])
    assert load['p_mw'] == 70.0 and load['scale'] == 1.2 and not study.scenario
    wid = doc.put_workspace('dsl', {'source': 'bloque'}, {'proposed': True}, doc.revision)
    doc.configure_case(duplicate, doc.revision, workspace_ids=[wid])
    doc.data['extensions']['vendor:unknown'] = {'preserve': [1, None]}
    doc.data['presentation']['formats'] = {'precision': 4}
    path = tmp_path / 'study.power_machine'
    doc.save(path)
    loaded = Document.open(path, StudyCase)
    assert loaded.to_data() == doc.to_data() and not loaded.modified
    assert loaded.materialize_case(duplicate) == doc.materialize_case(duplicate)
    loaded.study.name = 'Nueva revisión'
    loaded.save()
    assert list(tmp_path.glob('*.bak'))
    with pytest.raises(ValueError, match='escritor'):
        doc.save()


def test_migrate_v1_network_preserves_original(sample, tmp_path):
    doc, base, ids = sample
    doc.network = NetworkSession(doc.history, base)
    command = asdict(edit_command(base, 'update', 'loads', ids['l1'], {'p_mw': 65.0}))
    assert doc.network.commit([command], 0, 'Carga')['accepted']
    source = tmp_path / 'v1.power_machine'
    StudyCase(name='Legado').save(source)
    lateral = tmp_path / 'v1.power_machine.network.json'
    lateral.write_text(json.dumps(dict(schema='power_machine-gui-study-network-v1',
        study_sha256=value_sha256(StudyCase(name='Legado').to_json()), network=doc.network.to_data())), encoding='utf-8')
    before = source.read_bytes(), lateral.read_bytes()
    loaded = Document.open(source, StudyCase)
    loaded.restore_network(base)
    with pytest.raises(ValueError, match='otro archivo'):
        loaded.save()
    target = tmp_path / 'v2.power_machine'
    loaded.save(target)
    reopened = Document.open(target, StudyCase)
    assert reopened.materialize_case(reopened.active_case_id)[0]['loads'][0]['p_mw'] == 65.0
    assert before == (source.read_bytes(), lateral.read_bytes())
    payload = json.loads(lateral.read_text())
    payload['study_sha256'] = 'bad'
    lateral.write_text(json.dumps(payload), encoding='utf-8')
    with pytest.raises(ValueError, match='lateral'):
        Document.open(source, StudyCase)


@pytest.mark.parametrize('phase', ['serialize', 'write', 'flush', 'fsync', 'backup', 'replace', 'after_replace'])
def test_write_failure_keeps_complete_revision(sample, tmp_path, monkeypatch, phase):
    doc, _, ids = sample
    path = tmp_path / 'safe.power_machine'
    doc.save(path)
    before = path.read_bytes()
    doc.operation(dict(id=ids['l1'], **{'class': 'ElmLod'}, scale=1), {'load_scale': 1.1})
    def fail(*args, **kwargs):
        raise OSError('Fallo simulado ' + phase)
    if phase == 'serialize':
        monkeypatch.setattr(v2.json, 'dumps', fail)
    elif phase in ('fsync', 'backup'):
        original = v2.os.fsync
        calls = []
        def fsync(fd):
            calls.append(fd)
            if phase == 'fsync' or len(calls) == 2:
                fail()
            original(fd)
        monkeypatch.setattr(v2.os, 'fsync', fsync)
    elif phase in ('write', 'flush'):
        from pathlib import Path
        original = Path.open
        class Broken:
            def __init__(self, stream):
                self.stream = stream
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.stream.close()
            def write(self, data):
                if phase == 'write':
                    self.stream.write(data[:20])
                    fail()
                return self.stream.write(data)
            def flush(self):
                fail()
        def open_file(p, mode='r', *args, **kwargs):
            stream = original(p, mode, *args, **kwargs)
            return Broken(stream) if mode == 'xb' and p.suffix == '.tmp' else stream
        monkeypatch.setattr(Path, 'open', open_file)
    else:
        original = v2.os.replace
        def replace(a, b):
            if phase == 'after_replace':
                original(a, b)
            fail()
        monkeypatch.setattr(v2.os, 'replace', replace)
    with pytest.raises(OSError):
        doc.save()
    monkeypatch.undo()
    from power_machine.gui import workspace_links
    monkeypatch.setattr(workspace_links, "ROOT", tmp_path)
    loaded = Document.open(path, StudyCase)
    assert loaded.data['schema'] == v2.SCHEMA
    if phase != 'after_replace':
        assert path.read_bytes() == before
    else:
        assert loaded.study.scenario['load_scale'][ids['l1']] == 1.1


def test_network_public_transactions_drafts_and_geometry(sample):
    _, base, ids = sample
    session = NetworkSession(base=base)
    events = []
    token = session.subscribe(events.append)
    command = asdict(edit_command(base, 'move', 'buses', ids['b2'],
        dict(x_canvas=10, y_canvas=20, diagram_group='Subestación')))
    before = session.snapshot()
    assert session.preview([command], 0)['accepted'] and session.snapshot() == before
    assert session.commit([command], 0, 'Mover')['accepted']
    assert not events[-1]['electrical_changed'] and events[-1]['presentation_changed']
    assert not session.commit([command], 0, 'Obsoleto')['accepted']
    assert session.set_draft('borrador', {'incomplete': True}, session.revision)['accepted']
    assert session.snapshot()['model'] == base
    assert session.undo(session.revision)['accepted']
    assert session.redo(session.revision)['accepted']
    saved = session.to_data()
    restored = NetworkSession(base=base)
    restored.load_data(base, saved)
    assert restored.to_data() == saved
    assert session.get_element(ids['l1'])['class'] == 'ElmLod'
    assert session.get_ports(ids['l1'])[0]['bus_id'] == ids['b2']
    session.unsubscribe(token)


def test_result_content_workspace_and_history(sample, tmp_path):
    doc, _, ids = sample
    wid = doc.put_workspace('protection', {'pickup_a': 100}, {}, doc.revision)
    doc.configure_case(doc.active_case_id, doc.revision, workspace_ids=[wid])
    result = tmp_path / 'result.json'
    result.write_text('{}', encoding='utf-8')
    dep = doc.materialize_case(doc.active_case_id)[2]
    dep.update(engine_id='analytical', engine_version_sha256='test', input_files=[])
    rid = str(uuid4())
    link = dict(run_id=rid, case_id=doc.active_case_id, module='lf', panel_id='lf', created_at_utc=v2.now(),
        result_path=relative_path(result), result_file_sha256=file_sha256(result), schema='result-v1',
        status='partial', raw_status='partial', cancelled=False, dependencies=dep,
        calculation_sha256=calculation_sha256(dep), validation={'ref_comparison': {'status': 'not_compared'}}, coverage={})
    doc.attach_result(link)
    assert doc.result_freshness(rid) == 'current'
    doc.put_workspace('protection', {'pickup_a': 120}, {}, doc.revision, wid)
    assert doc.result_freshness(rid) == 'stale'
    doc.undo()
    assert doc.result_freshness(rid) == 'current'
    result.write_text('{"changed":true}', encoding='utf-8')
    assert doc.result_freshness(rid) == 'stale'


def test_paths_nonfinite_and_tampered_base(sample, tmp_path):
    doc, base, _ = sample
    with pytest.raises(ValueError):
        doc.save('../outside.power_machine')
    path = tmp_path / 'model.json'
    path.write_text(json.dumps(base), encoding='utf-8')
    doc.set_base(base, path)
    target = tmp_path / 'study.power_machine'
    doc.save(target)
    path.write_text('{}', encoding='utf-8')
    with pytest.raises(ValueError, match='alterado'):
        Document.open(target, StudyCase)
    target.write_text('{"schema":"power_machine-study-v2","revision":NaN}', encoding='utf-8')
    with pytest.raises(ValueError, match='no finito'):
        Document.open(target, StudyCase)


def test_private_type_explicit_commands(sample):
    _, base, ids = sample
    source = dict(type_id='ref_type', type_class='TypLod', instance_class='ElmLod', params={'x': 1}, units={'x': '1'})
    own = copy_type(source, 'Propio')
    own_before = deepcopy(own)
    commands = application_commands(base, own, [ids['l1']])
    assert len(commands) == 1 and source['type_id'] == 'ref_type' and own == own_before
    with pytest.raises(ValueError, match='incompatible'):
        application_commands(base, own, [ids['b1']])


def test_two_variants_do_not_merge_and_retired_target_rejects(sample):
    doc, base, ids = sample
    first = doc.create_variant('A', doc.revision)
    stage_a = doc.add_stage(first, 'A1', '2027-01-01T00:00:00+00:00',
        [asdict(edit_command(base, 'update', 'loads', ids['l1'], {'p_mw': 60.0}))], doc.revision)
    second = doc.create_variant('B', doc.revision)
    stage_b = doc.add_stage(second, 'B1', '2027-01-01T00:00:00+00:00',
        [asdict(edit_command(base, 'retire', 'loads', ids['l1']))], doc.revision)
    doc.configure_case(doc.active_case_id, doc.revision, variant_id=first, stage_id=stage_a)
    before = doc.to_data()
    with pytest.raises(ValueError):
        doc.configure_case(doc.active_case_id, doc.revision, variant_id=second, stage_id=stage_a)
    assert doc.to_data() == before
    doc.configure_case(doc.active_case_id, doc.revision, variant_id=second, stage_id=stage_b)
    assert not doc.apply_operations([edit(ids['l1'], 1.2)], doc.revision)['accepted']
    assert doc.undo()
    assert doc.to_data()['cases'][0]['variant_id'] == first


def test_unknown_schema_missing_result_and_v1_without_sidecar(sample, tmp_path):
    doc, base, ids = sample
    source = tmp_path / 'plain-v1.power_machine'
    case = StudyCase(scenario={'load_scale': {ids['l1']: 1.15}})
    case.save(source)
    loaded = Document.open(source, StudyCase)
    loaded.restore_network(base)
    original = source.read_bytes()
    target = tmp_path / 'migrated.power_machine'
    loaded.save(target)
    assert Document.open(target, StudyCase).study.scenario == case.scenario
    assert source.read_bytes() == original
    target.write_text('{"schema":"future-study-v99"}', encoding='utf-8')
    with pytest.raises(ValueError, match='desconocido'):
        Document.open(target, StudyCase)


def test_lock_and_previous_revision_are_preserved(sample, tmp_path):
    doc, _, _ = sample
    target = tmp_path / 'case.power_machine'
    doc.save(target)
    previous = target.read_bytes()
    lock = tmp_path / 'case.power_machine.lock'
    lock.write_text('otro escritor', encoding='utf-8')
    with pytest.raises(ValueError, match='escritor'):
        doc.save()
    assert target.read_bytes() == previous and lock.read_text() == 'otro escritor'
