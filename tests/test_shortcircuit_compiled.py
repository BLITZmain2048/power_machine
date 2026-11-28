"""Sequence-factor reuse, correction isolation and content invalidation."""
import importlib
from dataclasses import replace
import numpy as np
from test_shortcircuit_analytical import fixture, datum
from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions, run_shortcircuit
from power_machine.shortcircuit.compiled import ShortCircuitWorkspace, cached_workspace


def test_fault_columns_share_one_factorization(monkeypatch):
    model,data=fixture(two_buses=True)
    options=ShortCircuitOptions('iec60909','maximum')
    work=ShortCircuitWorkspace(model,data,options,'3ph')
    network=work.network('B1');nodes=network['topology']['islands'][0]['nodes']
    calls=[];original=np.linalg.solve
    def solve(*args):
        calls.append(args[0].shape)
        return original(*args)
    monkeypatch.setattr(np.linalg,'solve',solve)
    y,_,factor=work.matrix('B1',network,nodes,1)
    _,_,other=work.matrix('B2',work.network('B2'),nodes,1)
    assert factor is other and len(calls)==1
    for i in range(len(nodes)):
        assert np.linalg.norm(y@factor.column(i)-np.eye(len(nodes))[:,i])<1e-12


def test_internal_station_faults_have_distinct_correction_keys():
    model,data=fixture(two_buses=True)
    data.elements['L1']['station_source_id']='S1'
    options=ShortCircuitOptions('iec60909','maximum',profile='iec_ref2016_c1_v1')
    work=ShortCircuitWorkspace(model,data,options,'3ph')
    assert work.correction_key('B1')=='B1'
    assert work.correction_key('B2') is None


def test_modified_supplement_invalidates_public_workspace_cache():
    model,data=fixture(two_buses=True)
    options=ShortCircuitOptions('iec60909','maximum')
    before=cached_workspace(model,data,options,'3ph')
    data.sources['S1']['parameters']['z1']=datum(.1+.2j)
    after=cached_workspace(model,data,options,'3ph')
    assert after is not before
    assert cached_workspace(model,data,replace(options,case='minimum'),'3ph') is not after


def test_normal_fault_has_no_condition_svd(monkeypatch):
    model,data=fixture(two_buses=True)
    def forbidden(*args):raise AssertionError('Condition SVD in normal SC')
    monkeypatch.setattr(np.linalg,'cond',forbidden)
    result=run_shortcircuit(model,FaultSpec('B2','3ph'),ShortCircuitOptions('iec60909','maximum'),data)
    assert result.status in {'completed','partial'},result.diagnostics


def test_fault_dependent_corrections_never_share_factors(monkeypatch):
    module=importlib.import_module('power_machine.shortcircuit.compiled')
    model,data=fixture(two_buses=True)
    data.elements['L1']['station_source_id']='S1'
    options=ShortCircuitOptions('iec60909','maximum',profile='iec_ref2016_c1_v1')
    monkeypatch.setattr(module,'prepare_network',lambda model,data,options,kind,bus:{'fault':bus})
    monkeypatch.setattr(module,'stamp_island',lambda net,nodes,seq,backend:(np.eye(2)*(2 if net['fault']=='B1' else 3),[]))
    work=ShortCircuitWorkspace(model,data,options,'3ph')
    internal=work.matrix('B1',work.network('B1'),['B1','B2'],1)[2]
    external=work.matrix('B2',work.network('B2'),['B1','B2'],1)[2]
    assert internal is not external
    assert internal.column(0)[0]==.5
    assert external.column(0)[0]==1/3


def test_shared_workspace_validates_supplement_once_but_checks_each_fault(monkeypatch):
    solver=importlib.import_module('power_machine.shortcircuit.solver')
    model,data=fixture(two_buses=True);options=ShortCircuitOptions('iec60909','maximum')
    work=ShortCircuitWorkspace(model,data,options,'3ph')
    original=solver.asdict;calls=[]
    def asdict(value):
        if value is data:calls.append(value)
        return original(value)
    monkeypatch.setattr(solver,'asdict',asdict)
    for bus in ('B1','B2'):
        result=solver._run_shortcircuit(model,FaultSpec(bus,'3ph'),options,data,workspace=work)
        assert result.status in {'partial','completed'}
    assert len(calls)==1
    invalid=solver._run_shortcircuit(model,FaultSpec('B2','3ph',r_fault_ohm=-1),options,data,workspace=work)
    assert invalid.status=='invalid_input'
    assert any(d['code']=='invalid_fault_impedance' for d in invalid.diagnostics)


def test_cached_snapshot_isolated_from_equal_models_and_source_mutation():
    import copy
    model,data=fixture(two_buses=True);other=copy.deepcopy(model);other_data=copy.deepcopy(data)
    options=ShortCircuitOptions('iec60909','maximum')
    first=cached_workspace(model,data,options,'3ph')
    model['buses'][0]['un_kv']=20.
    data.sources['S1']['parameters']['z1']=datum(9+9j)
    reused=cached_workspace(other,other_data,options,'3ph')
    assert reused is first
    assert reused.model['buses'][0]['un_kv']==10.
    assert reused.data.sources['S1']['parameters']['z1']==other_data.sources['S1']['parameters']['z1']


def test_factorization_failure_keeps_numerical_failure_status(monkeypatch):
    module=importlib.import_module('power_machine.shortcircuit.compiled')
    model,data=fixture(two_buses=True);options=ShortCircuitOptions('iec60909','maximum')
    work=ShortCircuitWorkspace(model,data,options,'3ph')
    monkeypatch.setattr(module,'cached_workspace',lambda *args:work)
    monkeypatch.setattr(module,'stamp_island',lambda network,nodes,seq,backend:(np.zeros((len(nodes),len(nodes)),complex),[]))
    result=run_shortcircuit(model,FaultSpec('B2','3ph'),options,data)
    assert result.status=='numerical_failure'
    assert any(d['code']=='singular_sequence_matrix' for d in result.diagnostics)
    assert result.quantities['Ik_initial_sym_ka'] is None
