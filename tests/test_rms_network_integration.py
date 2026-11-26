"""Pruebas dirigidas native RMS, sin ejecutar PF ni depender de su API."""
from collections import Counter
import copy
import json

import numpy as np
import pytest

from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, WORKSPACE, build_rms_case
from power_machine.dynamics.native_registry import NativeControllerCatalog
from power_machine.pfcompare.rms_replay import load_reference
from power_machine.pfcompare.rms_network import compare_channel, fresh_output, sample_channels, simulate
from power_machine.pfcompare.modal_network import native_matrix, state_transform


@pytest.fixture
def rms():
    if not DEFAULT_PF_DIR.is_dir():
        pytest.skip("Referencia offline IEEE39 ausente")
    return build_rms_case()


def test_full_count_identity_references_and_equilibrium(rms):
    assert Counter(r["kind"] for r in rms.manifest) == dict(machine=59, avr=36, governor=52, load=19)
    assert rms.equilibrium["max_derivative_per_s"] < 1e-8
    assert rms.equilibrium["max_kcl_pu"] < 1e-8
    assert all(r["object_id"] and r["relative_path"] for r in rms.manifest)
    assert next(r for r in rms.manifest if r["variable"] == "gate_speed_pu_per_s")["unit"] == "p.u./s"
    assert len(rms.catalog.coverage) == 27
    assert sum(r["outserv"] == 1 for r in rms.catalog.coverage) == 9
    assert all("pss_CONV" not in type(d).__name__ for d in rms.devices)
    for d in rms.devices:
        if hasattr(d, "machine") and d.governor:
            assert d.gov_inputs["wref"] == 1.
            assert d.governor.pgn_mw == pytest.approx(d.machine.sgn_mva*d.machine.typ["cosn"])
            assert np.isfinite(d.avr_inputs["Vref"])


@pytest.mark.parametrize("stage,count", [("loads", 78), ("avr", 114)])
def test_staged_equilibrium(stage, count):
    if not DEFAULT_PF_DIR.exists():
        pytest.skip("Referencia offline ausente")
    model = build_rms_case(stage=stage)
    assert model.solver.nx == count
    assert model.equilibrium["max_derivative_per_s"] < 1e-8


def test_load_lf_stamp_not_scaled_twice_and_step(rms):
    for load in rms.model["loads"]:
        d = rms.loads[load["id"]]
        i = next(i for i, dev in enumerate(rms.devices) if dev.object_id == d.object_id)
        x = rms.initial.x[rms.solver.x_slices[i]]
        v = rms.case.voltages[d.bus_id]
        s = complex(load["p_mw"], load["q_mvar"])
        assert abs(d.current_injection(x, v)) < 1e-12
        assert d.consumed_power_mva(v, x) == pytest.approx(s)
        d.set_step_percent(dp_percent=10., dq_percent=0.)
        assert d.consumed_power_mva(v, x) == pytest.approx(complex(1.1*s.real, s.imag))


def test_catalog_unknown_active_rejected_outserv_ignored(rms):
    inventory = copy.deepcopy(rms.case.inventory)
    active = next(r for r in inventory["controllers"] if r["fields"]["outserv"]["value"] == 0)
    active["fields"]["typ_id"]["value"] = "unknown.BlkDef"
    comp = next(r for r in inventory["composites"] if active["path"] in r["fields"]["pelm"]["value"])
    machine_path = comp["fields"]["pelm"]["value"][0]
    machine = rms.case.machine_by_path[machine_path]
    with pytest.raises(ValueError, match="Unregistered"):
        NativeControllerCatalog(inventory).bind(machine_path, machine)
    active["fields"]["outserv"]["value"] = 1
    NativeControllerCatalog(inventory).bind(machine_path, machine)


def test_fault_exact_voltage_and_jacobian_row(rms):
    state = rms.initial.copy()
    state.t = 1.
    state, applied = rms.apply_events("E39-DIN-01", 1., state)
    assert len(applied) == 1
    bid = next(iter(rms.solver.bolted_bus_ids))
    i = rms.network.index_of_bus(bid)
    assert abs(state.v[i]) < 1e-12
    assert not rms.network.faults  # Sin impedancia arbitraria pequeña.
    _, _, channels = load_reference(DEFAULT_PF_DIR/"E39-DIN-01")
    outputs = sample_channels(rms, state, channels, {bid: 60. for bid in rms.case.voltages})
    assert np.isnan(outputs["B16_angle_deg"])
    assert np.isnan(outputs["B16_frequency_Hz"])
    jac = rms.solver._local_jacobian(state.t, state.x, state.z, state.v)
    assert np.count_nonzero(jac[i]) == 1
    assert jac[i, i] == 1.
    state.t = 1.1
    state, applied = rms.apply_events("E39-DIN-01", 1.1, state)
    assert len(applied) == 3
    assert not rms.solver.bolted_bus_ids
    assert len(rms.network.open_branches) == 1
    assert abs(state.v[i]) > .1


def test_trip_open_circuit_keeps_physical_states(rms):
    state = rms.initial.copy()
    state.t = 1.
    state, applied = rms.apply_events("E39-DIN-02", 1., state)
    dev = next(d for d in rms.devices if getattr(d, "detached", False))
    assert rms.solver.in_service[dev.object_id]
    xs = rms.solver.x_slices[rms.devices.index(dev)]
    x = state.x[xs]
    v = state.v[rms.network.index_of_bus(dev.bus_id)]
    terminal = dev.terminal_voltage(x, v)
    assert abs(dev.machine.current_injection(dev.parts(x)[0], terminal)) < 1e-11
    assert dev.current_injection(x, v) == 0j
    dx = dev.f(1., x, state.z, v)
    assert dx[dev.machine.n_states-2] > 0.
    assert dx[dev.machine.n_states-1] == 0.


def test_fault_crossing_avr_limit_converges(rms):
    state = rms.initial.copy()
    state.t = 1.
    state, _ = rms.apply_events("E39-DIN-01", 1., state)
    while state.t < 1.015-1e-12:
        state, _ = rms.solver.step(state, min(.001, 1.015-state.t))
    for d, xs in zip(rms.devices, rms.solver.x_slices):
        if getattr(d, "avr", None):
            xa = d.parts(state.x[xs])[1]
            assert d.avr.parameters["Vrmin"] <= xa[1] <= d.avr.parameters["Vrmax"]


def test_short_no_event_independent_prediction(rms):
    _, refs, channels = load_reference(DEFAULT_PF_DIR/"E39-DIN-03")
    t, pred, diag = simulate(rms, "E39-DIN-03", channels, stop_s=.03)
    assert len(pred) == 29 and len(t) == 4
    assert len(diag["steps"]) == 30
    for key, y in pred.items():
        np.testing.assert_allclose(y, y[0], atol=1e-9)
        assert y[0] == pytest.approx(refs[key][0], abs=1e-5)


def test_modal_transform_count_and_full_matrix(rms):
    pf_a, states, frozen = native_matrix(DEFAULT_PF_DIR)
    transform, mapping = state_transform(rms, states)
    assert transform.shape == (166, 166)
    assert len(mapping) == 166
    assert len(frozen) == 10
    assert pf_a.shape == transform.shape
    assert np.count_nonzero(transform) == 166


def test_closed_linearization_against_independent_algebraic_stencil(rms):
    a, diag = rms.linearize()
    half, _ = rms.linearize(rel_step=.5e-6)
    np.testing.assert_allclose(a, half, rtol=1e-6, atol=2e-6)
    assert diag["elimination_residual"] < 1e-9
    reference = rms.solver.reference_speed_index
    phi_ref = reference+1
    np.testing.assert_allclose(a[phi_ref], 0., atol=1e-8)
    for j in (0, reference, 23, 160):
        states = []
        for sign in (1., -1.):
            state = rms.initial.copy()
            state.x[j] += sign*1e-5
            state, _ = rms.solver.solve_algebraic(state)
            states.append(rms.solver._evaluate(0., state.x, state.z, state.v)[0])
        np.testing.assert_allclose(a[:, j], (states[0]-states[1])/2e-5, atol=1e-4, rtol=2e-5)


def test_metrics_declared_thresholds_peaks_and_missing():
    t = np.linspace(0., 10., 1001)
    ref = np.cos(2*np.pi*(t-1))
    channel = dict(signal="synthetic", unit="p.u.", object_id="id", relative_path="path")
    exact = compare_channel(t, ref, ref, channel)
    assert exact["max_pass"] and exact["peak_pass"]
    fail = compare_channel(t, ref, ref+.05, channel)
    assert not fail["max_pass"]
    missing = ref.copy()
    missing[10] = np.nan
    assert not compare_channel(t, missing, ref, channel)["complete_samples"]
    flat = compare_channel(t, np.ones(len(t)), np.ones(len(t)), channel)
    assert flat["max_pass"] is None and flat["peak_pass"] is None
    assert exact["max_tolerance"] == .02 and exact["peak_tolerance"] == .01


def test_output_requires_the_comparator_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("POWER_MACHINE_OUTPUT_DIR", str(tmp_path / "workspace"))
    """El comparador no acepta destinos arbitrarios ni archivos históricos."""
    with pytest.raises(ValueError, match="working directory"):
        fresh_output(tmp_path, "synthetic")


import pytest
pytestmark = [pytest.mark.pfdata]
