"""Ingestión y revisiones con exportaciones de solo lectura."""
from copy import deepcopy
import csv
import json
from pathlib import Path
import pytest
pytestmark = pytest.mark.gui
from power_machine.protection import operating_time
from power_machine.protection.io_exports import read_protection_exports, compile_stages, template_points, identity, file_sha256, TIME_COLUMNS
from power_machine.protection.settings import new_workspace, save_settings, load_settings, revise_settings, effective_stages
from power_machine.protection.schema import ProtectionError
from power_machine.gui.panels.protection import SOURCE_ROOT
from gui_support import stage

def export_fixture(tmp_path, *, primary=False):
    root = tmp_path / 'exports'
    (root / 'numeric').mkdir(parents=True)
    project = '\\user.IntUser\\Fixture.IntPrj'
    paths = [project + '\\Network Model.IntPrjfolder\\R1.ElmRelay\\' + name + '.RelToc' for name in ('s1', 's2')]
    paths.extend((project + '\\Network Model.IntPrjfolder\\R2.ElmRelay\\' + name + '.RelToc' for name in ('s1', 's2')))
    char = project + '\\Library.IntPrjfolder\\char.TypChatoc'
    ct = project + '\\Network Model.IntPrjfolder\\ct.StaCt'
    manifest = {'project': {'path': project}, 'status': 'completed'}
    (root / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
    objects = [{'path': p, 'object_id': 'explicit_' + str(i), 'class': 'RelToc', 'name': 'Igual', 'attributes': {'Tpset': {'value': None, 'unit': '1'}}} for (i, p) in enumerate(paths)]
    objects.extend([{'path': char, 'class': 'TypChatoc', 'name': 'curva', 'attributes': {}}, {'path': ct, 'class': 'StaCt', 'name': 'TC', 'attributes': {}}])
    with (root / 'objects.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=('path', 'object_id', 'class', 'name', 'calc_relevant', 'outserv', 'type_path'), extrasaction='ignore')
        writer.writeheader()
        writer.writerows(objects)
    (root / 'inventory.json').write_text(json.dumps({'objects': objects}), encoding='utf-8')
    curves = [{'element_path': p, 'curve_id': 'c' + str(i), 'characteristic_path': char if i != 2 else 'missing_char', 'pickup_primary_A' if primary else 'pickup_secondary_A': 100 if primary else 1, 'measurement_type': 'Phase Current (3ph)', 'block_outserv': 0, 'ancestor_outserv': False, 'status': 'derived', 'time_dial': None} for (i, p) in enumerate(paths)]
    (root / 'numeric/numeric_report.json').write_text(json.dumps({'curves': curves}), encoding='utf-8')
    current_key = 'current_primary_A' if primary else 'current_secondary_A'
    with (root / 'numeric/curve_points_secondary.csv').open('w', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=('curve_id', 'element_path', current_key, *TIME_COLUMNS, 'time_graph_floor_s', 'provenance'))
        writer.writeheader()
        for (index, path) in enumerate(paths):
            for (current, time) in ((1.0, 10), (10.0, 1)):
                writer.writerow({'curve_id': 'c' + str(index), 'element_path': path, current_key: current * 100 if primary else current, 'time_equation_s': 'NULL' if index == 3 else time, 'time_setting_limited_s': time, 'time_with_pickup_s': time + 0.02, 'time_graph_floor_s': 100, 'provenance': 'derived_from_exported_settings_not_native_vertices'})
    mapping = {path: {'protected_element_id': 'L1', 'terminal_id': 'B1', 'measurement': 'phase', 'ct_primary_rated_a': 1000, 'ct_secondary_rated_a': 5, 'ct_source_full_path': ct, 'provenance': 'fixture_mapping'} for path in paths}
    return (root, mapping)

def test_case_6_inventory_identity_nulls_and_references(tmp_path):
    (root, mapping) = export_fixture(tmp_path)
    inventory = read_protection_exports(root)
    assert inventory['coverage']['objects'] == 6
    assert inventory['coverage']['stages'] == 4
    assert inventory['coverage']['points'] == 8
    assert [s['object_id'] for s in inventory['stages']] == ['explicit_' + str(i) for i in range(4)]
    assert inventory['stages'][0]['relay_id'] == inventory['stages'][1]['relay_id']
    assert inventory['stages'][0]['relay_id'] != inventory['stages'][2]['relay_id']
    assert inventory['objects'][0]['attributes']['Tpset']['value'] is None
    assert inventory['stages'][3]['points'][0]['time_equation_s'] is None
    (stages, coverage, diagnostics) = compile_stages(inventory, mapping, time_semantics='time_equation_s')
    assert len(stages) == 2
    assert {d['code'] for d in diagnostics} == {'broken_characteristic_reference', 'missing_or_nonfinite'}
    assert all((c['object_id'] and c['relative_path'] for c in coverage))
    broken = deepcopy(mapping)
    broken[next(iter(mapping))]['ct_source_full_path'] = 'missing_ct'
    assert compile_stages(inventory, broken, time_semantics='time_with_pickup_s')[2][0]['code'] == 'broken_ct_reference'

@pytest.mark.parametrize('primary', [False, True])
def test_case_6_explicit_primary_secondary_semantics(tmp_path, primary):
    (root, mapping) = export_fixture(tmp_path, primary=primary)
    snapshot = read_protection_exports(root)
    (stages, _, _) = compile_stages(snapshot, mapping, time_semantics='time_with_pickup_s')
    assert len(stages) == 3
    s = stages[0]
    assert s.pickup_primary_a == (100 if primary else 200)
    assert s.ct_ratio == (None if primary else 200)
    assert s.time_dial is None
    assert operating_time(s, 1000 if primary else 2000) == pytest.approx(1.02)
    assert s.curve.source['time_semantics'] == 'time_with_pickup_s'
    with pytest.raises(ProtectionError):
        compile_stages(snapshot, mapping, time_semantics='time_graph_floor_s')

def test_stable_identity_library_null_and_no_latest(tmp_path):
    project = '\\user.IntUser\\Project.IntPrj'
    p = project + '\\Library.IntPrjfolder\\s.RelToc'
    ident = identity(project, p, 'library_template')
    assert ident == identity(project, p, 'library_template')
    assert ident['object_id'].startswith('prot_') and ident['source_object_id'] is None
    assert ident['relative_path'] is None
    assert identity(project, '\\Lib.IntLibrary\\p')['relative_path'] is None
    assert identity(project, project + '\\Network Model.IntPrjfolder\\p')['relative_path'] == 'Network Model.IntPrjfolder\\p'
    with pytest.raises(ProtectionError, match='Seleccione'):
        read_protection_exports(tmp_path)

def test_case_7_roundtrip_revisions_and_immutable_source(tmp_path):
    (root, mapping) = export_fixture(tmp_path)
    source_hashes = {p: file_sha256(p) for p in root.rglob('*') if p.is_file()}
    s = stage()
    ws = new_workspace((s,), source_manifest_sha256='fixture')
    changed = revise_settings(ws, 'P', {'time_dial': 0.234567891, 'enabled': False}, units={'time_dial': '1', 'enabled': 'bool'}, provenance={'origin': 'fixture'})
    save_settings(ws, tmp_path / 'revision0.json')
    save_settings(changed, tmp_path / 'revision1.json')
    assert load_settings(tmp_path / 'revision1.json') == changed
    assert effective_stages(changed)[0].time_dial == 0.234567891
    assert not effective_stages(changed)[0].enabled
    assert ws['stages']['P']['time_dial'] == 0.1 and ws['revision'] == 0
    assert changed['stages']['P'] == ws['stages']['P']
    assert changed['revision'] == 1
    assert operating_time(effective_stages(changed)[0], 200) is None
    assert {p: file_sha256(p) for p in source_hashes} == source_hashes
    with pytest.raises(FileExistsError):
        save_settings(changed, tmp_path / 'revision1.json')
    with pytest.raises(ProtectionError):
        save_settings(changed, Path(tmp_path.anchor) / 'escape.json')

def test_sampled_adjustment_requires_new_points(tmp_path):
    (root, mapping) = export_fixture(tmp_path)
    (stages, _, _) = compile_stages(read_protection_exports(root), mapping, time_semantics='time_with_pickup_s')
    ws = new_workspace(stages)
    with pytest.raises(ProtectionError, match='nuevos puntos'):
        revise_settings(ws, stages[0].object_id, {'pickup_primary_a': 250}, units={'pickup_primary_a': 'A'}, provenance='fixture')
    changed = revise_settings(ws, stages[0].object_id, {'enabled': False}, units={'enabled': 'bool'}, provenance='fixture')
    assert not effective_stages(changed)[0].enabled

def test_missing_service_state_is_not_disabled_zero(tmp_path):
    (root, mapping) = export_fixture(tmp_path)
    inventory = read_protection_exports(root)
    inventory['stages'][0]['summary']['block_outserv'] = None
    (stages, coverage, diagnostics) = compile_stages(inventory, mapping, time_semantics='time_with_pickup_s')
    assert inventory['stages'][0]['object_id'] not in {s.object_id for s in stages}
    assert coverage[0]['missing'] == ['missing_service_state']
