"""Identidad de canales físicos y opción heredada por defecto."""
import inspect
import json
from types import SimpleNamespace

import numpy as np
import pytest

from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, WORKSPACE, build_rms_case
from power_machine.pfcompare.rms_replay import load_reference
from power_machine.pfcompare import rms_network as original
from power_machine.pfcompare import frequency_observers as frequency_observers


def test_default_estimator_disabled():
    assert inspect.signature(frequency_observers.simulate).parameters["freq_estimator"].default == "accepted_step"
    assert inspect.signature(frequency_observers.run_trial).parameters["freq_estimator"].default == "accepted_step"


def test_original_simulation_and_h_option_bit_identical():
    if not DEFAULT_PF_DIR.is_dir():
        pytest.skip("Referencia offline ausente")
    _, _, channels = load_reference(DEFAULT_PF_DIR/"E39-DIN-03")
    rms = [build_rms_case(avr_profile="pf_identificado") for _ in range(3)]
    t0, p0, d0 = original.simulate(rms[0], "E39-DIN-03", channels, stop_s=.02)
    t1, p1, d1 = frequency_observers.simulate(rms[1], "E39-DIN-03", channels, stop_s=.02)
    t2, p2, d2 = frequency_observers.simulate(rms[2], "E39-DIN-03", channels, stop_s=.02, freq_estimator="nominal_grid")
    assert t0.tobytes() == t1.tobytes() == t2.tobytes()
    for ch in channels:
        key = ch["signal"]
        assert p0[key].tobytes() == p1[key].tobytes()
        if ch["variable"] != "m:fehz":
            assert p0[key].tobytes() == p2[key].tobytes()
    assert len([ch for ch in channels if ch["variable"] != "m:fehz"]) == 27
    assert d0["steps"] == d1["steps"] == d2["steps"]
    assert d0["events"] == d1["events"] == d2["events"]


def test_destination_guard_and_no_overwrite(tmp_path, monkeypatch):
    monkeypatch.setenv("POWER_MACHINE_OUTPUT_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="carpeta de trabajo"):
        frequency_observers.run_trial(DEFAULT_PF_DIR, WORKSPACE.parent, "E39-DIN-01")
    target = tmp_path/"E39-DIN-01"/"frequency_observers"
    target.mkdir(parents=True)
    with pytest.raises(FileExistsError):
        frequency_observers.run_trial(DEFAULT_PF_DIR, tmp_path, "E39-DIN-01")
    with pytest.raises(ValueError, match="Estimador"):
        frequency_observers.run_trial(DEFAULT_PF_DIR, tmp_path, "E39-DIN-01", freq_estimator="fit")


def test_completed_campaign_identity_if_present():
    for trial in original.CASES:
        folder = original.OUTPUT_DIR/trial/"frequency_observers"
        if not (folder/"identity_audit.json").exists():
            continue
        audit = json.loads((folder/"identity_audit.json").read_text(encoding="utf-8"))
        assert audit["disabled_deterministic_artifacts_identical"]
        assert audit["physical_channels_float64_identical"]
        assert audit["physical_channels_per_variant"] == 27
        assert audit["full_horizon_s"] == 10.
        assert audit["samples"] == 1001


def test_adaptive_steps_and_both_event_sides_keep_27_channels(monkeypatch):
    """Calendario con un aterrizaje interior y salto, física no constante."""
    class State:
        def __init__(self, t=0., phase=0.):
            self.t = t
            self.x = np.array([1.+t*.001, phase])
            self.z = np.zeros(0)
            self.v = np.array([np.exp(1j*(phase+t*t))])

        def copy(self):
            return State(self.t, self.x[1])

    class Solver:
        step_s = .001
        reference_speed_index = 0
        bolted_bus_ids = set()
        projections = []
        limit_events = []

        def step(self, state, dt):
            target = state.t+dt
            if state.t < .004831 < target:
                target = .004831
            return State(target, state.x[1]), SimpleNamespace(
                iterations=2, scaled_residual=0., method="trapezoidal")

        def _evaluate(self, *args):
            return None, None, np.zeros(1)

    class RMS:
        def __init__(self):
            self.initial = State()
            self.solver = Solver()
            self.network = SimpleNamespace(bus_voltages=lambda v: {"b4": v[0], "b16": .9*v[0]})
            fields = {"time": {"value": .01}, "outserv": {"value": 0}}
            self.case = SimpleNamespace(inventory={"trials": {"toy": {"events": [{"fields": fields}]}}})

        def apply_events(self, trial, event_s, state):
            return State(event_s, .5), [{"time_s": event_s}]

    channels = [{"signal": f"physical_{i}", "variable": "m:u", "object_id": "b4"} for i in range(27)]
    channels += [{"signal": "B4_frequency_Hz", "variable": "m:fehz", "object_id": "b4"},
                 {"signal": "B16_frequency_Hz", "variable": "m:fehz", "object_id": "b16"}]

    def sample(rms, state, channels, frequency):
        return {ch["signal"]: (frequency[ch["object_id"]] if ch["variable"] == "m:fehz"
                                else state.x[0]+state.x[1]+i*.01)
                for i, ch in enumerate(channels)}

    monkeypatch.setattr(original, "sample_channels", sample)
    _, base, old_diag = original.simulate(RMS(), "toy", channels, stop_s=.03)
    _, disabled, disabled_diag = frequency_observers.simulate(RMS(), "toy", channels, stop_s=.03)
    _, enabled, enabled_diag = frequency_observers.simulate(RMS(), "toy", channels, stop_s=.03, freq_estimator="nominal_grid")
    assert old_diag["steps"] == disabled_diag["steps"] == enabled_diag["steps"]
    assert old_diag["events"] == disabled_diag["events"] == enabled_diag["events"]
    for key in base:
        assert base[key].tobytes() == disabled[key].tobytes()
        if key.startswith("physical"):
            assert base[key].tobytes() == enabled[key].tobytes()
    assert base["B4_frequency_Hz"].tobytes() != enabled["B4_frequency_Hz"].tobytes()
    # La muestra de evento sigue siendo t-, el nuevo valor aparece después.
    assert enabled["physical_0"][1] < enabled["physical_0"][2]-.4


import pytest
pytestmark = [pytest.mark.pfdata]
