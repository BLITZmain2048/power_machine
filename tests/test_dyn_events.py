"""Eventos RMS: falla, despeje, apertura y cambio de admitancia de carga."""

import numpy as np

from power_machine.dynamics.events import Event, EventTimeline
from power_machine.dynamics.network import DynamicNetwork
from power_machine.dynamics.solver import DAEState, DAESolver

from test_dyn_solver import InfiniteBus, model_for, smib


def test_fault_clear_three_bus_exact_landing_and_duplicate_samples():
    solver, initial = smib(step_s=0.02, three_bus=True)
    original_y = solver.network.ybus.copy()
    timeline = EventTimeline([Event(0.037, "fault_apply", "mid", 0.12j),
                              Event(0.083, "fault_clear", "mid")])
    result = timeline.run(solver, initial, 0.16)
    assert len(result.discontinuities) == 2
    assert [d["time_s"] for d in result.discontinuities] == [0.037, 0.083]
    for discontinuity in result.discontinuities:
        before = result.samples[discontinuity["before_index"]]
        after = result.samples[discontinuity["after_index"]]
        assert before.t == after.t == discontinuity["time_s"]
        np.testing.assert_array_equal(before.x, after.x)
    fault = result.discontinuities[0]
    before = result.samples[fault["before_index"]]
    after = result.samples[fault["after_index"]]
    mid = solver.network.index_of_bus("mid")
    assert abs(after.v[mid]) < abs(before.v[mid])
    np.testing.assert_allclose(solver.network.ybus, original_y)
    assert any(m.method == "euler" for m in result.metrics)
    assert result.samples[-1].t == 0.16


def test_branch_open_and_reclose_rebuilds_network():
    solver, initial = smib(step_s=0.02, three_bus=True)
    original_y = solver.network.ybus.copy()
    timeline = EventTimeline([Event(0.031, "branch_open", "l2"),
                              Event(0.071, "branch_close", "l2")])
    result = timeline.run(solver, initial, 0.1)
    assert len(result.discontinuities) == 2
    np.testing.assert_allclose(solver.network.ybus, original_y)
    assert "l2" not in solver.network.open_branches


def test_load_factor_changes_constant_admittance_from_lf_voltage():
    load = {"id": "load", "class": "ElmLod", "bus": "grid", "outserv": 0,
            "params": {"plini": 20.0, "qlini": 10.0, "scale0": 1.0}}
    net = DynamicNetwork(model_for("grid", loads=[load]), {"grid": 1.0 + 0j})
    np.testing.assert_allclose(net.ybus[0, 0], 0.2 - 0.1j)
    grid = InfiniteBus()
    solver = DAESolver(net, [grid], step_s=0.02)
    initial, _ = solver.solve_algebraic(DAEState(0.0, np.empty(0), np.empty(0),
                                                  np.array([1.0 + 0j])))
    result = EventTimeline([Event(0.03, "load_factor", "load", 1.5)]).run(
        solver, initial, 0.06)
    np.testing.assert_allclose(net.ybus[0, 0], 0.3 - 0.15j)
    d = result.discontinuities[0]
    before, after = (result.samples[d[key]] for key in ("before_index", "after_index"))
    assert before.t == after.t == 0.03
    assert abs(after.v[0] - before.v[0]) > 1e-5


def test_coupler_open_changes_node_count_and_preserves_state():
    class SecondSource(InfiniteBus):
        object_id = "second"
        relative_path = "test/second"
        bus_id = "second"

    coupler = {"id": "coup", "class": "ElmCoup", "bus1": "grid",
               "bus2": "second", "closed": True, "outserv": 0}
    net = DynamicNetwork(model_for("grid", "second", switches=[coupler]))
    assert len(net.node_ids) == 1
    solver = DAESolver(net, [InfiniteBus(), SecondSource()], step_s=0.02)
    initial, _ = solver.solve_algebraic(DAEState(
        0.0, np.empty(0), np.empty(0), np.array([1 + 0j])))
    result = EventTimeline([Event(0.03, "branch_open", "coup")]).run(
        solver, initial, 0.06)
    assert len(net.node_ids) == 2
    d = result.discontinuities[0]
    before, after = (result.samples[d[key]] for key in ("before_index", "after_index"))
    assert before.v.size == 1 and after.v.size == 2
    np.testing.assert_array_equal(before.x, after.x)

