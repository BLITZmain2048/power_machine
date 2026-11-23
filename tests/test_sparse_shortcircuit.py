"""Sparse sequence matrices and factors match the dense analytical solution."""
import numpy as np
import pytest
pytest.importorskip('scipy')
from test_shortcircuit_analytical import fixture
from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions, run_shortcircuit
from power_machine.shortcircuit.compiled import ShortCircuitWorkspace


def test_sparse_sequence_fault_matches_dense():
    model,data=fixture(two_buses=True)
    dense=run_shortcircuit(model,FaultSpec('B2','llg'),ShortCircuitOptions('iec60909','maximum',backend='dense'),data)
    sparse=run_shortcircuit(model,FaultSpec('B2','llg'),ShortCircuitOptions('iec60909','maximum',backend='sparse'),data)
    assert sparse.status==dense.status
    assert sparse.quantities['Ik_initial_sym_ka']==pytest.approx(dense.quantities['Ik_initial_sym_ka'],rel=1e-10)
    for phase,value in dense.quantities['phase_currents_ka'].items():
        assert sparse.quantities['phase_currents_ka'][phase]==pytest.approx(value,rel=1e-10)
    work=ShortCircuitWorkspace(model,data,ShortCircuitOptions('iec60909','maximum',backend='sparse'),'3ph')
    net=work.network('B1');nodes=net['topology']['islands'][0]['nodes']
    y,_,factor=work.matrix('B1',net,nodes,1)
    assert y.format=='csr' and factor.factor is not None and factor.columns is None
