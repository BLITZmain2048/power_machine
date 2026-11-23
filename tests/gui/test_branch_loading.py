from power_machine.gui.i18n import tr
"""Pruebas unitarias de corriente y cargabilidad trazable de ramas."""
import math
from types import SimpleNamespace
import pytest
pytestmark = pytest.mark.gui
from power_machine.gui.diagram.view import result_text
from power_machine.gui.diagram.view import DiagramView
from power_machine.loading import branch_loading

def model_with(branches=None, branches3=None):
    return dict(branches=branches or [], branches3=branches3 or [])

def lf_with(buses, branches):
    return dict(buses=buses, branches=branches)

def test_line_uses_declared_ka_and_parallel_circuits():
    line = dict(id='line', name='Línea', **{'class': 'ElmLne'}, params=dict(Inom=0.5, nlnum=2), units=dict(Inom='kA'), type_params={}, type_units={})
    lf = lf_with([dict(id='a', u_kv=100), dict(id='b', u_kv=100)], [dict(id='line', bus_from='a', p_from_mw=10, q_from_mvar=0, bus_to='b', p_to_mw=-9, q_to_mvar=0)])
    values = branch_loading(model_with([line]), lf)['line']
    assert values['rating']['rating_ka'] == pytest.approx(1.0)
    assert values['ends']['from']['s_mva'] == pytest.approx(10.0)
    assert values['ends']['from']['i_ka'] == pytest.approx(10 / math.sqrt(3) / 100)
    assert values['ends']['from']['loading_pct'] == pytest.approx(1000 / math.sqrt(3) / 100)
    assert values['loading_pct'] == values['ends']['from']['loading_pct']

def test_line_converts_declared_amperes_not_as_ka():
    line = dict(id='line', name='Línea A', **{'class': 'ElmLne'}, params=dict(Inom=1000, nlnum=1), units=dict(Inom='A'), type_params={}, type_units={})
    lf = lf_with([dict(id='a', u_kv=10), dict(id='b', u_kv=10)], [dict(id='line', bus_from='a', p_from_mw=math.sqrt(3) * 10, q_from_mvar=0, bus_to='b', p_to_mw=0, q_to_mvar=0)])
    values = branch_loading(model_with([line]), lf)['line']
    assert values['rating']['rating_ka'] == pytest.approx(1.0)
    assert values['ends']['from']['i_ka'] == pytest.approx(1.0)
    assert values['loading_pct'] == pytest.approx(100.0)

def test_line_rejects_unverified_current_unit():
    line = dict(id='line', name='Línea sin unidad', **{'class': 'ElmLne'}, params=dict(Inom=1000, nlnum=1), units=dict(Inom='MA'), type_params={}, type_units={})
    lf = lf_with([dict(id='a', u_kv=10), dict(id='b', u_kv=10)], [dict(id='line', bus_from='a', p_from_mw=10, q_from_mvar=0, bus_to='b', p_to_mw=-10, q_to_mvar=0)])
    values = branch_loading(model_with([line]), lf)['line']
    assert values['loading_pct'] is None
    assert 'unidad no soportada' in values['cause']

def test_transformer_uses_mva_per_unit_and_number_of_units():
    transformer = dict(id='tr2', name='TR2', **{'class': 'ElmTr2'}, params=dict(ntnum=2), units={}, type_params=dict(strn=10), type_units=dict(strn='MVA'))
    lf = lf_with([dict(id='h', u_kv=100), dict(id='l', u_kv=10)], [dict(id='tr2', bus_from='h', p_from_mw=12, q_from_mvar=16, bus_to='l', p_to_mw=-12, q_to_mvar=-16)])
    values = branch_loading(model_with([transformer]), lf)['tr2']
    assert values['rating']['rating_mva'] == pytest.approx(20.0)
    assert values['ends']['from']['s_mva'] == pytest.approx(20.0)
    assert values['ends']['from']['loading_pct'] == pytest.approx(100.0)
    assert values['ends']['to']['i_ka'] == pytest.approx(20 / math.sqrt(3) / 10)

def test_three_winding_uses_each_winding_rating():
    transformer = dict(id='tr3', name='TR3', params={}, units={}, **{'class': 'ElmTr3'}, type_params=dict(strn3_h=100, strn3_mv=50, strn3_lv=25), type_units=dict(strn3_h='MVA', strn3_mv='MVA', strn3_lv='MVA'))
    lf = lf_with([dict(id='h', u_kv=100), dict(id='m', u_kv=20), dict(id='l', u_kv=10)], [dict(id='tr3', bus_hv='h', p_hv_mw=50, q_hv_mvar=0, bus_mv='m', p_mv_mw=-30, q_mv_mvar=0, bus_lv='l', p_lv_mw=-20, q_lv_mvar=0)])
    values = branch_loading(model_with(branches3=[transformer]), lf)['tr3']
    assert values['ends']['hv']['loading_pct'] == pytest.approx(50.0)
    assert values['ends']['mv']['loading_pct'] == pytest.approx(60.0)
    assert values['ends']['lv']['loading_pct'] == pytest.approx(80.0)
    assert values['loading_pct'] == pytest.approx(80.0)

def test_missing_rating_is_explicit_and_result_text_uses_local_end_loading():
    zpu = dict(id='z', name='Equivalente externo', params={}, units={}, type_params={}, type_units={}, **{'class': 'ElmZpu'})
    lf = lf_with([dict(id='a', u_kv=220), dict(id='b', u_kv=220)], [dict(id='z', bus_from='a', p_from_mw=10, q_from_mvar=0, bus_to='b', p_to_mw=-10, q_to_mvar=0)])
    values = branch_loading(model_with([zpu]), lf)['z']
    assert values['loading_pct'] is None
    assert 'ElmZpu' in values['cause']
    symbol = type('Symbol', (), dict(kind='branch', item=dict(_local_bus='b')))()
    text = result_text(symbol, dict(bus_from='a', bus_to='b', p_from_mw=1, q_from_mvar=0, p_to_mw=-1, q_to_mvar=0, loading_pct=80, loading_from_pct=80, loading_to_pct=40), include_loading=True)
    assert tr('diagram.loading') + ' 40.0 %' in text

def test_nonconverged_island_masks_line_before_current_calculation():
    line = dict(id='line', name='Línea', **{'class': 'ElmLne'}, params=dict(Inom=1, nlnum=1), units=dict(Inom='kA'), type_params={}, type_units={})
    lf = lf_with([dict(id='a', u_kv=100, status='resuelto'), dict(id='b', u_kv=100, status='resuelto')], [dict(id='line', bus_from='a', p_from_mw=100, q_from_mvar=0, bus_to='b', p_to_mw=-100, q_to_mvar=0, status='resuelto')])
    lf['islands'] = [dict(status='no_convergente', buses=['a', 'b'], elements=dict(branches=['line'], branches3=[]))]
    values = branch_loading(model_with([line]), lf)['line']
    assert values['loading_pct'] is None
    assert values['ends']['from']['i_ka'] is None
    assert 'no convergente' in values['cause']

def test_deenergized_island_and_metric_are_explicit():
    transformer = dict(id='tr2', name='TR2', **{'class': 'ElmTr2'}, params=dict(ntnum=1), units={}, type_params=dict(strn=10), type_units=dict(strn='MVA'))
    lf = lf_with([dict(id='h', u_kv=100), dict(id='l', u_kv=10)], [dict(id='tr2', bus_from='h', p_from_mw=10, q_from_mvar=0, bus_to='l', p_to_mw=-10, q_to_mvar=0)])
    lf['islands'] = [dict(status='sin_referencia', buses=['h', 'l'], elements=dict(branches=['tr2'], branches3=[]))]
    values = branch_loading(model_with([transformer]), lf)['tr2']
    assert values['loading_pct'] is None
    assert 'desenergizada' in values['cause']
    assert values['loading_metric'] == dict(thermal_factors_included=False, overload_factors_included=False, quantity='apparent_power_ratio', numerator='S extremo [MVA]', denominator='strn × ntnum [MVA]', per_winding=False)

def test_dense_result_boxes_keep_buses_and_selected_element_at_low_zoom():
    view = SimpleNamespace(show_results=True, is_dense=True, scale=0.65, result_detail_zoom=0.8, selected_id='selected')
    bus = SimpleNamespace(id='bus', kind='bus', result_box=(0, 0, 1, 1))
    selected = SimpleNamespace(id='selected', kind='branch', result_box=(0, 0, 1, 1))
    other = SimpleNamespace(id='other', kind='branch', result_box=(0, 0, 1, 1))
    assert DiagramView.result_visible(view, bus)
    assert DiagramView.result_visible(view, selected)
    assert not DiagramView.result_visible(view, other)
