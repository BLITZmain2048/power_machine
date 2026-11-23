"""Contratos de la fase 2 para el administrador de datos."""
import json
from pathlib import Path

import pytest

from power_machine.gui.datamanager import (
    DATA_COLUMNS, GROUPS, build_operations, combined_filter, data_rows, parse_cell_value,
)
from power_machine.gui.i18n import language, set_language, tr


def test_data_catalogue_has_all_owned_keys_in_both_languages():
    root = Path(__file__).parents[2] / 'power_machine' / 'gui' / 'locale'
    english = json.loads((root / 'en.json').read_text(encoding='utf8'))
    spanish = json.loads((root / 'es.json').read_text(encoding='utf8'))
    used = {'data.title', 'data.new_case', 'data.clear_filters', 'data.columns'}
    used.update(title for _, title, _ in GROUPS)
    used.update(label for columns in DATA_COLUMNS.values() for _, label in columns)
    assert all(key in english and key in spanish for key in used)


def test_state_filter_stays_canonical_when_display_language_changes():
    rows = [
        {'object_id': 'A', 'outserv': False, 'closed': True},
        {'object_id': 'B', 'outserv': True, 'closed': False},
    ]
    assert combined_filter(rows, state='in_service') == rows[:1]
    assert combined_filter(rows, state='closed') == rows[:1]


def test_edit_parser_rejects_units_not_declared_and_never_rounds_values():
    assert parse_cell_value('load_scale', '1.01') == pytest.approx(1.01)
    assert parse_cell_value('gen_p_mw', '12.5 MW') == pytest.approx(12.5)
    with pytest.raises(ValueError):
        parse_cell_value('gen_p_mw', '12.5 kV')
    with pytest.raises(ValueError):
        parse_cell_value('load_scale', '-0.01')


def test_batch_factor_uses_source_value_without_inventing_a_default():
    row = {'object_id': 'L1', 'name': 'Load 1', 'class': 'ElmLod', 'load_scale': 1.2}
    edit = build_operations([row], 'load_scale', '1.05', factor=True)[0]
    assert edit['value'] == pytest.approx(1.26)


def test_rows_keep_missing_substations_empty_instead_of_using_display_fallback():
    rows = data_rows({'buses': [{'id': 'B1', 'name': 'B1', 'path': 'B1.ElmTerm'}]}, 'buses')
    assert rows[0]['substation'] == ''
    assert rows[0]['has_substation'] is False


def test_data_messages_retranslate_from_catalogue():
    previous = language()
    try:
        set_language('en')
        assert tr('data.title') == 'DATA MANAGER'
        set_language('es')
        assert tr('data.title') == 'ADMINISTRADOR DE DATOS'
    finally:
        set_language(previous)
