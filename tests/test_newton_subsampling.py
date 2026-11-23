"""Equivalencia del Newton local y salida submuestreada de local Newton."""

import csv
from pathlib import Path

import numpy as np
import pytest

from power_machine.dynamics.interfaces import finite_difference_jacobian
from power_machine.dynamics.runner import RMSRunner
from power_machine.dynamics.solver import DAEState, DAESolver, SolverConvergenceError
from test_dyn_solver import smib


ROOT = Path(__file__).resolve().parents[1]


def test_local_jacobian_evaluates_only_the_affected_device():
    class Network:
        node_ids = ("bus",)
        ybus = np.array([[1+2j]])

        @staticmethod
        def index_of_bus(bus_id):
            assert bus_id == "bus"
            return 0

    class Device:
        bus_id = "bus"
        n_states = 1
        n_alg = 0
        calls = 0

        def __init__(self, index):
            self.object_id = str(index)

        def f(self, t, x, z, v):
            self.calls += 1
            return np.array([-x[0]+v.real])

        def g(self, t, x, z, v):
            return np.empty(0)

        def current_injection(self, x, v):
            return complex(x[0], -x[0])+v

    devices = [Device(i) for i in range(20)]
    solver = DAESolver(Network(), devices)
    old = DAEState(0.0, np.ones(20), np.empty(0), np.array([1+0j]))
    jac = solver._local_jacobian(0.001, old.x, old.z, old.v,
                                 old=old, h=0.001, f_old=np.zeros(20),
                                 method="trapezoidal")
    assert jac.shape == (22, 22)
    assert sum(device.calls for device in devices) == 20*6
    np.testing.assert_allclose(jac[:20, :20], np.eye(20)*1.0005, atol=1e-9)


class GlobalDifferenceSolver(DAESolver):
    """Newton original: diferencias centrales del residuo DAE completo."""

    def _newton(self, residual, guess, scale, jacobian, *, cache_key=None):
        u = np.asarray(guess, dtype=float).copy()
        last_alpha = 1.0
        for iteration in range(self.max_iter + 1):
            r = residual(u)
            error = float(np.max(abs(r/scale))) if r.size else 0.0
            if error <= 1.0:
                return u, iteration, error, last_alpha
            if iteration == self.max_iter:
                break
            jac = finite_difference_jacobian(
                lambda values: residual(values)/scale, u,
                rel_step=self.fd_rel_step)
            delta = np.linalg.solve(jac, -r/scale)
            alpha = 1.0
            for _ in range(20):
                candidate = u+alpha*delta
                trial = residual(candidate)
                if float(np.max(abs(trial/scale))) < error:
                    u, last_alpha = candidate, alpha
                    break
                alpha *= 0.5
            else:
                raise SolverConvergenceError("Referencia sin descenso")
        raise SolverConvergenceError("Referencia sin convergencia")


def test_trajectory_matches_original_newton_for_02_s():
    fast, state_fast = smib(step_s=0.002, delta_offset=0.02)
    baseline, state_baseline = smib(step_s=0.002, delta_offset=0.02)
    baseline = GlobalDifferenceSolver(baseline.network, baseline.devices,
                                      step_s=0.002, max_iter=15)
    max_state_difference = 0.0
    max_voltage_difference_pu = 0.0
    for _ in range(100):
        state_fast, _ = fast.step(state_fast)
        state_baseline, _ = baseline.step(state_baseline)
        max_state_difference = max(max_state_difference,
                                   float(np.max(abs(state_fast.x-state_baseline.x))))
        max_voltage_difference_pu = max(
            max_voltage_difference_pu,
            float(np.max(abs(state_fast.v-state_baseline.v))))
    assert max_state_difference < 1e-6
    assert max_voltage_difference_pu < 1e-6


@pytest.mark.skip(reason="Requiere una maniobra dinámica externa no incluida en IEEE 39")
def test_output_decimation_keeps_both_sides_of_event(tmp_path):
    runner = RMSRunner(ROOT, "LOAD_P10")
    report = runner.run(end_s=1.001, output=tmp_path / "p10", output_every=10)
    assert report["failure"] is None
    assert report["output_every"] == 10
    with (tmp_path / "p10/signals.csv").open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    times = [float(row["time|b:tnow|s"]) for row in rows]
    assert len(rows) < 120
    assert abs(times[0]+0.1) < 1e-12
    assert sum(abs(t-1.0) < 1e-12 for t in times) == 2
    assert abs(times[-1]-1.001) < 1e-12

