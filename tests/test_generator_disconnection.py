"""Invariantes DAE de desconexión, con red y dispositivos mínimos."""
from types import SimpleNamespace

import numpy as np
import pytest

from power_machine.dynamics.solver import DAESolver, DAEState


class Device:
    n_states = n_alg = 1
    bus_id = 'bus'

    def __init__(self, identifier):
        self.object_id = identifier

    def f(self, t, x, z, v):
        return np.array([v.real-x[0]])

    def g(self, t, x, z, v):
        return z-v.real

    def current_injection(self, x, v):
        return complex(x[0])


def make_solver():
    network = SimpleNamespace(node_ids=['bus'], ybus=np.array([[2.+0j]]),
                              index_of_bus=lambda bid: 0)
    solver = DAESolver(network, [Device('keep'), Device('trip')])
    state = DAEState(0., np.ones(2), np.ones(2), np.ones(1, complex))
    return solver, state


def test_trip_zero_current_frozen_states_and_regular_algebraic():
    solver, before = make_solver()
    slices = tuple(solver.x_slices), tuple(solver.z_slices)
    solver.disconnect_device('trip')
    after, metric = solver.solve_algebraic(before)
    assert metric.scaled_residual <= 1
    assert after.v[0] == pytest.approx(.5)
    assert after.z[1] == 0
    assert np.array_equal(after.x, before.x)
    f, g, net = solver._evaluate(after.t, after.x, after.z, after.v)
    assert f[1] == 0 and f[0] < 0
    assert np.max(abs(net)) < 1e-8
    for _ in range(3):
        after, metric = solver.step(after)
        assert metric.scaled_residual <= 1
        assert after.x[1] == before.x[1]
        assert after.z[1] == 0
        assert np.isfinite(np.r_[after.x, after.z, after.v]).all()
    assert slices == (tuple(solver.x_slices), tuple(solver.z_slices))
    assert solver.in_service == {'keep': True, 'trip': False}


def test_reference_and_unknown_rejected_without_mutation():
    solver, _ = make_solver()
    solver.reference_speed_index = 1
    with pytest.raises(ValueError, match='angular reference'):
        solver.disconnect_device('trip')
    with pytest.raises(ValueError, match='Unknown'):
        solver.disconnect_device('missing')
    assert all(solver.in_service.values())


def test_multiport_algebraic_block_is_identity_when_disconnected():
    solver, state = make_solver()
    dev = solver.devices[1]
    dev.read_bus_ids = dev.injection_bus_ids = ('bus',)
    dev.read_branch_ids = ()
    dev.current_injections_multi = lambda *args: {'bus': 1.+0j}
    dev.f_multi = lambda t, x, z, ports: np.array([1.])
    dev.g_multi = lambda t, x, z, ports: z-2
    solver = DAESolver(solver.network, solver.devices)
    solver.disconnect_device('trip')
    after, _ = solver.solve_algebraic(state)
    after, _ = solver.step(after)
    assert after.z[1] == 0
    assert after.x[1] == state.x[1]
    assert np.isfinite(after.v).all()


def test_trip_last_generator_deenergizes_island():
    from power_machine.dynamics.network import DynamicNetwork
    from power_machine.dynamics.events import Event, EventTimeline
    model = {'meta': {'s_base_mva': 100},
             'buses': [{'id': 'bus', 'un_kv': 220, 'outserv': 0}],
             'gens': [{'id': 'trip', 'bus': 'bus', 'outserv': 0, 'ref': False}],
             'branches': [], 'branches3': [], 'loads': [], 'shunts': [], 'switches': []}
    network = DynamicNetwork(model, {'bus': 1.+0j})
    solver = DAESolver(network, [Device('trip')])
    initial = DAEState(0., np.ones(1), np.ones(1), np.ones(1, complex))
    trajectory = EventTimeline([Event(0., 'gen_trip', 'trip')]).run(solver, initial, .003)
    final = trajectory.samples[-1]
    assert np.array_equal(final.x, initial.x)
    assert np.array_equal(final.v, np.zeros(1, complex))
    assert final.z[0] == 0
    assert network.deenergized_bus_ids == {'bus'}

