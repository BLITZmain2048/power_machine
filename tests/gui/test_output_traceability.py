from gui_support import ieee_model, ieee_root
import pytest

pytestmark = pytest.mark.gui

"""Pruebas sin pantalla de la salida estructurada study."""
from power_machine.gui.output import (
    OUTPUT_SCHEMA,
    entry_matches,
    make_output_entry,
    normalize_element_ref,
    output_group_key,
)


def test_output_entry_preserves_traceability_and_utc_fields():
    entry = make_output_entry(
        'warning', 'Equipo sin suministro', module='loadflow', code='island.deenergized',
        case_id='base', run_id='lf-001',
        element_refs=[{'object_id': 'ref_1', 'relative_path': 'Red/Barra', 'origin': 'base', 'terminal': 'bus1'}],
        details={'coverage': 'partial'}, timestamp_utc='2026-11-14T12:00:00+00:00', entry_id='entry-1')
    assert entry['schema'] == OUTPUT_SCHEMA
    assert entry['level'] == 'AVISO'
    assert entry['timestamp_utc'].endswith('+00:00')
    assert entry['element_refs'][0] == {
        'object_id': 'ref_1', 'relative_path': 'Red/Barra', 'origin': 'base', 'terminal': 'bus1'}
    assert entry['details']['coverage'] == 'partial'


def test_output_grouping_is_limited_to_run_code_target_and_same_message():
    base = make_output_entry('ERROR', 'Falla A', code='cc.unsupported', run_id='cc-1',
                             element_refs=[{'object_id': 'ref_a'}], entry_id='a')
    equal = make_output_entry('ERROR', 'Falla A', code='cc.unsupported', run_id='cc-1',
                              element_refs=[{'object_id': 'ref_a'}], entry_id='b')
    other_target = make_output_entry('ERROR', 'Falla A', code='cc.unsupported', run_id='cc-1',
                                     element_refs=[{'object_id': 'ref_b'}], entry_id='c')
    other_message = make_output_entry('ERROR', 'Falla B', code='cc.unsupported', run_id='cc-1',
                                      element_refs=[{'object_id': 'ref_a'}], entry_id='d')
    assert output_group_key(base) == output_group_key(equal)
    assert output_group_key(base) != output_group_key(other_target)
    assert output_group_key(base) != output_group_key(other_message)


def test_output_filters_and_reference_validation_are_explicit():
    entry = make_output_entry('info', 'Resultado disponible', module='cortocircuito', code='done',
                              run_id='cc-5', element_refs=[{'id': 'ref_123'}])
    assert entry_matches(entry, level='INFO', module='corto', text='ref_123')
    assert not entry_matches(entry, level='ERROR')
    assert normalize_element_ref({'id': 'ref_2'})['object_id'] == 'ref_2'


