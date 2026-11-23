"""Synthetic analytical external-grid cases with no exported network data."""
import math
from copy import deepcopy
import numpy as np
import pytest
from power_machine.external_grid import impedance,normalize_external
from power_machine.loadflow import run_loadflow
from power_machine.shortcircuit import FaultSpec,ShortCircuitData,ShortCircuitOptions,run_shortcircuit
from power_machine.dynamics.external_grid import ExternalGridRMS
from power_machine.dynamics.network import DynamicNetwork
from power_machine.dynamics.solver import DAESolver


def grid():
    return normalize_external(dict(id='x',name='External synthetic',path='synthetic/x',**{'class':'ElmXnet'},bus='a',outserv=0,
        params=dict(bustp='SL',usetp=1,phiini=15,pgini=0,qgini=0,snss=1000,snssmin=500,rntxn=.1,rntxnmin=.2),
        units=dict(usetp='p.u.',phiini='deg',pgini='MW',qgini='Mvar',snss='MVA',snssmin='MVA')))


def network():
    return dict(meta=dict(s_base_mva=100,f_nom_hz=50),buses=[dict(id='a',name='a',un_kv=10)],
        gens=[grid()],branches=[],branches3=[],switches=[],shunts=[],loads=[],warnings=[])


@pytest.mark.parametrize('case,sk,rx',[('maximum',1000,.1),('minimum',500,.2)])
def test_native_fault_matches_sk(case,sk,rx):
    m=network()
    z=impedance(m['gens'][0],10,case=case)
    assert abs(z)==pytest.approx(100/sk)
    assert z.real/z.imag==pytest.approx(rx)
    data=ShortCircuitData('power_machine-sc-data-v1',{}, {})
    result=run_shortcircuit(m,FaultSpec('a','3ph'),ShortCircuitOptions('iec60909',case),data)
    assert result.quantities['Ik_initial_sym_ka']==pytest.approx(sk/(math.sqrt(3)*10)),result.diagnostics


def test_current_input_and_x_over_r():
    g=grid();g['params'].pop('snss');g['params'].pop('rntxn')
    g['params'].update(ikss=10,xntrn=10);g['units']['ikss']='kA'
    z=impedance(g,10)
    assert abs(z)==pytest.approx(10/(math.sqrt(3)*10))
    assert z.imag/z.real==pytest.approx(10)


def test_slack_voltage_and_angle():
    result=run_loadflow(network())
    assert result['converged']
    assert result['buses'][0]['u_pu']==pytest.approx(1)
    assert result['buses'][0]['angle_deg']==pytest.approx(15)


@pytest.mark.parametrize('mode',['PV','PQ'])
def test_operating_modes(mode):
    m=network();g=deepcopy(grid());g.update(id='other',bus='b')
    g['params'].update(bustp=mode,pgini=10,qgini=2,usetp=1.01)
    normalize_external(g);m['gens'].append(g)
    m['buses'].append(dict(id='b',name='b',un_kv=10))
    m['branches']=[dict(id='line',name='line',**{'class':'ElmSind'},bus_from='a',bus_to='b',params=dict(rrea=0,xrea=.1))]
    result=run_loadflow(m);assert result['converged']
    other=next(row for row in result['gens'] if row['id']=='other')
    assert other['p_mw']==pytest.approx(10,abs=1e-6)
    if mode=='PQ':assert other['q_mvar']==pytest.approx(2,abs=1e-6)
    else:assert next(row for row in result['buses'] if row['id']=='b')['u_pu']==pytest.approx(1.01)


def test_rms_thevenin_initialization_and_voltage_drop():
    m=network();dev=ExternalGridRMS(m['gens'][0],bus_un_kv=10,s_base_mva=100)
    v=1+0j;s=10+2j;state=dev.init(v,s)
    assert dev.current_injection(state,v)==pytest.approx(s.conjugate()/100)
    assert dev.current_injection(state,0)==pytest.approx(dev.emf/dev.z)
    # Constant impedance demand Y=.1-.02j gives V=E/(1+ZY).
    y=.1-.02j
    expected=dev.emf/(1+dev.z*y)
    assert dev.current_injection(state,expected)==pytest.approx(y*expected)


def test_missing_minimum_never_uses_maximum():
    g=grid();g['params'].pop('snssmin')
    with pytest.raises(ValueError):impedance(g,10,case='minimum')


def test_native_import_keeps_parameters(monkeypatch):
    from types import SimpleNamespace
    from pathlib import Path
    from power_machine import builder
    objects=[dict(object_id=i,name=i,scope='synthetic',relative_path=i,**{'class':c}) for i,c in [('a','ElmTerm'),('x','ElmXnet')]]
    g=grid()
    params=[dict(object_id='a',attribute='uknom',value=10,unit='kV')]
    params += [dict(object_id='x',attribute=k,value=v,unit=g['units'].get(k,'')) for k,v in g['params'].items()]
    package=SimpleNamespace(root=Path('synthetic'),sha256_inputs={},interface_map=[],by_id={o['object_id']:o for o in objects},
        by_path={o['relative_path']:o for o in objects},scopes={'synthetic':dict(parameters=params,references=[],ports=[dict(element_id='x',port='bus1',terminal_relative_path='a',status='PRESENT')])})
    monkeypatch.setattr(builder,'load_package',lambda *a,**kw:package)
    m=builder.build_model('synthetic',f_nom_hz=50)
    assert m['gens'][0]['params']==g['params']
    assert m['gens'][0]['bus']=='a' and m['gens'][0]['ref']
    assert run_loadflow(m)['converged']


def test_optional_inertia_equilibrium_and_acceleration():
    g=grid();g['params'].update(tag=10,cmax=1.1);g['units']['tag']='s'
    d=ExternalGridRMS(g,bus_un_kv=10,s_base_mva=100)
    x=d.init(1,10+0j)
    np.testing.assert_allclose(d.f(0,x,[],1),[0,0],atol=1e-14)
    v=.98+0j
    pe=(d.emf*d.current_injection(x,v).conjugate()).real
    expected=(d.mechanical_power-pe)/(10*(.2*math.hypot(1,.1)*1000/1.1)/100)
    assert d.f(0,x,[],v)[1]==pytest.approx(expected)
    g['rms_fixed_voltage']=True
    assert ExternalGridRMS(g,bus_un_kv=10,s_base_mva=100).n_states==0


@pytest.mark.parametrize('inertia',[False,True])
def test_rms_dae_equilibrium_and_fault_step(inertia):
    m=network();g=m['gens'][0]
    if inertia:g['params'].update(tag=10,cmax=1.1);g['units']['tag']='s'
    net=DynamicNetwork(m)
    device=ExternalGridRMS(g,bus_un_kv=10,s_base_mva=100)
    solver=DAESolver(net,[device],step_s=.001)
    state,_=solver.initialize({'a':1+0j},{'x':0j})
    state,_=solver.step(state)
    np.testing.assert_allclose(state.v,[1],atol=1e-10)
    net.set_fault('a',.5j)
    state,_=solver.solve_algebraic(state)
    expected=1/(1+device.z/(.5j))
    assert state.v[0]==pytest.approx(expected)
    state,_=solver.step(state)
    assert np.isfinite(state.v).all()
