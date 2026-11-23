from gui_support import ieee_model, ieee_root
"""Regresiones NET-FIX de apertura e historial persistido con API pública."""
from copy import deepcopy
from pathlib import Path
import json
import pytest
from power_machine.gui.document import Document
from power_machine.gui.network_session import NetworkSession
from power_machine.gui.netedit.controller import EditorController
from power_machine.gui.netedit.forms import creation_template
from power_machine.study import StudyCase
from gui_support import analytic_history
from gui_support import FakeContext


def setup_editor():
    _, history, ids = analytic_history()
    context = FakeContext(history.model)
    return EditorController(context), history.model, ids


def state(session):
    return {key: value for key, value in session.snapshot().items()
            if key in ('model', 'variant', 'layout', 'drafts', 'base_model')}


def test_completion_single_notification_exact_undo_and_invalid_atomicity():
    c, base, ids = setup_editor()
    data = creation_template('loads')
    data.update(bus=ids['b2'], p_mw=4, q_mvar=0)
    iid = c.create('loads', data, draft=True)
    c.move(iid, 13, 29)
    before = state(c.session)
    saved = c.session.to_data()
    events = []
    c.session.subscribe(events.append)
    with pytest.raises(ValueError):
        c.complete_draft(iid, dict(data, p_mw=None))
    assert c.session.to_data() == saved and not events
    c.complete_draft(iid, data)
    after = state(c.session)
    assert len(events) == 1
    assert iid not in after['drafts']
    assert c.session.get_element(iid)
    c.undo()
    assert state(c.session) == before
    c.redo()
    assert state(c.session) == after
    assert len(events) == 3


def test_cold_variant_open_restores_both_stacks_and_draft_steps(tmp_path):
    c, base, ids = setup_editor()
    initial = state(c.session)
    iid = c.create('loads', dict(creation_template('loads'), bus=ids['b2'], p_mw=3, q_mvar=0), draft=True)
    draft = state(c.session)
    c.complete_draft(iid, c.drafts[iid])
    completed = state(c.session)
    c.move(ids['b2'], 100, 200)
    moved = state(c.session)
    c.undo()
    payload = c.session.to_data()
    path = tmp_path/'session.json'
    c.save(path)
    restored = NetworkSession(base=base)
    restored.load_data(base, json.loads(path.read_text(encoding='utf-8-sig')))
    assert restored.to_data() == payload
    assert restored.snapshot()['can_undo'] and restored.snapshot()['can_redo']
    assert restored.redo() and state(restored) == moved
    assert restored.undo() and state(restored) == completed
    assert restored.undo() and state(restored) == draft
    assert restored.undo() and state(restored) == initial
    assert not restored.undo()
    assert restored.redo() and state(restored) == draft
    assert restored.redo() and state(restored) == completed
    revision = restored.snapshot()['revision']
    assert restored.set_draft('otra', {'name': 'Borrador'}, revision)['accepted']
    assert not restored.redo()


def test_replace_one_notification_invalid_history_and_revisions_are_atomic(tmp_path):
    c, base, ids = setup_editor()
    other = EditorController(FakeContext(base))
    other.edit(ids['l1'], {'p_mw': 70})
    c.edit(ids['l1'], {'p_mw': 60})
    before = state(c.session)
    events = []
    c.session.subscribe(events.append)
    payload = other.session.to_data()
    revision = c.session.snapshot()['revision']
    assert not c.session.replace_data(base, payload, revision+1)['accepted']
    bad = deepcopy(payload)
    first = next(iter(bad['transactions']['states'].values()))
    first['drafts']['alterado'] = {}
    assert not c.session.replace_data(base, bad, revision)['accepted']
    assert not events and state(c.session) == before
    path = tmp_path/'other.json'
    other.save(path)
    c.open(path)
    after = state(c.session)
    assert len(events) == 1 and after == state(other.session)
    assert events[0]['revision'] == revision+1
    c.undo()
    assert state(c.session) == before
    c.redo()
    assert state(c.session) == after


def test_load_replaces_only_network_domain_and_legacy_engine_history():
    c, base, ids = setup_editor()
    c.edit(ids['l1'], {'p_mw': 60})
    first = state(c.session)
    c.move(ids['b2'], 33, 42)
    second = state(c.session)
    payload = c.session.to_data()
    payload.pop('transactions')
    # Archivo legado producido por VariantHistory, con el cursor antes del final.
    payload['history']['cursor'] -= 1
    payload['variant'] = deepcopy(payload['history']['snapshots'][payload['history']['cursor']])
    restored = NetworkSession(base=base)
    restored.set_draft('descartar', {}, 0)
    restored.load_data(base, payload)
    assert state(restored) == first
    assert restored.redo() and state(restored) == second
    assert restored.undo() and state(restored) == first
    assert restored.undo()
    assert restored.model == base
    assert not restored.undo()


def test_document_reopen_preserves_history_drafts_and_clean_state(tmp_path):
    c, base, ids = setup_editor()
    document = Document(StudyCase(name='NET-FIX'))
    document.set_base(base)
    document.network.load_data(base, c.session.to_data())
    context = FakeContext(base)
    context.network_session = document.network
    c = EditorController(context)
    initial = state(c.session)
    data = dict(creation_template('loads'), bus=ids['b2'], p_mw=2, q_mvar=0)
    iid = c.create('loads', data, draft=True)
    drafted = state(c.session)
    c.complete_draft(iid, data)
    completed = state(c.session)
    c.move(ids['b2'], 88, 99)
    moved = state(c.session)
    c.undo()
    path = tmp_path/'case.power_machine'
    document.save(path)
    loaded = Document.open(path, StudyCase)
    assert not loaded.modified
    assert state(loaded.network) == completed
    assert loaded.network.redo() and state(loaded.network) == moved
    assert loaded.modified
    assert loaded.network.undo() and not loaded.modified
    assert loaded.network.undo() and state(loaded.network) == drafted
    assert loaded.network.undo() and state(loaded.network) == initial
    assert not loaded.network.undo()
    assert base == initial['model']


def test_document_undo_to_base_materializes_base_after_reopen(tmp_path):
    c, base, ids = setup_editor()
    document = Document(StudyCase(name='Base'))
    document.set_base(base)
    document.network.load_data(base, c.session.to_data())
    context = FakeContext(base)
    context.network_session = document.network
    c = EditorController(context)
    c.edit(ids['l1'], {'p_mw': 60})
    path = tmp_path/'base.power_machine'
    document.save(path)
    loaded = Document.open(path, StudyCase)
    assert loaded.network.undo()
    model, study, _ = loaded.materialize_case(loaded.active_case_id)
    assert model == base == loaded.network.model
    assert study.scenario == {}


def test_document_stage_session_reopens_with_history(tmp_path):
    from dataclasses import asdict
    from power_machine.netedit import edit_command
    _, base, ids = setup_editor()
    document = Document(StudyCase(name='Etapa'))
    document.set_base(base)
    variant = document.create_variant('Alternativa', document.revision)
    command = asdict(edit_command(base, 'update', 'loads', ids['l1'], {'p_mw': 60}))
    stage = document.add_stage(variant, 'Primera', '2026-11-14T00:00:00+00:00', [command], document.revision)
    document.configure_case(document.active_case_id, document.revision, variant_id=variant, stage_id=stage)
    context = FakeContext(document.network.model)
    context.network_session = document.network
    c = EditorController(context)
    before = state(c.session)
    c.move(ids['b2'], 55, 44)
    after = state(c.session)
    path = tmp_path/'stage.power_machine'
    document.save(path)
    loaded = Document.open(path, StudyCase)
    assert not loaded.modified
    assert state(loaded.network) == after
    assert loaded.network.undo() and state(loaded.network) == before
    assert loaded.network.redo() and state(loaded.network) == after


def test_replacement_restores_base_and_preserves_document_history():
    from power_machine.gui.history import SnapshotCommand
    c, base, ids = setup_editor()
    marker = []
    c.session.history.record('document', SnapshotCommand(marker.append, 'antes', 'después'))
    alternate = deepcopy(base)
    alternate['meta']['source_package'] = 'Otra base de ensayo'
    other = NetworkSession(base=alternate)
    before = state(c.session)
    assert c.session.replace_data(alternate, other.to_data(), c.session.revision)['accepted']
    assert c.base == alternate
    c.undo()
    assert state(c.session) == before
    c.redo()
    assert c.base == alternate
    assert c.session.history.step('document')
    assert marker == ['antes']
