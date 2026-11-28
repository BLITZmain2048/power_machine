"""Paridad de ranking y trazabilidad de límites publicados por el motor."""
from copy import deepcopy
import pytest
from power_machine.api import ContingencyResult, StudyTable
from power_machine.gui.results_tables import (ranked_contingency_cases, contingency_violation_text,
                                              ContingencyRanking, ResultsTable)
from power_machine.gui.settings import Settings
from power_machine.gui.i18n import tr, language, set_language


@pytest.fixture(scope='module')
def cases():
    from power_machine.resources import ieee39_root
    from power_machine.study import load_network
    from power_machine.contingency import run_contingencies, ContingencyOptions
    result = run_contingencies(load_network(ieee39_root() / 'model.json'), {}, ContingencyOptions())
    return result['payload']['cases']


def test_rank_uses_public_api_and_separates_unsolved(cases, monkeypatch):
    before = deepcopy(cases)
    method = ContingencyResult.rank
    called = []
    def spy(result, *args, **kwargs):
        called.append(True)
        return method(result, *args, **kwargs)
    monkeypatch.setattr(ContingencyResult, 'rank', spy)
    solved, unresolved = ranked_contingency_cases(cases)
    canonical = ContingencyResult(status='completed', cases=StudyTable('cases', tuple(
        dict(case, unserved_mw=case['metrics'].get('unsupplied_p_mw'),
             max_loading_pct=case['metrics'].get('max_loading_pct')) for case in cases))).rank().to_records()
    failed = {case['case_id'] for case in unresolved}
    assert [c['case_id'] for c in solved] == [c['case_id'] for c in canonical if c['case_id'] not in failed]
    assert solved[0]['outages'][0]['object_id'] == 'L21-22'
    assert any(c['outages'][0]['object_id'] == 'G39' for c in unresolved)
    assert called and cases == before


def test_low_loading_violations_are_actual_overvoltages(cases):
    expected = {'L03-04': ('B25', 1.07034), 'L02-03': ('B02', 1.07113),
                'L16-24': ('B24', 1.07970), 'L08-09': ('B36', 1.06360)}
    for case in cases:
        outage = case['outages'][0]['object_id']
        if outage not in expected:
            continue
        assert case['status'] == 'limit_violation'
        assert case['metrics']['max_loading_pct'] < 100
        assert all(v['kind'] == 'voltage' for v in case['violations'])
        ident, value = expected[outage]
        assert ident in contingency_violation_text(case)
        assert f'{value:.5f}' in contingency_violation_text(case)


def test_substation_default_and_shared_preference(tk_root, tmp_path):
    store = Settings(tmp_path / 'settings.json')
    rows = [dict(id='B1', name='B1', u_pu=1.)]
    table = ResultsTable(tk_root, 'buses', rows, settings_store=store)
    assert 'substation' not in dict(table.visible_columns)
    table.column_visible['substation'].set(True)
    table._columns_changed()
    assert store.get('panel_options')['data']['columns']['substation'] is True
    reopened = ResultsTable(tk_root, 'buses', rows, settings_store=Settings(store.path))
    assert 'substation' in dict(reopened.visible_columns)
    store.set('panel_options', {})
    populated = ResultsTable(tk_root, 'buses', [dict(id='B2', substation='Station A')], settings_store=store)
    assert 'substation' in dict(populated.visible_columns)


def test_ranking_groups_and_live_translation(tk_root, cases):
    old = language()
    try:
        set_language('en')
        view = ContingencyRanking(tk_root, cases)
        assert view.ranked_table.rows[0]['case'] == 'L21-22'
        g39 = next(row for row in view.unresolved_table.rows if row['case'] == 'G39')
        view.select_case(g39['object_id'])
        assert '159.46' in view.detail_hint.cget('text')
        view.select_case(view.ranked_table.rows[0]['object_id'])
        assert 'worst' in view.ranked_table.rows[0]['violation']
        set_language('es')
        tk_root.update_idletasks()
        assert 'peor' in view.ranked_table.rows[0]['violation']
        set_language('en')
        tk_root.update_idletasks()
        assert 'worst' in view.ranked_table.rows[0]['violation']
        assert tr('results.id_ref') == 'REF ID'
    finally:
        view.destroy()
        set_language(old)
