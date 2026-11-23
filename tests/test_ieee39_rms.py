"""Portable numerical regression of IEEE 39 classical RMS and CCT."""

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from power_machine.benchmarks.ieee39_dynamics import ClassicalSystem, critical_clearing_time, simulate
from power_machine.resources import required_readonly_path


RMS_100_MS_SHA256 = "876642fee7df93d61607620b53ae3690acf0d17437ae7c04a3e33441a5c52123"
CCT_B16_SHA256 = "5d1ec1898125563b813539473379c5a9c7eb321a328057ff1adc2133c1323121"


def _inputs():
    model = json.loads(required_readonly_path("cases", "ieee39", "model.json").read_text(encoding="utf-8"))
    data = json.loads(required_readonly_path("cases", "ieee39", "dynamic_data.json").read_text(encoding="utf-8"))
    return model, data


def test_rms_ieee39_fault_100_ms_matches_reference(record_property):
    model, data = _inputs()
    run = simulate(ClassicalSystem(model, data), "E39-DIN-01", end_s=2.0, clearing_s=0.1, step_s=0.001)
    values = np.concatenate([np.r_[sample.time_s, sample.state.ravel(), sample.voltage_pu.real,
                                   sample.voltage_pu.imag, sample.generator_power_pu.real,
                                   sample.generator_power_pu.imag, sample.bus_frequency_hz,
                                   sample.coi_frequency_hz] for sample in run.samples])
    assert values.shape == (318477,)
    record_property("reference_sha256", RMS_100_MS_SHA256)
    record_property("observed_sha256", hashlib.sha256(values.tobytes()).hexdigest())
    with np.load(Path(__file__).parent / "data/ieee39_classical_reference.npz", allow_pickle=False) as reference:
        np.testing.assert_allclose(values, reference["rms"], rtol=1e-9, atol=1e-12)


def test_cct_ieee39_b16_matches_reference(record_property):
    model, data = _inputs()
    result = critical_clearing_time(model, data, end_s=10.0, step_s=0.002, tolerance_s=0.002)
    values = np.asarray([[trial["clearing_s"], trial["stable_finite_horizon"],
                          trial["max_pairwise_rotor_spread_deg"]] for trial in result["trials"]])
    assert result["status"] == "informativo"
    assert result["estimate_s"] == pytest.approx(0.12294921875000003)
    assert values.shape == (10, 3)
    record_property("reference_sha256", CCT_B16_SHA256)
    record_property("observed_sha256", hashlib.sha256(values.tobytes()).hexdigest())
    with np.load(Path(__file__).parent / "data/ieee39_classical_reference.npz", allow_pickle=False) as reference:
        np.testing.assert_allclose(values, reference["cct"], rtol=1e-9, atol=1e-12)
