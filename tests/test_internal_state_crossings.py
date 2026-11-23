"""Cruces internos de estado, sin ajustar parámetros ni tolerancias DAE."""
from pathlib import Path
import numpy as np
import pytest

from power_machine.dynamics.dsl_ir import BlockIR
from power_machine.dynamics.dsl_compile import CompiledDSL
from power_machine.dynamics.dsl_switching import inspect_block
from power_machine.dynamics.solver import DAESolver, DAEState, SolverConvergenceError


class Network:
    node_ids = ('bus',)
    ybus = np.array([[1+0j]])

    def index_of_bus(self, bus):
        return 0


class Device:
    object_id = 'pf_test'
    relative_path = 'Test.ElmDsl'
    bus_id = 'bus'
    n_states = 1
    n_alg = 0

    def __init__(self, equation='select(x<1,100,0)'):
        ir = BlockIR.from_fields('type', 'Macro.BlkDef', {
            'sStates': ['x'], 'sOutput': ['y'], 'sAddEquat': [f'x.={equation}', 'y=x']})
        self.block = CompiledDSL(self.object_id, self.relative_path, 'type',
                                 ir.relative_path, ir, {}, (), (), ('x',))
        self.blocks = {'block': self.block}
        self.slices = {'block': slice(0, 1)}

    def f(self, t, x, z, v):
        return self.block.evaluate(t, x, {})[1]

    def g(self, t, x, z, v):
        return np.empty(0)

    def current_injection(self, x, v):
        return 1+0j


def state(x=.9):
    return DAEState(.009, np.array([x]), np.empty(0), np.array([1+0j]))


def test_original_threshold_failure_is_reproducible():
    solver = DAESolver(Network(), [Device()], restart_euler=False)
    solver.internal_events_enabled = False
    with pytest.raises(SolverConvergenceError):
        solver.step(state(.9000000000000241), .001)


def test_threshold_landing_and_frozen_jacobian():
    solver = DAESolver(Network(), [Device()], restart_euler=False)
    result, metric = solver.step(state(.9000000000000241), .001)
    assert result.t == pytest.approx(.01, abs=1e-11)
    assert result.x[0] == 1.
    assert len(solver.internal_events) == 1
    following, _ = solver.step(result, .0005)
    assert following.x[0] == 1.
    assert solver.internal_events[0]['relative_path'] == 'Test.ElmDsl'


def test_crossing_inside_step_and_downward_crossing():
    solver = DAESolver(Network(), [Device()], restart_euler=False)
    result, _ = solver.step(state(.96), .001)
    assert result.t == pytest.approx(.0094, abs=1e-11)
    assert result.x[0] == 1.
    down = DAESolver(Network(), [Device('select(x>0,-100,0)')], restart_euler=False)
    result, _ = down.step(state(.03), .001)
    assert result.t == pytest.approx(.0093, abs=1e-11)
    assert result.x[0] == 0.


def test_no_crossing_is_bit_identical():
    ordinary = DAESolver(Network(), [Device()], restart_euler=False)
    ordinary.internal_events_enabled = False
    protected = DAESolver(Network(), [Device()], restart_euler=False)
    a, b = state(.1), state(.1)
    for _ in range(4):
        a, _ = ordinary.step(a, .001)
        b, _ = protected.step(b, .001)
        assert a.x.tobytes() == b.x.tobytes()
        assert a.v.tobytes() == b.v.tobytes()
    assert not protected.internal_events


def test_inventory_preserves_model_macro_and_expression():
    rows, surfaces = inspect_block(Device().block)
    assert len(surfaces) == 1
    assert surfaces[0].level == 1.
    assert any(row['kind'] == 'select' for row in rows)
    assert all(row['object_id'] == 'pf_test' for row in rows)


def test_curved_guard_lands_on_surface():
    solver = DAESolver(Network(), [Device('select(x*x<1,100,0)')], restart_euler=False)
    result, _ = solver.step(state(.9), .002)
    assert result.t == pytest.approx(.010, abs=1e-10)
    assert result.x[0] == pytest.approx(1., abs=1e-8)
    assert len(solver.internal_events) == 1
    following, _ = solver.step(result, .0005)
    assert following.x[0] == pytest.approx(1., abs=1e-8)


@pytest.mark.parametrize('compiled', [False, True])
def test_limiter_output_and_derivative_share_frozen_mode(compiled, monkeypatch):
    dev = Device('100')
    dev.block.ir = BlockIR.from_fields('type', 'Macro.BlkDef', {
        'sStates': ['x'], 'sOutput': ['y'],
        'sAddEquat': ['x.=100', 'y=limstate(x,0,1)']})
    if not compiled:
        from power_machine.dynamics.dsl_expr import evaluate
        monkeypatch.setattr(CompiledDSL, '_eval', lambda self, expr, scope, t_s=None:
            evaluate(expr, scope, time_s=t_s, runtime=self.memory))
    solver = DAESolver(Network(), [dev], restart_euler=False)
    result, _ = solver.step(state(.96), .001)
    assert result.t == pytest.approx(.0094, abs=1e-10)
    assert result.x[0] == 1.
    following, _ = solver.step(result, .001)
    assert following.x[0] == 1.


def test_limiter_constant_variant_stops_integrator():
    dev = Device('100')
    dev.block.ir = BlockIR.from_fields('type', 'Macro.BlkDef', {
        'sStates': ['x'], 'sOutput': ['y'],
        'sAddEquat': ['x.=100', 'y=limstate_const(x,0,1)']})
    solver = DAESolver(Network(), [dev], restart_euler=False)
    result, _ = solver.step(state(.96), .001)
    following, _ = solver.step(result, .001)
    assert following.x[0] == 1.


def test_variable_state_limit_is_a_switching_surface():
    dev = Device('100')
    dev.block.ir = BlockIR.from_fields('type', 'Macro.BlkDef', {
        'sStates': ['x'], 'sOutput': ['y'],
        'sAddEquat': ['x.=100', 'y=limstate(x,0,2-x)']})
    solver = DAESolver(Network(), [dev], restart_euler=False)
    result, _ = solver.step(state(.96), .001)
    assert result.t == pytest.approx(.0094, abs=1e-10)
    following, _ = solver.step(result, .001)
    assert following.x[0] == pytest.approx(1., abs=1e-8)


def test_degenerate_algebraic_limit_has_no_internal_event():
    dev = Device('100')
    dev.block.ir = BlockIR.from_fields('type', 'Macro.BlkDef', {
        'sStates': ['x'], 'sOutput': ['y'],
        'sAddEquat': ['x.=100', 'y=lim(x,0,0)']})
    solver = DAESolver(Network(), [dev], restart_euler=False)
    result, _ = solver.step(state(-.05), .001)
    assert result.x[0] == pytest.approx(.05)
    assert solver.internal_events == []


def test_shared_ir_instances_keep_independent_modes():
    import copy
    first, second = Device(), Device()
    second.object_id = 'pf_second'
    second.block = copy.copy(first.block)
    from power_machine.dynamics.dsl_expr import DSLMemory
    second.block.memory = DSLMemory()
    second.blocks = {'block': second.block}
    solver = DAESolver(Network(), [first, second], restart_euler=False)
    start = DAEState(0., np.array([.9, 1.]), np.empty(0), np.array([2+0j]))
    result, _ = solver.step(start, .001)
    np.testing.assert_allclose(result.x, [1., 1.], atol=1e-10)
    assert len(solver.internal_events) == 1


def test_simultaneous_crossings_use_one_time():
    first, second = Device(), Device()
    second.object_id = 'pf_second'
    solver = DAESolver(Network(), [first, second], restart_euler=False)
    start = DAEState(0., np.array([.95, .95]), np.empty(0), np.array([2+0j]))
    result, _ = solver.step(start, .001)
    assert result.t == pytest.approx(.0005, abs=1e-11)
    assert len(solver.internal_events) == 2
    assert len({event['time_s'] for event in solver.internal_events}) == 1

