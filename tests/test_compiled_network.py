"""Prepared network isolation, cache invalidation and solver diagnostics."""
import copy
import importlib
import numpy as np
import pytest
from power_machine.benchmarks.ieee39 import build_model
from power_machine.compiled import compile_network, cached_network
lf=importlib.import_module('power_machine.loadflow')

def buses(result):
    return {b['id']:complex(b['u_pu'] or 0)*np.exp(1j*np.deg2rad(b['angle_deg'] or 0)) for b in result['buses']}

def test_reuses_preparation_without_deepcopy_or_restamping(monkeypatch):
    p=compile_network(build_model())
    p.run_loadflow()
    def forbidden(*args,**kwargs):raise AssertionError('Network preparation was repeated')
    monkeypatch.setattr(copy,'deepcopy',forbidden)
    monkeypatch.setattr(lf,'_stamp',forbidden)
    monkeypatch.setattr(lf,'build_topology',forbidden)
    assert p.run_loadflow({'load_scale':{p.model['loads'][0]['id']:1.01}}, {'warm_start':False})['converged']

def test_source_and_scenario_results_are_isolated():
    model=build_model();original=copy.deepcopy(model)
    p=compile_network(model)
    base=p.run_loadflow()
    changed=p.run_loadflow({'gen_p_mw':{'G30':200}}, {'warm_start':False})
    assert buses(base)!=buses(changed)
    base['islands'][0]['nodes'].clear()
    base['bus_to_node'].clear()
    restored=p.run_loadflow()
    assert restored['converged'] and restored['bus_to_node']
    assert model==original

def test_parameter_and_topology_mutations_invalidate_raw_cache():
    model=build_model();first=cached_network(model)
    model['branches'][0]['params']['X1']*=1.01
    second=cached_network(model)
    assert second is not first
    model['branches'][0]['outserv']=1
    third=cached_network(model)
    assert third is not second
    first.invalidate(model)
    assert first.fingerprint==third.fingerprint

def test_outages_do_not_contaminate_base_and_match_fresh_preparation():
    model=build_model();p=compile_network(model)
    before=buses(p.run_loadflow())
    scenario={'outserv':{'L01-02':1}}
    cached=p.run_loadflow(scenario,{'warm_start':False})
    fresh=compile_network(model).run_loadflow(scenario,{'warm_start':False})
    assert buses(cached)==buses(fresh)
    assert buses(p.run_loadflow())==before

def test_condition_estimate_is_opt_in(monkeypatch):
    p=compile_network(build_model())
    def forbidden(*args,**kwargs):raise AssertionError('SVD in normal solve')
    monkeypatch.setattr(np.linalg,'cond',forbidden)
    assert p.run_loadflow()['jacobian_condition'] is None

def test_explicit_condition_diagnostic():
    r=lf.run_loadflow(build_model(),options={'diagnostic_condition':True})
    assert r['jacobian_condition']>1

def test_sparse_fallback_is_explicit(monkeypatch):
    compiled=importlib.import_module('power_machine.compiled')
    monkeypatch.setattr(compiled,'_scipy',lambda:None)
    p=compile_network(build_model())
    result=p.run_loadflow(options={'backend':'sparse'})
    assert result['backend']=='dense'
    assert result['diagnostics'][0]['code']=='sparse_backend_unavailable'
    assert p.run_loadflow(options={'backend':'dense'})['diagnostics']==[]

def test_parameter_deltas_keep_input_unchanged():
    model=build_model();p=compile_network(model)
    ident=model['branches'][0]['id'];old=model['branches'][0]['params']['X1']
    p.run_loadflow({'params':{ident:{'X1':old*1.02}}},{'warm_start':False})
    assert model['branches'][0]['params']['X1']==old
    assert p.model['branches'][0]['params']['X1']==old
