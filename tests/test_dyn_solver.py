"""Pruebas de integración RMS sobre una máquina clásica SMIB sintética."""

import numpy as np

from power_machine.dynamics.network import DynamicNetwork
from power_machine.dynamics.solver import DAEState, DAESolver


def model_for(*bus_ids, lines=(), loads=(), switches=()):
    return {
        "meta": {"s_base_mva": 100.0, "f_nom_hz": 50.0},
        "buses": [{"id": bus, "un_kv": 10.0, "outserv": 0} for bus in bus_ids],
        "branches": [
            {"id": name, "name": name, "class": "ElmLne", "bus_from": left,
             "bus_to": right, "outserv": 0,
             "params": {"R1": 0.0, "X1": x, "G1": 0.0, "B1": 0.0,
                        "nlnum": 1}}
            for name, left, right, x in lines
        ],
        "branches3": [], "shunts": [], "gens": [],
        "loads": list(loads), "switches": list(switches),
    }


class ClassicalMachine:
    object_id = "machine"
    relative_path = "SMIB/machine"
    bus_id = "gen"
    n_states = 2
    n_alg = 0
    discrete_state = {}

    def __init__(self, e_pu=1.05, x_pu=0.3, h_s=3.5, delta0=0.25):
        self.e_pu, self.x_pu, self.h_s = e_pu, x_pu, h_s
        self.delta0 = delta0
        self.pm_pu = 0.0

    def init(self, v_bus, i_or_s_from_lf):
        return np.array([self.delta0, 0.0])

    def current_injection(self, x, v):
        return (self.e_pu * np.exp(1j * x[0]) - v) / (1j * self.x_pu)

    def f(self, t, x, z, v):
        e = self.e_pu * np.exp(1j * x[0])
        pe_pu = (e * np.conj(self.current_injection(x, v))).real
        return np.array([2 * np.pi * 50 * x[1],
                         (self.pm_pu - pe_pu - 0.5 * x[1]) / (2 * self.h_s)])

    def g(self, t, x, z, v):
        return np.empty(0)

    def events(self):
        return ()


class InfiniteBus:
    object_id = "infinite"
    relative_path = "SMIB/infinite"
    bus_id = "grid"
    n_states = 0
    n_alg = 0
    discrete_state = {}

    def __init__(self, x_pu=0.08):
        self.x_pu = x_pu

    def init(self, v_bus, i_or_s_from_lf):
        return np.empty(0)

    def current_injection(self, x, v):
        return (1.0 - v) / (1j * self.x_pu)

    def f(self, t, x, z, v):
        return np.empty(0)

    def g(self, t, x, z, v):
        return np.empty(0)

    def events(self):
        return ()


def smib(step_s=0.02, *, three_bus=False, delta_offset=0.0):
    lines = [("l1", "gen", "mid" if three_bus else "grid", 0.25)]
    if three_bus:
        lines.append(("l2", "mid", "grid", 0.2))
    net = DynamicNetwork(model_for("gen", "mid", "grid", lines=lines)
                         if three_bus else model_for("gen", "grid", lines=lines))
    machine = ClassicalMachine()
    grid = InfiniteBus()
    gen_i, grid_i = net.index_of_bus("gen"), net.index_of_bus("grid")
    y = net.ybus.copy()
    y[gen_i, gen_i] += 1 / (1j * machine.x_pu)
    y[grid_i, grid_i] += 1 / (1j * grid.x_pu)
    source = np.zeros(len(net.node_ids), complex)
    source[gen_i] = machine.e_pu * np.exp(1j * machine.delta0) / (1j * machine.x_pu)
    source[grid_i] = 1 / (1j * grid.x_pu)
    voltage = np.linalg.solve(y, source)
    e = machine.e_pu * np.exp(1j * machine.delta0)
    machine.pm_pu = (e * np.conj(machine.current_injection(
        np.array([machine.delta0, 0.0]), voltage[gen_i]))).real
    solver = DAESolver(net, [machine, grid], step_s=step_s, max_iter=15)
    state = DAEState(0.0, np.array([machine.delta0 + delta_offset, 0.0]),
                     np.empty(0), voltage)
    state, _ = solver.solve_algebraic(state)
    return solver, state


def integrate(solver, state, end_s):
    metrics = []
    while state.t < end_s - 1e-12:
        state, measure = solver.step(state, min(solver.step_s, end_s - state.t))
        metrics.append(measure)
    return state, metrics


def test_smib_equilibrium_five_seconds():
    solver, initial = smib(step_s=0.05)
    terminal, metrics = integrate(solver, initial, 5.0)
    assert max(abs(solver._evaluate(terminal.t, terminal.x, terminal.z,
                                    terminal.v)[0])) < 1e-10
    assert np.max(abs(terminal.x - initial.x)) < 1e-9
    assert np.max(abs(terminal.v - initial.v)) < 1e-9
    assert all(m.scaled_residual <= 1 for m in metrics)


def test_smib_against_fine_step_and_order_two():
    end_s = 0.4
    solutions = {}
    for h in (0.04, 0.02, 0.01, 0.0005):
        solver, initial = smib(step_s=h, delta_offset=0.02)
        terminal, _ = integrate(solver, initial, end_s)
        solutions[h] = np.r_[terminal.x, terminal.v.real, terminal.v.imag]
    reference = solutions[0.0005]
    errors = {h: np.linalg.norm(solutions[h] - reference, ord=np.inf)
              for h in (0.04, 0.02, 0.01)}
    assert errors[0.04] < 5e-3
    assert 3.0 < errors[0.04] / errors[0.02] < 5.0
    assert 3.0 < errors[0.02] / errors[0.01] < 5.0


def test_device_algebraic_state_is_solved_with_voltage():
    class AlgebraicSource(InfiniteBus):
        object_id = "algebraic"
        n_alg = 1

        def g(self, t, x, z, v):
            return np.array([z[0] - abs(v) ** 2])

    net = DynamicNetwork(model_for("grid"))
    solver = DAESolver(net, [AlgebraicSource()])
    initial = DAEState(0.0, np.empty(0), np.array([0.5]),
                       np.array([0.8 + 0.1j]))
    state, metric = solver.solve_algebraic(initial)
    np.testing.assert_allclose(state.v, [1 + 0j], atol=1e-8)
    np.testing.assert_allclose(state.z, [1], atol=1e-8)
    assert metric.scaled_residual <= 1

