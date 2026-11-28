"""Ruta operativa N-1, detalle de límites y precisión sin pérdida en CSV."""
import csv
import json
import pytest
from power_machine.gui.results_tables import format_value, summary_row, export_rows
from power_machine.gui.i18n import language, set_language
from power_machine.gui.bus import bus
from power_machine.gui.panels.contingency import create_panel
from test_gui_results_revision2 import cases
from test_contingency_worker_contract import FakeContext
from gui_support import ieee_model


def test_panel_operativo_uses_api_ranking_and_explains_b25(tk_root, tmp_path, cases):
    old = language()
    set_language('es')
    panel = create_panel(tk_root, FakeContext(ieee_model(), tmp_path))
    panel.pack(fill='both', expand=True)
    result = dict(schema='power_machine-parity-result-v1', status='partial', payload=dict(cases=cases))
    panel.controller.on_message(('result', result))
    tk_root.update()
    ranking = panel.ranking
    assert ranking.ranked_table.rows[0]['case'] == 'L21-22'
    target = next(c for c in cases if c['outages'][0]['object_id'] == 'L03-04')
    messages = []
    token = bus.subscribe('selection.changed', lambda **payload: messages.append(payload))
    try:
        ranking.select_case(target['case_id'])
        tk_root.update()
        row = next(r for r in ranking.ranked_table.rows if r['case'] == 'L03-04')
        assert len(row['violation']) < 100 and 'B25' in row['violation'] and '1.0703' in row['violation']
        first = ranking.detail_tree.get_children()[0]
        values = ranking.detail_tree.item(first, 'values')
        assert values[1:] == ('B25', '1.0703', '0.9500', '1.0500', 'p.u.')
        assert messages and messages[-1]['ids'] == ['L03-04']
        bus.publish('selection.changed', ids=['L02-03'], source='diagram')
        tk_root.update()
        assert ranking.selected_case['outages'][0]['object_id'] == 'L02-03'
        exported = tmp_path/'ranking.csv'
        export_rows(exported, ranking.ranked_table.visible_columns, ranking.ranked_table.visible_rows)
        with exported.open(encoding='utf-8-sig', newline='') as stream:
            records = list(csv.DictReader(stream))
        raw = next(r for r in records if r['ID REF'] == target['case_id'])
        detail = json.loads(raw['Detalle de violaciones · precisión completa en CSV'])
        assert next(v['u_pu'] for v in detail if v['object_id'] == 'B25') == next(v['u_pu'] for v in target['violations'] if v['object_id']=='B25')
        set_language('en')
        tk_root.update()
        assert 'worst' in next(r for r in ranking.ranked_table.rows if r['case']=='L03-04')['violation']
    finally:
        bus.unsubscribe(token)
        panel.destroy()
        set_language(old)


@pytest.mark.parametrize('key,raw,expected', [('u_pu',1.0703427,'1.0703'),('max_u_pu',1.0703427,'1.0703'),
    ('angle_deg',-12.123456,'-12.12'),('p_mw',12.123456,'12.12'),('q_mvar',12.123456,'12.12'),
    ('loading_pct',157.719405,'157.72'),('i_from_ka',.123456,'.123')])
def test_precision_shared_by_rows_and_summary(key,raw,expected):
    if expected.startswith('.'):
        expected='0'+expected
    assert format_value(raw,key)==expected
    summary=summary_row([{key:raw}],[(key,key)],mode='extremes')
    assert summary[key]==expected+' … '+expected
