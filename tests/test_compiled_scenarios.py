"""Operating deltas and result isolation on reusable network snapshots."""
import importlib
import pytest
from power_machine.benchmarks.ieee39 import build_model
from power_machine.compiled import compile_network, scenario_view
from power_machine.elements import load_pq_mw


def test_total_power_is_independent_of_delta_order():
    model=build_model()
    ident=model['loads'][0]['id']
    a=scenario_view(model,{'load_p_mw':{ident:42.0},'load_scale':{ident:0.7}})
    b=scenario_view(model,{'load_scale':{ident:0.7},'load_p_mw':{ident:42.0}})
    assert a==b
    assert load_pq_mw(a['loads'][0])[0]==pytest.approx(42.0)
    assert model['loads'][0]['params']!=a['loads'][0]['params']


def test_zero_scale_has_explicit_power_contract():
    model=build_model();ident=model['loads'][0]['id']
    with pytest.raises(ValueError,match='zero_load_scale'):
        scenario_view(model,{'load_scale':{ident:0},'load_p_mw':{ident:1}})
    view=scenario_view(model,{'load_scale':{ident:0},'load_p_mw':{ident:0}})
    assert load_pq_mw(view['loads'][0])[0]==0


def test_three_winding_taps_require_named_windings():
    model={'branches3':[{'id':'T','class':'ElmTr3','params':{}}]}
    view=scenario_view(model,{'tap_pos':{'T':{'hv':2,'lv':-1}}})
    assert view['branches3'][0]['params']=={'n3tap_h':2.,'n3tap_l':-1.}
    assert model['branches3'][0]['params']=={}
    with pytest.raises(ValueError,match='invalid_tap_delta'):
        scenario_view(model,{'tap_pos':{'T':2}})


def test_failed_solve_keeps_condition_diagnostic():
    prepared=compile_network(build_model())
    result=prepared.run_loadflow({'load_scale':{prepared.model['loads'][0]['id']:2}},
                                 {'max_iter':0,'warm_start':False})
    assert not result['converged']
    assert result['islands'][0]['jacobian_condition'] is not None


def test_prepared_fingerprint_matches_public_json_hash_for_unicode():
    from power_machine.contingency.results import fingerprint
    model=build_model();model['buses'][0]['name']='Bus with accented name: á'
    assert compile_network(model).fingerprint==fingerprint(model)
