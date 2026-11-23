'Runner support for power-system critical clearing time.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import copy
from dataclasses import asdict, is_dataclass
import json
import math
from pathlib import Path
import tempfile

import numpy as np

from power_machine.benchmarks.ieee39_dynamics import ClassicalSystem
from .results import diagnostic, envelope, fingerprint
from .schema import CCTInput, CCTOptions


WORK_ROOT = Path(__file__).resolve().parents[2]


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _coi_metrics(times, angles_deg, voltages_pu, weights, clear_s, options):
    """Evalúa la envolvente angular respecto del COI y la ventana final de tensión."""
    if not times or len(times) != len(angles_deg) or len(times) != len(voltages_pu):
        return {"classification": "not_evaluable", "stable": None,
                "reason": 'RMS trajectory is missing or inconsistent', "trajectory": []}
    rows, max_angle = [], 0.0
    for time_s, angles, voltages in zip(times, angles_deg, voltages_pu):
        angle_array = np.asarray(angles, dtype=float)
        voltage_array = np.asarray(voltages, dtype=float)
        if not np.isfinite(angle_array).all() or not np.isfinite(voltage_array).all():
            return {"classification": "not_evaluable", "stable": None,
                    "reason": 'RMS trajectory contains nonfinite values', "trajectory": rows}
        coi = float(np.dot(weights, angle_array) / np.sum(weights))
        relative = angle_array - coi
        peak = float(np.max(np.abs(relative)))
        max_angle = max(max_angle, peak)
        rows.append({"time_s": float(time_s), "coi_angle_deg": coi,
                     "rotor_angles_from_coi_deg": [float(v) for v in relative],
                     "min_voltage_pu": float(np.min(voltage_array))})
    recovery_start = clear_s + options.recovery_window_s
    recovered = [row["min_voltage_pu"] for row in rows if row["time_s"] >= recovery_start - 1e-12]
    min_recovery = min(recovered) if recovered else None
    if min_recovery is None:
        return {"classification": "not_evaluable", "stable": None,
                "reason": 'The horizon does not contain the requested recovery window',
                "max_angle_from_coi_deg": max_angle, "min_recovery_voltage_pu": None,
                "trajectory": rows}
    angular_ok = max_angle <= options.max_angle_from_coi_deg
    voltage_ok = min_recovery >= options.recovery_min_voltage_pu
    classification = "stable" if angular_ok and voltage_ok else "unstable"
    return {"classification": classification, "stable": classification == "stable", "max_angle_from_coi_deg": max_angle,
            "min_recovery_voltage_pu": min_recovery, "angular_ok": angular_ok,
            "voltage_recovery_ok": voltage_ok, "trajectory": rows}


class _CCTClassicalSystem(ClassicalSystem):
    """Adaptación de eventos del IEEE39 clásico, sin alterar su integrador RK4."""

    def __init__(self, model, dynamic_data, clearing_action, clearing_branch_id):
        super().__init__(model, dynamic_data)
        self.clearing_action = clearing_action
        self.clearing_branch_id = clearing_branch_id

    def apply(self, kind):
        if kind == "fault":
            self.fault_bus = "B16"
            self.rebuild()
        elif kind == "clear":
            self.fault_bus = None
            if self.clearing_action == "open_branch":
                self.network.set_branch_open(self.clearing_branch_id)
            self.rebuild()
        else:
            super().apply(kind)


def _run_classical_trial(model, dynamic_data, request, options, duration_s):
    from power_machine.benchmarks.ieee39_dynamics import simulate
    impedance = (request.fault_r_ohm, request.fault_x_ohm)
    if impedance != (None, None) and impedance != (0.0, 0.0):
        raise _coded_error(NotImplementedError, 'unsupported_runner_data', 'The IEEE39 classical engine only supports a bolted fault, Rf=Xf=0 ohm')
    if (request.fault_r_ohm is None) != (request.fault_x_ohm is None):
        raise _coded_error(NotImplementedError, 'unsupported_runner_data', 'The IEEE39 classical engine requires both Rf and Xf, or neither')
    if request.fault_bus_id not in ("", "B16"):
        raise _coded_error(NotImplementedError, 'unsupported_runner_data', 'The IEEE39 classical engine only supports a three-phase fault at B16')
    if abs(request.fault_inception_s - 1.0) > 1e-12:
        raise _coded_error(NotImplementedError, 'unsupported_runner_data', 'The IEEE39 classical engine fixes fault initiation at 1 s')
    branch = request.clearing_branch_id
    if request.clearing_action == "open_branch" and branch != "L16-17":
        raise _coded_error(NotImplementedError, 'unsupported_runner_data', 'The IEEE39 classical engine only opens L16-17 at clearing')
    system = _CCTClassicalSystem(model, dynamic_data, request.clearing_action, branch)
    run = simulate(system, "E39-DIN-01", end_s=options.end_s, step_s=options.step_s,
                   clearing_s=duration_s, stop_on_instability=False)
    times = [sample.time_s for sample in run.samples]
    angles = [np.degrees(sample.state[0, sample.active]) for sample in run.samples]
    voltages = [np.abs(sample.voltage_pu) for sample in run.samples]
    weights = system.h_system_s[system.active]
    metric = _coi_metrics(times, angles, voltages, weights,
                          request.fault_inception_s + duration_s, options)
    return {"clearing_s": float(duration_s), "engine": "ieee39_classical",
            "report": copy.deepcopy(run.report), **metric}


def _pm_weights(model):
    weights = {}
    s_base = float(model.get("meta", {}).get("s_base_mva", 0))
    for gen in model.get("gens", []):
        if gen.get("class") != "ElmSym" or gen.get("outserv"):
            continue
        inertia = gen.get("type_params", {}).get("h")
        rating = gen.get("s_rated_mva")
        units = gen.get("n_units", 1)
        if inertia is None or rating is None:
            raise _coded_error(ValueError, 'missing_runner_data', f"Missing inertia or rated power for {gen['id']}")
        value = float(inertia) * float(rating) * float(units) / s_base
        if not math.isfinite(value) or value <= 0:
            raise _coded_error(ValueError, 'invalid_runner_data', f"Invalid equivalent inertia for {gen['id']}")
        weights[gen["id"]] = value
    if len(weights) < 2:
        raise _coded_error(ValueError, 'missing_runner_data', 'The COI criterion requires at least two synchronous machines')
    return weights


def _run_pm_trial(root, model, study, request, options, duration_s, cancel, progress):
    """Usa StudyCase y Simulation existentes, solicitando solo señales necesarias."""
    from power_machine.lf_status import energized_status
    from power_machine.study import SimEvent, SimOptions, Simulation, StudyCase, run_loadflow, signal_catalog
    weights_by_id = _pm_weights(model)
    index = {item["id"]: item for group in ("buses", "branches", "branches3", "switches", "gens", "loads", "shunts")
             for item in model.get(group, [])}
    if request.fault_bus_id not in index or index[request.fault_bus_id].get("class") != "ElmTerm":
        raise _coded_error(ValueError, 'invalid_runner_data', 'fault_bus_id must identify an ElmTerm bus in the model')
    if request.fault_r_ohm is None or request.fault_x_ohm is None:
        raise _coded_error(ValueError, 'missing_runner_data', 'The RMS engine requires explicit Rf and Xf in ohm')
    clear_s = request.fault_inception_s + duration_s
    events = [SimEvent(request.fault_inception_s, "short_circuit", request.fault_bus_id,
                       {"r_ohm": float(request.fault_r_ohm), "x_ohm": float(request.fault_x_ohm)}),
              SimEvent(clear_s, "clear_short_circuit", request.fault_bus_id)]
    if request.clearing_action == "open_branch":
        if request.clearing_branch_id not in index:
            raise _coded_error(ValueError, 'invalid_runner_data', 'clearing_branch_id does not exist in the model')
        events.append(SimEvent(clear_s, "switch_element", request.clearing_branch_id, {"action": "open"}))
    source_options = study.get("options", {})
    case = StudyCase(name="CCT", scenario=copy.deepcopy(study.get("scenario", {})), events=events,
                     options=SimOptions(t_start_s=float(source_options.get("t_start_s", -0.1)),
                                        t_end_s=options.end_s, dt_s=options.step_s, output_every=1),
                     monitored=[], ldf_options=copy.deepcopy(study.get("ldf_options", {"q_limits": True})))
    # El booleano global converged marca también islas sin referencia. CCT parte
    # solo de las islas energizadas que realmente convergieron.
    loadflow = run_loadflow(root, case, model)
    preflight = energized_status(loadflow)
    if not preflight["ok"]:
        raise _coded_error(RuntimeError, 'invalid_runner_data', preflight['message'])
    bus_ids = [row["id"] for row in loadflow["buses"] if row.get("energized")]
    gens = [gen for gen in model.get("gens", []) if gen["id"] in weights_by_id and gen.get("bus") in bus_ids]
    if len(gens) < 2 or not bus_ids:
        raise _coded_error(RuntimeError, 'invalid_runner_data', 'Base load flow has insufficient energized machines and buses for CCT')
    catalog = signal_catalog(root, model)
    angle_by_id = {row["element_id"]: row["key"] for row in catalog
                   if row["element_class"] == "ElmSym" and row["variable"] == "s:phi" and row["unit"] == "deg"}
    voltage_by_id = {row["element_id"]: row["key"] for row in catalog
                     if row["element_class"] == "ElmTerm" and row["variable"] == "m:u" and row["unit"] == "p.u."}
    missing_angles = [gen["id"] for gen in gens if gen["id"] not in angle_by_id]
    missing_voltages = [bus_id for bus_id in bus_ids if bus_id not in voltage_by_id]
    if missing_angles or missing_voltages:
        raise _coded_error(RuntimeError, 'missing_runner_data', 'Public catalog lacks CCT signals, angles=' + repr(missing_angles) + ', voltages=' + repr(missing_voltages))
    angle_keys = [angle_by_id[gen["id"]] for gen in gens]
    voltage_keys = [voltage_by_id[bus_id] for bus_id in bus_ids]
    case.monitored = [*angle_keys, *voltage_keys]
    header, captured = [], []
    from power_machine.resources import work_path
    parent = (Path(study["output_root"]) / "_temporales" / "cct"
              if study.get("output_root") is not None else work_path("_temporales") / "cct")
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cct_", dir=parent) as output:
        simulation = Simulation(root, case, model=model)
        summary = simulation.run(on_header=lambda values: header.extend(values),
                                 on_samples=lambda rows: captured.extend(rows), on_progress=progress,
                                 cancel=cancel, output_dir=output, batch=10)
    if summary.get("cancelled"):
        return None
    if summary.get("failure") or not summary.get("completed"):
        raise _coded_error(RuntimeError, 'invalid_runner_data', summary.get('failure') or 'RMS run did not finish')
    positions = {key: number for number, key in enumerate(header)}
    time_key = "time|b:tnow|s"
    missing = [key for key in [time_key, *angle_keys, *voltage_keys] if key not in positions]
    if missing:
        raise _coded_error(RuntimeError, 'missing_runner_data', 'RMS engine did not return public-contract CCT signals: ' + repr(missing))
    times, angles, voltages = [], [], []
    for row in captured:
        times.append(float(row[positions[time_key]]))
        angles.append([float(row[positions[key]]) for key in angle_keys])
        voltages.append([float(row[positions[key]]) for key in voltage_keys])
    metric = _coi_metrics(times, angles, voltages, np.array([weights_by_id[g["id"]] for g in gens]), clear_s, options)
    return {"clearing_s": float(duration_s), "engine": "pm_rms", "report": summary,
            "energized_loadflow": preflight, **metric}


def run_cct(model, study, options: CCTOptions, request: CCTInput, *, root=None,
            dynamic_data=None, cancel=None, progress=None):
    'Bisect a critical-clearing-time interval and retain the stable and unstable frontier trajectories.'
    if not isinstance(study, dict):
        if not is_dataclass(study):
            raise _coded_error(TypeError, 'invalid_runner_data', 'study must be a dictionary or a study case')
        study = asdict(study)
    if dynamic_data is None and model.get("meta", {}).get("benchmark") == "ieee39":
        from power_machine.resources import required_readonly_path
        dynamic_data = json.loads(required_readonly_path("cases", "ieee39", "dynamic_data.json").read_text(encoding="utf-8"))
    engine = request.engine
    if engine == "auto":
        engine = "ieee39_classical" if model.get("meta", {}).get("benchmark") == "ieee39" else "pm_rms"
    if request.fault_inception_s + options.upper_clearing_s + options.recovery_window_s > options.end_s:
        raise _coded_error(ValueError, 'invalid_runner_data', 'Horizon does not cover the maximum fault duration and recovery window')
    if engine == "ieee39_classical":
        if dynamic_data is None:
            raise _coded_error(ValueError, 'missing_runner_data', 'The IEEE39 classical engine requires dynamic_data')
        run_one = lambda duration, current: _run_classical_trial(model, dynamic_data, request, current, duration)
        validity = ['Independent IEEE39 benchmark, not validated against PowerFactory',
                    'Bolted fault at B16 at 1 s. Simple clearing or L16-17 opening as supported by the classical engine']
    elif engine == "pm_rms":
        if root is None:
            raise _coded_error(ValueError, 'missing_runner_data', 'The RMS engine requires root')
        run_one = lambda duration, current: _run_pm_trial(Path(root), model, study, request, current, duration, cancel, progress)
        validity = ['RMS engine, not validated against PowerFactory',
                    'Outside the application reference case if fault, switching, step or horizon changes']
    else:
        raise _coded_error(ValueError, 'invalid_runner_data', 'Unknown CCT engine')
    trials = []
    def evaluate(duration, phase, current_options=options):
        if _cancelled(cancel):
            return None
        if progress:
            progress({"phase": phase, "completed": len(trials), "total": None,
                      "clearing_s": float(duration)})
        try:
            item = run_one(duration, current_options)
        except NotImplementedError:
            raise
        except Exception as exc:
            # Un fallo numérico o del motor no demuestra inestabilidad.
            item = {"clearing_s": float(duration), "engine": engine, "stable": None,
                    "classification": "not_evaluable", "reason": f'{type(exc).__name__}: {exc}',
                    "trajectory": []}
        if item is not None:
            trials.append(item)
        return item
    grid = [options.lower_clearing_s + (options.upper_clearing_s-options.lower_clearing_s)*n/4 for n in range(5)]
    probes = [evaluate(duration, "interior_probe") for duration in grid]
    if any(item is None for item in probes):
        return {"status": "partial", "engine": engine, "trials": trials, "cancelled": True,
                "validity_messages": validity}
    non_evaluable = [item for item in probes if item["classification"] == "not_evaluable"]
    stable_probes = [item for item in probes if item["classification"] == "stable"]
    unstable_probes = [item for item in probes if item["classification"] == "unstable"]
    if non_evaluable:
        return {"status": "partial", "engine": engine, "trials": trials, "cancelled": False,
                "validity_messages": validity, "reason": 'Unevaluable probes cannot serve as unstable endpoints',
                "stable_trials": stable_probes, "unstable_trials": unstable_probes,
                "not_evaluable_trials": non_evaluable}
    segments, previous, stable_intervals = [], None, []
    stable_start = None
    for item in probes:
        if item["classification"] != previous:
            segments.append(item["classification"])
            previous = item["classification"]
        if item["classification"] == "stable" and stable_start is None:
            stable_start = item["clearing_s"]
        if item["classification"] != "stable" and stable_start is not None:
            stable_intervals.append({"lower_s": stable_start, "upper_s": item["clearing_s"]})
            stable_start = None
    if stable_start is not None:
        stable_intervals.append({"lower_s": stable_start, "upper_s": probes[-1]["clearing_s"]})
    if segments.count("stable") > 1:
        return {"status": "partial", "engine": engine, "trials": trials, "cancelled": False,
                "validity_messages": validity, "reason": 'Probing found separate stable windows. No unique CCT is reported',
                "stable_probe_intervals": stable_intervals, "stable_trials": stable_probes,
                "unstable_trials": unstable_probes, "not_evaluable_trials": []}
    if probes[0]["classification"] != "stable" or probes[-1]["classification"] != "unstable":
        bracket = {"classification": "not_evaluable", "stable": None,
                   "reason": 'Requested interval does not establish a stable/unstable CCT bracket',
                   "lower_clearing_s": options.lower_clearing_s, "upper_clearing_s": options.upper_clearing_s}
        return {"status": "partial", "engine": engine, "trials": trials, "cancelled": False,
                "validity_messages": validity, "reason": 'No stable/unstable bracket in the requested interval',
                "stable_trials": stable_probes, "unstable_trials": unstable_probes,
                "not_evaluable_trials": [bracket]}
    split = next(number for number, item in enumerate(probes) if item["classification"] == "unstable")
    lower, stable = probes[split-1]["clearing_s"], probes[split-1]
    upper, unstable = probes[split]["clearing_s"], probes[split]
    while upper - lower > options.resolution_s:
        item = evaluate((lower + upper) / 2.0, "bisection")
        if item is None:
            return {"status": "partial", "engine": engine, "trials": trials, "cancelled": True,
                    "validity_messages": validity}
        if item["classification"] == "not_evaluable":
            return {"status": "partial", "engine": engine, "trials": trials, "cancelled": False,
                    "validity_messages": validity, "reason": 'Bisection stopped after an unevaluable trial',
                    "stable_trials": [row for row in trials if row["classification"] == "stable"],
                    "unstable_trials": [row for row in trials if row["classification"] == "unstable"],
                    "not_evaluable_trials": [row for row in trials if row["classification"] == "not_evaluable"]}
        if item["classification"] == "stable":
            lower, stable = item["clearing_s"], item
        else:
            upper, unstable = item["clearing_s"], item
    refined_options = CCTOptions(**{**asdict(options), "step_ms": options.step_ms / 2.0})
    refined_stable = evaluate(lower, "step_refinement", refined_options)
    refined_unstable = evaluate(upper, "step_refinement", refined_options)
    if refined_stable is None or refined_unstable is None:
        return {"status": "partial", "engine": engine, "trials": trials, "cancelled": True,
                "validity_messages": validity}
    refinement = {"base_step_ms": options.step_ms, "reduced_step_ms": refined_options.step_ms,
                  "stable_classification": refined_stable["classification"],
                  "unstable_classification": refined_unstable["classification"],
                  "consistent": refined_stable["classification"] == "stable" and refined_unstable["classification"] == "unstable"}
    if not refinement["consistent"]:
        return {"status": "partial", "engine": engine, "trials": trials, "cancelled": False,
                "validity_messages": validity,
                "reason": 'Reduced-step refinement did not preserve both boundary classes. CCT interval is unconfirmed',
                "step_refinement": refinement,
                "candidate_frontiers": {"stable": stable, "unstable": unstable},
                "stable_trials": [row for row in trials if row["classification"] == "stable"],
                "unstable_trials": [row for row in trials if row["classification"] == "unstable"],
                "not_evaluable_trials": [row for row in trials if row["classification"] == "not_evaluable"]}
    return {"status": "completed", "engine": engine, "trials": trials, "cancelled": False,
            "validity_messages": validity, "stable_lower_s": lower, "unstable_upper_s": upper,
            "resolution_ms": (upper - lower) * 1000.0, "simulation_count": len(trials),
            "stable_frontier": stable, "unstable_frontier": unstable, "step_refinement": refinement,
            "stable_trials": [row for row in trials if row["classification"] == "stable"],
            "unstable_trials": [row for row in trials if row["classification"] == "unstable"],
            "not_evaluable_trials": [row for row in trials if row["classification"] == "not_evaluable"]}


def execute_request(request, *, cancel=None, progress=None):
    'Execute a serializable worker request using explicit snapshots and confined input paths.'
    model, study, result = {}, {}, None
    try:
        if not isinstance(request, dict) or request.get("schema") != "pm-parity-request-v1" or request.get("module") != "cct":
            raise _coded_error(ValueError, 'invalid_runner_data', 'Incompatible CCT schema or module')
        root = Path(request["root"]).resolve()
        snapshot = copy.deepcopy(request["snapshot"])
        model, study = snapshot["model"], snapshot["study"]
        hashes = snapshot.get("fingerprints", {})
        if hashes.get("model_sha256") != fingerprint(model) or hashes.get("study_sha256") != fingerprint(study):
            raise _coded_error(ValueError, 'invalid_runner_data', 'Incompatible snapshot fingerprint')
        options = CCTOptions(**request.get("options", {}))
        input_data = CCTInput(**request.get("input", {}))
        result = envelope(model, study, asdict(options))
        raw_dynamic = None
        if input_data.engine in ("auto", "ieee39_classical") and model.get("meta", {}).get("benchmark") == "ieee39":
            from power_machine.resources import required_readonly_path
            data_path = required_readonly_path("cases", "ieee39", "dynamic_data.json")
            raw_dynamic = json.loads(data_path.read_text(encoding="utf-8-sig"))
        search = run_cct(model, study, options, input_data, root=root, dynamic_data=raw_dynamic,
                         cancel=cancel, progress=progress)
        result["status"] = search.pop("status")
        result["payload"] = search
        result["provenance"]["validity_messages"] = search.get("validity_messages", [])
        result["provenance"]["assumptions"] = ['Bisection assumes a monotone boundary within the interval',
                                                  'Finite-horizon stability does not establish asymptotic stability']
        if result["status"] == "partial":
            result["diagnostics"].append(diagnostic('cct_partial', search.get("reason", 'Incomplete CCT calculation')))
        return result
    except NotImplementedError as exc:
        result = result or envelope(model, study, {})
        result["status"] = "unsupported"
        result["diagnostics"].append(diagnostic('unsupported', exc, level="error"))
        return result
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
        result = result or envelope(model, study, {})
        result["status"] = "invalid_input" if isinstance(exc, (ValueError, KeyError, TypeError)) else "error"
        result["diagnostics"].append(diagnostic('cct_request_error', exc, level="error"))
        return result
