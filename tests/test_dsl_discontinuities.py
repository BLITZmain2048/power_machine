import pytest
"""Discontinuidades DSL aisladas, tiempos [s] y estados sin conversión de unidad."""
import numpy as np

from power_machine.dsl_editor import new_draft, PreviewCase
from power_machine.dsl_editor.preview import _simulate_local
from power_machine.dsl_editor.workspace import parameter, port


def draft(equations, states=("x",), outputs=("u", "y"), params=None):
    params = params or {}
    return new_draft({"sInput": [], "sOutput": list(outputs), "sStates": list(states),
                      "sParams": list(params), "sAddEquat": equations}, params,
                     [port(n, "output", None, port_id=n) for n in outputs])


@pytest.mark.parametrize("h_s", [.001, .0005, .007])
def test_time_landing_restart_and_both_sides(h_s):
    model = draft(["inc(x)=0", "u=select(time()>=0.0103,1,0)",
                   "x.=u", "y=x"])
    result = _simulate_local(model, PreviewCase((0., .01, .0103, .011, .03), {}, {}), {"step_s": h_s})
    assert result["status"] == "completed", result
    p = result["payload"]
    assert p["states"]["x"] == pytest.approx([0., 0., 0., .0007, .0197], abs=1e-10)
    event, = p["time_events"]
    assert event["time_s"] == .0103
    assert event["before_outputs"] == {"u": 0., "y": 0.}
    assert event["after_outputs"] == {"u": 1., "y": 0.}
    assert "euler" in p["integration_methods"]


@pytest.mark.parametrize("h_s", [.001, .0005])
def test_saturation_localization_and_release_publication(h_s):
    model = draft(["inc(x)=0", "u=select(time()<0.03,1,-1)",
                   "x.=u", "y=limstate_const(x,0,0.0103)"])
    result = _simulate_local(model, PreviewCase((0., .01, .0103, .03, .031, .032), {}, {}), {"step_s": h_s})
    assert result["status"] == "completed", result
    p = result["payload"]
    assert p["outputs"]["y"] == pytest.approx(p["states"]["x"], abs=1e-12)
    assert p["states"]["x"][-2:] == pytest.approx([.0093, .0083], abs=1e-10)
    event = next(e for e in p["internal_events"] if e.get("side") == "upper")
    assert event["time_s"] == pytest.approx(.0103, abs=1e-10)
    assert "before_outputs" in event and "after_outputs" in event


def test_affine_time_parameter_alias_and_simultaneous_changes():
    model = draft(["inc(x)=0", "clock=2*time()", "u=select(clock<T,0,1)",
                   "x.=u", "y=select(time()>T/2,2,0)"],
                  params={"T": parameter(.0206, "s")})
    p = _simulate_local(model, PreviewCase((0., .01, .0103, .02), {}, {}), {"step_s": .003})["payload"]
    event, = p["time_events"]
    assert len(event["guards"]) == 2
    assert event["before_outputs"]["y"] == 0. and event["after_outputs"]["y"] == 2.
    assert p["states"]["x"][-1] == pytest.approx(.0097, abs=1e-10)


def test_smooth_preview_preserves_original_solver_bitwise():
    from power_machine.dsl_editor import template_draft
    from power_machine.dsl_editor.compiler_adapter import compile_artifact
    from power_machine.dsl_editor.preview import _IsolatedNetwork, _PreviewDevice
    from power_machine.dynamics.solver import DAESolver, DAEState
    model = template_draft("first_order")
    case = PreviewCase(tuple(np.arange(21)*.001), {"u": (1.,)*21}, {})
    p = _simulate_local(model, case, {"step_s": .001})["payload"]
    block = compile_artifact(model)
    x, _ = block.initialize({"u": 1.})
    block.accept_step(0., x, {"u": 1.})
    solver = DAESolver(_IsolatedNetwork(), [_PreviewDevice(block, {"u": 1.})],
                       step_s=.001, atol=1e-12, rtol=1e-10, restart_euler=False)
    state = DAEState(0., x, np.empty(0), np.zeros(1, complex))
    reference = [float(x[0])]
    for target in case.time_s[1:]:
        while state.t < target-1e-12:
            state, _ = solver.step(state, min(.001, target-state.t))
            block.accept_step(state.t, state.x, {"u": 1.})
        reference.append(float(state.x[0]))
    np.testing.assert_array_equal(p["states"]["x"], reference)
    assert p["integration_methods"] == ["trapezoidal"]


def test_flag_threshold_like_severe_fault_finishes():
    model = draft(["inc(x)=0", "x.=select(x<1,100,0)", "y=x"], outputs=("y",))
    p = _simulate_local(model, PreviewCase((0., .009, .01, .011, .02), {}, {}), {"step_s": .001})
    assert p["status"] == "completed", p
    assert p["payload"]["states"]["x"] == pytest.approx([0., .9, 1., 1., 1.], abs=1e-10)


def test_state_guard_publishes_both_sides_at_localized_crossing():
    model = draft(["inc(x)=0", "x.=select(x<1,100,0)",
                   "y=select(x<1,100,0)"], outputs=("y",))
    result = _simulate_local(model, PreviewCase((0., .007, .02), {}, {}), {"step_s": .007})
    assert result["status"] == "completed", result
    events = result["payload"]["internal_events"]
    assert events and all(e["time_s"] == pytest.approx(.01, abs=1e-10) for e in events)
    event = next(e for e in events if e["target"] == "y")
    assert event["before_outputs"]["y"] == 100.
    assert event["after_outputs"]["y"] == 0.
    assert event["before_states"] == event["after_states"] == {"x": 1.}


@pytest.mark.parametrize("initial,derivative,guard,left,right", [
    (0., 100., "x<=1", 100., 0.),
    (2., -100., "x<1", 0., 100.),
])
def test_output_at_state_crossing_uses_directional_right_limit(initial, derivative, guard, left, right):
    model = draft([f"inc(x)={initial}", f"x.={derivative}",
                   f"u=select({guard},100,0)", f"y=select({guard},100,0)"])
    result = _simulate_local(model, PreviewCase((0., .009, .01, .011), {}, {}), {"step_s": .001})
    assert result["status"] == "completed", result
    p = result["payload"]
    assert p["outputs"]["y"][2:] == [right, right]
    for event in p["internal_events"]:
        assert event["before_outputs"] == {"u": left, "y": left}
        assert event["after_outputs"] == {"u": right, "y": right}


def test_non_affine_time_guard_is_explicitly_unsupported():
    model = draft(["inc(x)=0", "u=select(sqr(time())<0.01,0,1)", "x.=u", "y=x"])
    result = _simulate_local(model, PreviewCase((0., .2), {}, {}), {"step_s": .001})
    assert result["status"] == "unsupported"
    assert result["diagnostics"][0]["code"] == "unsupported_time_guard"


def test_time_calendar_and_interpreter_agree_for_strict_guards():
    model = draft(["inc(x)=0", "u=select(time()>0.01,1,0)", "x.=u", "y=x"])
    case = PreviewCase((0., .01-2e-13, .011, .02), {}, {})
    compiled = _simulate_local(model, case, {"step_s": .001})
    interpreted = _simulate_local(model, case, {"step_s": .001}, interpreted=True)
    assert compiled["status"] == interpreted["status"] == "completed"
    for group in ("states", "outputs", "time_events"):
        assert compiled["payload"][group] == interpreted["payload"][group]
    event, = compiled["payload"]["time_events"]
    assert event["time_s"] == .01
    assert compiled["payload"]["states"]["x"][-1] == pytest.approx(.01, abs=1e-10)


@pytest.mark.refdata
def test_ref_bench_accepts_both_steps_without_changing_gate(tmp_path):
    from power_machine.refcompare.dsl import DEFAULT_REF_DIR, compare_bench
    if not (DEFAULT_REF_DIR / "E39-DSL-01/trajectories_full_precision.csv").is_file():
        pytest.skip("Referencia externa offline ausente")
    result = compare_bench(output_dir=tmp_path)
    assert result["options"]["normalized_limit"] == .001
    assert result["options"]["step_s"] == .001
    assert result["accepted_excluding_exact_discontinuities"]
    assert result["accepted_half_step_excluding_exact_discontinuities"]
    assert len(result["metrics"]) == len(result["half_step_metrics"]) == 9
    assert not result["accepted_all_samples"]
    assert all(r["max_algebraic_state_residual"] == 0. for r in result["algebraic_state_checks"])

