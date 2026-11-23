from gui_support import ieee_model, ieee_root
"""Pruebas sin pantalla de las tablas flexibles study."""
import csv
import time

from power_machine.gui.results_tables import (
    COLUMNS,
    alarm_tags,
    export_rows,
    filtered_rows,
    sorted_rows,
    summary_row,
)


def test_filters_use_raw_numeric_values_and_keep_missing_out_of_ranges():
    rows = [
        {'id': 'a', 'name': 'Interface', 'p_mw': 10.0, 'u_pu': 1.01},
        {'id': 'b', 'name': 'Norte', 'p_mw': 2.0, 'u_pu': None},
        {'id': 'c', 'name': 'Sur', 'p_mw': 20.0, 'u_pu': .94},
    ]
    assert [row['id'] for row in filtered_rows(rows, text='interface')] == ['a']
    assert [row['id'] for row in filtered_rows(rows, numeric_ranges={'p_mw': (5, 15)})] == ['a']
    assert [row['id'] for row in sorted_rows(rows, 'p_mw', reverse=True)] == ['c', 'a', 'b']


def test_summary_and_limits_do_not_invent_values():
    rows = [{'name': 'A', 'p_mw': 10.0, 'u_pu': .94}, {'name': 'B', 'p_mw': None, 'u_pu': 1.02}]
    columns = [('name', 'Nombre'), ('p_mw', 'P [MW]'), ('u_pu', 'U [p.u.]')]
    total = summary_row(rows, columns, 'totals')
    extremes = summary_row(rows, columns, 'extremes')
    assert total['p_mw'] == 10.0 and total['u_pu'] == 1.96
    assert extremes['p_mw'] == '10.00 … 10.00'
    assert alarm_tags(rows[0]) == ('alarm',)
    assert not alarm_tags(rows[0], {'u_pu': (.9, 1.1)})


def test_csv_keeps_full_precision_and_legacy_identifier_header(tmp_path):
    path = tmp_path / 'results.csv'
    rows = [{'id': 'pf_1', 'name': 'A', 'p_mw': 1.234567890123456}]
    export_rows(path, COLUMNS['gens'], rows)
    with path.open(encoding='utf-8-sig', newline='') as stream:
        data = list(csv.reader(stream))
    from power_machine.gui.i18n import tr
    assert data[0][0] == tr('results.id_pf')
    assert 'P [MW]' in data[0]
    assert data[1][0] == 'pf_1'
    assert '1.234567890123456' in data[1]


def test_flexible_table_prepares_500_rows_without_expensive_processing():
    rows = [{'id': f'pf_{number}', 'name': f'Barra {number}', 'p_mw': float(number),
             'loading_pct': number / 5} for number in range(500)]
    started = time.perf_counter()
    visible = filtered_rows(rows, text='barra', numeric_ranges={'p_mw': (10.0, 490.0)})
    summary = summary_row(visible, [('name', 'Nombre'), ('p_mw', 'P [MW]')])
    elapsed_s = time.perf_counter() - started
    assert len(visible) == 481 and summary['p_mw'] == sum(range(10, 491))
    assert elapsed_s < 1.0
