from gui_support import ieee_model, ieee_root
"""Pruebas focalizadas del administrador de datos study."""
from copy import deepcopy
from pathlib import Path
from time import perf_counter

import pytest

from power_machine.gui.datamanager import (build_operations, combined_filter, data_rows,
                                  paste_operations, sort_rows)
from power_machine.gui.i18n import tr
from power_machine.gui.document import Document
from power_machine.study import load_model, new_study


from gui_support import ROOT


def test_combined_filter_and_order_keep_missing_values_separate():
    rows = [
        {'object_id': 'a', 'name': 'Carga Norte', 'substation': 'Norte', 'un_kv': 220, 'outserv': False, 'load_scale': 1.1},
        {'object_id': 'b', 'name': 'Carga Sur', 'substation': 'Sur', 'un_kv': 110, 'outserv': True, 'load_scale': None},
    ]
    assert combined_filter(rows, text='norte', substation_name='Norte', voltage_kv='220', state='En servicio') == rows[:1]
    assert combined_filter(rows, state='Fuera de servicio') == rows[1:]
    assert [row['object_id'] for row in sort_rows(rows, 'load_scale')] == ['a', 'b']


def test_paste_requires_complete_selection_and_preserves_preconditions():
    rows = [
        {'object_id': 'a', 'name': 'A', 'class': 'ElmLod', 'load_scale': 1.0},
        {'object_id': 'b', 'name': 'B', 'class': 'ElmLod', 'load_scale': 1.1},
    ]
    edits = paste_operations('a\t1.2\nb\t1.3', rows, 'load_scale')
    assert [(edit['object_id'], edit['value'], edit['expected_value']) for edit in edits] == [('a', 1.2, 1.0), ('b', 1.3, 1.1)]
    with pytest.raises(ValueError, match=tr('data.error.empty_cell')):
        paste_operations('a\t1.2\nb\t', rows, 'load_scale')
    with pytest.raises(ValueError, match=tr('data.error.paste_row_count')):
        paste_operations('1.2', rows, 'load_scale')


def test_real_power_machine_five_load_batch_is_reversible_and_base_unchanged():
    """Ejercita Document.apply_operations con cinco cargas del modelo power_machine real."""
    base = ieee_model()
    base_before = deepcopy(base)
    document = Document(new_study(ieee_root()))
    document.set_base(base)
    rows = [row for row in data_rows(base, 'loads') if row.get('class') == 'ElmLod' and not row.get('outserv')][:5]
    assert len(rows) == 5
    edits = build_operations(rows, 'load_scale', 1.01)
    result = document.apply_operations(edits, document.revision, 'study cinco cargas')
    assert result['accepted'] and result['changed_ids'] == sorted(row['object_id'] for row in rows)
    materialized, _study, _dependencies = document.materialize_case(document.active_case_id)
    scales = {item['id']: item['scale'] for item in materialized['loads']}
    assert all(scales[row['object_id']] == 1.01 for row in rows)
    assert base == base_before
    assert document.undo()
    restored, _study, _dependencies = document.materialize_case(document.active_case_id)
    assert restored == base_before


def test_invalid_batch_never_publishes_partial_change():
    base = ieee_model()
    document = Document(new_study(ieee_root()))
    document.set_base(base)
    rows = [row for row in data_rows(base, 'loads') if row.get('class') == 'ElmLod'][:2]
    valid = build_operations(rows[:1], 'load_scale', 1.02)
    invalid = dict(valid[0], object_id=rows[1]['object_id'], value='')
    before = deepcopy(document.to_data())
    result = document.apply_operations(valid + [invalid], document.revision, 'Debe rechazar')
    assert not result['accepted']
    assert document.to_data() == before


def test_filter_and_batch_preparation_handle_500_rows_quickly():
    rows = [
        {'object_id': f'load-{number}', 'name': f'Carga {number}', 'class': 'ElmLod',
         'substation': 'Norte' if number % 2 else 'Sur', 'un_kv': 220,
         'outserv': False, 'load_scale': 1.0}
        for number in range(500)
    ]
    started = perf_counter()
    visible = combined_filter(rows, text='Carga', voltage_kv='220', state='En servicio')
    edits = build_operations(visible, 'load_scale', 1.005, factor=True)
    assert len(edits) == 500
    assert perf_counter() - started < 1.0
