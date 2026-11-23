"""Optional SciPy backend. Never allocate dense Ybus or Jacobian in sparse mode."""
import importlib
import numpy as np
import pytest
pytest.importorskip('scipy')
from power_machine.compiled import compile_network
from power_machine.benchmarks.ieee39 import build_model

def test_sparse_matches_dense_and_reuses_pattern():
    p=compile_network(build_model())
    dense=p.run_loadflow(options={'backend':'dense'})
    sparse=p.run_loadflow(options={'backend':'sparse'})
    assert sparse['backend']=='sparse' and sparse['converged']
    for a,b in zip(dense['buses'],sparse['buses']):
        assert a['u_pu']==pytest.approx(b['u_pu'],rel=1e-10,abs=1e-12)
        assert a['angle_deg']==pytest.approx(b['angle_deg'],rel=1e-9,abs=1e-10)
    state=p.state(p.model,{}, {'backend':'sparse'})
    plan=state['plans'][0]
    assert plan['ybus'].format=='csr'
    assert plan['ybus'].nnz<10*len(p.model['buses'])
    patterns=list(plan['patterns'].values())
    p.run_loadflow({'gen_p_mw':{'G30':200}}, {'backend':'sparse','warm_start':False})
    assert list(plan['patterns'].values())==patterns

def test_sparse_path_has_no_dense_ybus_or_jacobian(monkeypatch):
    lf=importlib.import_module('power_machine.loadflow')
    def forbidden(*args,**kwargs):raise AssertionError('Dense Jacobian in sparse mode')
    monkeypatch.setattr(lf,'_jacobian',forbidden)
    p=compile_network(build_model(),backend='sparse')
    assert p.run_loadflow()['converged']
