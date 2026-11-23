"""Cotejo offline E39-DSL-01 y auditoría de sensibilidad E39-DSL-02.

Solo biblioteca estándar y numpy. No conecta PowerFactory ni sustituye AVR
por un modelo deducido de su nombre. Delta significa pm menos PF o Ka=2,5
menos Ka=5, según la columna. Tiempo en segundos, unidades PF conservadas.
"""
from __future__ import annotations
from power_machine.resources import output_root

import argparse
import csv
from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import re

import numpy as np

from ..dsl_editor import ModelDraft, PreviewCase, compile_draft, simulate_preview
from ..dsl_editor.compiler_adapter import block_view, parse_fields
from ..dsl_editor.schema import compiler_fingerprint
from power_machine.pfcompare.rms_replay import identity, load_reference, dominant_mode, first_peak

ROOT = Path(__file__).resolve().parents[2]
from ..benchmarks.ieee39_pf import DEFAULT_PF_DIR
OUTPUT_DIR = output_root() / "comparison_results/ieee39_pf"
EVENTS_S = (1.0, 3.0, 3.25)
FIELDS = ("sInput", "sOutput", "sStates", "sParams", "sLowLimPar", "sUpLimPar", "sAddEquat")
SIGNALS = ("ustep", "ulimit", "ygain", "ylag", "yint", "ylim", "xlag", "xint", "xlim")
FINE_LIMIT = .001
COARSE_LIMIT = .01


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def digest(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def write_json(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def write_csv(path, rows):
    if not rows:
        raise ValueError("Tabla sin filas ni esquema")
    with Path(path).open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def output_path(path):
    from ..resources import confined_output
    from power_machine.pfcompare.rms_replay import inside_workspace
    path = inside_workspace(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def names(rows):
    return [part.strip() for row in rows for part in row.split(",") if part.strip()]


def seconds(spec):
    """Conversión explícita de ms PF a s, sin inferir unidades por nombre."""
    factors = {"s": 1., "ms": .001}
    if spec.get("unit") not in factors:
        raise ValueError("Unidad temporal PF no soportada")
    return float(spec["value"]) * factors[spec["unit"]]


def solver_options(case_dir, filename):
    raw = read_json(Path(case_dir) / filename)
    inc, sim = raw["ComInc"], raw["ComSim"]
    step_s = seconds(inc["dtgrd"])
    if step_s <= 0:
        raise ValueError("Paso PF no positivo")
    return {"raw": raw, "step_s": step_s, "start_s": seconds(inc["tstart"]),
            "stop_s": seconds(sim["tstop"]), "output_step_s": seconds(inc["dtout"]),
            "syncperiod_s": seconds(inc["syncperiod"]),
            "normalized_limit": FINE_LIMIT if step_s <= .001000000001 else COARSE_LIMIT,
            "criterion": "paso fino, 0,1 %" if step_s <= .001000000001 else "paso grueso, 1 %",
            "method_note": "PF iopt_lt y alpha_rms crudos, pm trapezoidal implícito. No se infiere nombre de enumeración PF."}


def reconstruct_draft(case_dir):
    """Importa ecuaciones y valores exactos del BlkDef, sin usar plantillas."""
    case_dir = Path(case_dir)
    definition, run = read_json(case_dir / "definition.json"), read_json(case_dir / "run.json")
    if definition["check"] != 0 or any(run[k] != 0 for k in ("init_return", "sim_return")):
        raise ValueError("No existe corrida PF exitosa vigente")
    fields = {k: definition["attrs"][k]["value"] for k in FIELDS}
    if fields["sAddEquat"] != definition["equations"]:
        raise ValueError("Ecuaciones duplicadas en export no coinciden")
    param_names = names(fields["sParams"])
    if names(run["model"]["parnam"]["value"]) != param_names:
        raise ValueError("Orden de parámetros PF no coincide")
    values = run["model"]["params"]["value"]
    if len(values) != len(param_names) or any(v is None for v in values):
        raise ValueError("Parámetros PF ausentes")
    # vardef exportado aporta unidad. Ymin/Ymax no declaran unidad en PF.
    units = {}
    for equation in fields["sAddEquat"]:
        match = re.match(r"vardef\((\w+)\)='([^']*)'", equation)
        if match:
            units[match[1]] = match[2]
    params = {k: {"value": v, "unit": units.get(k), "range": [None, None],
                  "provenance": "run.json/model/params, orden definition.json/sParams"}
              for k, v in zip(param_names, values)}
    ident = identity(definition["type"]["path"])
    ports = tuple({"id": ident["object_id"] + ":" + direction + ":" + name,
                   "name": name, "direction": direction, "signal_kind": "continuous_real",
                   "unit": None, "target_id": None, "cardinality": 1}
                  for field, direction in (("sInput", "input"), ("sOutput", "output"))
                  for name in names(fields[field]))
    return ModelDraft("pm-dsl-draft-v1", "E39-DSL-01", **ident,
                      source_format="pf_blkdef_fields_v1", source_text=json.dumps(fields, ensure_ascii=False),
                      parameters=params, ports=ports, original_sha256=digest(case_dir / "definition.json"),
                      metadata={"origin": "pf_export", "pf_object_id": definition["attrs"]["oid_"]["value"],
                                "pf_relative_path": ident["relative_path"], "pf_type_path": definition["type"]["path"],
                                "pf_instance_oid": run["model"]["oid_"]["value"],
                                "pf_instance_name": run["model"]["loc_name"]["value"],
                                "pf_instance_grid_path": run["model"]["cpGrid"]["value"],
                                "state_units": {k: None for k in names(fields["sStates"])},
                                "unit_note": "No se infieren unidades de señales sin vardef exportado"})


def load_bench_csv(path, columns=SIGNALS):
    """Proyecta columnas al iterar el CSV. Vacíos quedan NaN internos, nunca cero."""
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"time_s", *columns} <= set(reader.fieldnames or ()):
            raise ValueError("CSV sin las columnas requeridas")
        data = {k: [] for k in ("time_s", *columns)}
        for row in reader:
            for key in data:
                data[key].append(float(row[key]) if row[key].strip() else np.nan)
    result = {k: np.asarray(v, dtype=float) for k, v in data.items()}
    time_s = result.pop("time_s")
    if len(time_s) < 2 or not np.all(np.isfinite(time_s)) or np.any(np.diff(time_s) <= 0):
        raise ValueError("Calendario PF ausente o no creciente")
    return time_s, result


def analytical_bench(draft, time_s):
    """Solución cerrada independiente, exp numpy y segmentos del estímulo PF.

    No usa el compilador ni resultados PF. Rechaza otra definición antes de
    aplicar estas fórmulas. La limitación es del estado, con antiwindup.
    """
    fields = parse_fields(draft)
    required = {"ustep=select(time()>=1,1,0)",
                "ulimit=select(time()<1,0,select(time()<3,1,select(time()<3.25,-1,0)))",
                "ygain=Kg*ustep", "xlag.=(Kl*ustep-xlag)/Tl", "ylag=xlag",
                "xint.=Ki*ustep", "yint=xint", "xlim.=Ki*ulimit", "ylim=limstate_const(xlim,Ymin,Ymax)"}
    actual = {re.sub(r"\s+", "", e) for e in fields["sAddEquat"] if not e.startswith("vardef(")}
    initial = {f"inc({k})=0" for k in SIGNALS}
    if actual != required | initial:
        raise ValueError("Solución analítica definida solo para las ecuaciones exactas del banco exportado")
    p = {k: v["value"] for k, v in draft.parameters.items()}
    if p["Tl"] <= 0 or p["Ki"] < 0 or not p["Ymin"] <= 0 <= p["Ymax"]:
        raise ValueError("Perfil analítico requiere Tl>0, Ki>=0 y estado cero dentro de límites")
    t = np.asarray(time_s, dtype=float)
    elapsed_s = np.maximum(t - 1., 0.)
    step = (t >= 1.).astype(float)
    limit_input = np.where(t < 1, 0., np.where(t < 3, 1., np.where(t < 3.25, -1., 0.)))
    lag = p["Kl"] * (-np.expm1(-elapsed_s / p["Tl"]))
    integral = p["Ki"] * elapsed_s
    peak = min(p["Ymax"], 2. * p["Ki"])
    limited = np.where(t < 3., np.minimum(p["Ymax"], p["Ki"] * elapsed_s),
                       np.maximum(p["Ymin"], peak - p["Ki"] * np.clip(t - 3., 0., .25)))
    return {"ustep": step, "ulimit": limit_input, "ygain": p["Kg"] * step,
            "ylag": lag, "yint": integral, "ylim": limited,
            "xlag": lag.copy(), "xint": integral.copy(), "xlim": limited.copy()}


def event_mask(time_s, events_s=EVENTS_S):
    return np.any(np.isclose(np.asarray(time_s)[:, None], events_s, atol=1e-10, rtol=0), axis=1)


def error_metrics(reference, predicted, limit):
    """Error absoluto y normalizado por excursión pico a pico de la referencia."""
    ref, pred = np.asarray(reference), np.asarray(predicted)
    if ref.shape != pred.shape or not len(ref):
        raise ValueError("Señales incompatibles o ventana vacía")
    finite = np.isfinite(ref) & np.isfinite(pred)
    if not np.any(finite):
        return {"samples": len(ref), "missing_samples": len(ref), "pf_excursion": None,
                "max_abs_error": None, "max_normalized_error": None, "nrmse": None,
                "end_error": None, "normalized_limit": limit, "passed": False}
    delta = pred[finite] - ref[finite]
    span = float(np.ptp(ref[finite]))
    maximum = float(np.max(abs(delta)))
    normalized = maximum / span if span > 1e-12 else None
    return {"samples": len(ref), "missing_samples": int(np.count_nonzero(~finite)), "pf_excursion": span,
            "max_abs_error": maximum, "max_normalized_error": normalized,
            "nrmse": float(np.sqrt(np.mean(delta**2))) / span if span > 1e-12 else None,
            "end_error": float(pred[-1] - ref[-1]) if finite[-1] else None,
            "normalized_limit": limit,
            "passed": bool(np.all(finite) and (normalized <= limit if normalized is not None else maximum <= 1e-12))}


def metric_tables(time_s, pf, pm, analytical, limit, ident):
    rows = []
    events = event_mask(time_s)
    windows = {"all_samples": np.ones(len(time_s), dtype=bool),
               "excluding_exact_discontinuities": ~events,
               "before_1s": time_s < 1., "1_to_3s": (time_s >= 1.) & (time_s < 3.),
               "3_to_3_25s": (time_s >= 3.) & (time_s < 3.25), "after_3_25s": time_s >= 3.25}
    for comparison, reference, predicted in (("pm_vs_PF", pf, pm), ("analytical_vs_PF", pf, analytical),
                                              ("pm_vs_analytical", analytical, pm)):
        for window, mask in windows.items():
            if not np.any(mask):
                continue
            for signal in SIGNALS:
                rows.append({"comparison": comparison, "window": window, "signal": signal, **ident,
                             "unit": None, **error_metrics(reference[signal][mask], predicted[signal][mask], limit)})
    return rows


def simulate_bench(draft, time_s, step_s):
    result = simulate_preview(draft, PreviewCase(tuple(map(float, time_s)), {}, {}),
                              options={"step_s": step_s, "timeout_s": 120.})
    if result["status"] != "completed":
        raise RuntimeError(json.dumps({"status": result["status"], "diagnostics": result["diagnostics"]}, ensure_ascii=False))
    payload = result["payload"]
    if not np.allclose(payload["time_s"], time_s, atol=1e-12, rtol=0):
        raise ValueError("Calendario pm diferente de PF")
    signals = {k: np.asarray(v) for k, v in payload["outputs"].items()}
    signals.update({k: np.asarray(v) for k, v in payload["states"].items()})
    return signals, result


def provenance(paths):
    return [{"path": str(Path(path).resolve()), "sha256": digest(path)} for path in paths]


def compare_bench(pf_dir=DEFAULT_PF_DIR, output_dir=OUTPUT_DIR, *, halving=True):
    case = Path(pf_dir) / "E39-DSL-01"
    dest = output_path(Path(output_dir) / case.name)
    draft = reconstruct_draft(case)
    options = solver_options(case, "command_options.json")
    preview = compile_draft(draft)
    write_json(dest / "draft.json", asdict(draft))
    write_json(dest / "compile.json", asdict(preview))
    if preview.status != "completed":
        summary = {"status": "unsupported", "accepted": False, "compile": asdict(preview)}
        write_json(dest / "summary.json", summary)
        return summary
    write_json(dest / "ir.json", block_view(draft))
    time_s, pf = load_bench_csv(case / "trajectories_full_precision.csv")
    if abs(time_s[0] - options["start_s"]) > 1e-10 or abs(time_s[-1] - options["stop_s"]) > 1e-10:
        raise ValueError("Horizonte CSV diferente de ComInc/ComSim")
    analytical = analytical_bench(draft, time_s)
    pm, simulation = simulate_bench(draft, time_s, options["step_s"])
    ident = {"object_id": draft.object_id, "relative_path": draft.relative_path}
    metrics = metric_tables(time_s, pf, pm, analytical, options["normalized_limit"], ident)
    write_csv(dest / "metrics.csv", metrics)
    write_json(dest / "simulation.json", simulation)
    write_csv(dest / "trajectories_comparison.csv",
              [{"time_s": float(t), **{f"{source}_{k}": float(data[k][i]) if np.isfinite(data[k][i]) else None
                 for source, data in (("PF", pf), ("pm", pm), ("analytical", analytical)) for k in SIGNALS}}
               for i, t in enumerate(time_s)])
    write_csv(dest / "discontinuities.csv",
              [{"time_s": float(time_s[i]), "signal": k, "pf": float(pf[k][i]), "pm_right": float(pm[k][i]),
                "analytical_right": float(analytical[k][i]), "delta_pm_minus_pf": float(pm[k][i] - pf[k][i])}
               for i in np.flatnonzero(event_mask(time_s)) for k in SIGNALS])
    convergence = []
    half_step_gate = []
    if halving:
        fine, fine_result = simulate_bench(draft, time_s, options["step_s"] / 2.)
        half_metrics = metric_tables(time_s, pf, fine, analytical, options["normalized_limit"], ident)
        write_csv(dest / "metrics_half_step.csv", half_metrics)
        half_step_gate = [r for r in half_metrics if r["window"] == "excluding_exact_discontinuities" and r["comparison"] == "pm_vs_PF"]
        for k in SIGNALS:
            mask = ~event_mask(time_s)
            coarse_error = error_metrics(analytical[k][mask], pm[k][mask], FINE_LIMIT)["max_abs_error"]
            fine_error = error_metrics(analytical[k][mask], fine[k][mask], FINE_LIMIT)["max_abs_error"]
            convergence.append({"signal": k, "step_s": options["step_s"], "half_step_s": options["step_s"] / 2.,
                                "coarse_analytical_error": coarse_error, "fine_analytical_error": fine_error,
                                "fine_over_coarse_error": fine_error/coarse_error if coarse_error > 1e-12 else None,
                                "max_delta_h_halved": float(np.max(abs(fine[k]-pm[k]))),
                                "note": "Incluye error de tratamiento de time() en los saltos, no solo error suave de integración"})
        write_csv(dest / "step_halving.csv", convergence)
        write_json(dest / "simulation_half_step.json", fine_result)
    gate = [r for r in metrics if r["window"] == "excluding_exact_discontinuities" and r["comparison"] == "pm_vs_PF"]
    all_gate = [r for r in metrics if r["window"] == "all_samples" and r["comparison"] == "pm_vs_PF"]
    analytical_gate = [r for r in metrics if r["window"] == "excluding_exact_discontinuities" and r["comparison"] == "analytical_vs_PF"]
    findings = [{"signal": k, "time_s": float(time_s[int(np.argmax(abs(pm[k]-pf[k])))]),
                 "max_algebraic_state_residual": float(np.max(abs(pm[k]-pm[state]))),
                 "expected_relation": k+"="+state}
                for k, state in (("ylag", "xlag"), ("yint", "xint"), ("ylim", "xlim"))]
    write_csv(dest / "algebraic_state_checks.csv", findings)
    requested_changes = ["DSL editor: aterrizar y reiniciar explícitamente comparaciones con time() en 1, 3 y 3,25 s. El preview solo registra un evento interno de limstate.",
                         "Propietario dynamics: revisar publicación de limstate_const al salir de un límite. A 3,001 s pm publica ylim=1 y xlim=0,9995, aunque la ecuación requiere igualdad dentro de banda.",
                         "Propietario dynamics/editor: estabilizar calendario flotante. h/2 cambia el lado evaluado a 1 s y no resuelve la aceptación de todas las señales."]
    summary = {"case": case.name, "status": "completed", "accepted_excluding_exact_discontinuities": all(r["passed"] for r in gate),
               "accepted_all_samples": all(r["passed"] for r in all_gate),
               "accepted_half_step_excluding_exact_discontinuities": all(r["passed"] for r in half_step_gate) if halving else None,
               "pf_analytical_acceptance_independent": all(r["passed"] for r in analytical_gate),
               "options": options, "samples": len(time_s), "signals": list(SIGNALS), **ident,
               "exact_discontinuity_times_s": list(EVENTS_S), "excluded_samples": int(event_mask(time_s).sum()),
               "event_note": "PF publica valores fraccionarios en saltos time(), pm/analítica publican lado derecho. No se ajustan ni interpolan para ocultar la diferencia.",
               "scope": "Banco aislado exportado. No valida lenguaje DSL completo, editor completo ni conexión de controladores a red.",
               "initialization_matches_pf": all(pm[k][0] == pf[k][0] == 0. for k in SIGNALS),
               "requested_changes_outside_scope": requested_changes,
               "algebraic_state_checks": findings,
               "historical_failure": read_json(case / "failure.json"), "assessment": read_json(case / "assessment.json"),
               "compiler_sha256": compiler_fingerprint(), "comparator_sha256": digest(__file__),
               "inputs": provenance([case / k for k in ("definition.json", "run.json", "command_options.json", "trajectories_full_precision.csv", "assessment.json", "failure.json", "check_log.json", "simulation_log.json", "export_options.json", "LEEME.txt")]),
               "dependency_inputs": provenance([ROOT / "power_machine/pfcompare/rms_replay.py"]),
               "metrics": gate, "analytical_metrics": analytical_gate, "half_step_metrics": half_step_gate, "step_halving": convergence}
    summary["result_files"] = provenance(sorted(p for p in dest.iterdir() if p.is_file() and p.name != "summary.json"))
    write_json(dest / "summary.json", summary)
    return summary


def audit_parameter_change(change):
    """Reproduce en copia el cambio exacto Ka 5 a 2,5, conservando otros valores."""
    param_names = change["parameter_names"]
    before, after = change["before"], change["after"]
    if len(param_names) != len(before) or len(before) != len(after) or len(set(param_names)) != len(param_names):
        raise ValueError("Inventario de parámetros incompatible")
    if any(v is None or not np.isfinite(v) for v in before + after):
        raise ValueError("Parámetro ausente o no finito")
    changed = [name for name, a, b in zip(param_names, before, after) if a != b]
    if changed != ["Ka"] or before[param_names.index("Ka")] != 5. or after[param_names.index("Ka")] != 2.5:
        raise ValueError("El ensayo requiere exclusivamente Ka 5 a 2,5")
    replay = dict(zip(param_names, before))
    replay["Ka"] = 2.5
    if list(replay.values()) != after:
        raise ValueError("Reproducción del cambio inconsistente")
    return [{"parameter": k, "pf_before": a, "pf_after": b, "pm_parameter_copy": replay[k],
             "delta": b-a, "unit": None, "changed": a != b,
             "simulation_status": "unsupported, definición AVR y marco no exportados"}
            for k, a, b in zip(param_names, before, after)]


def compare_sensitivity(pf_dir=DEFAULT_PF_DIR, output_dir=OUTPUT_DIR):
    """Audita PF Ka2,5 menos Ka5. Predicciones pm quedan ausentes por huecos."""
    root = Path(pf_dir)
    case, base = root / "E39-DSL-02", root / "E39-DIN-03"
    dest = output_path(Path(output_dir) / case.name)
    change = read_json(case / "avr_change.json")
    ident = identity(change["object"]["path"])
    parameters = audit_parameter_change(change)
    probe = read_json(case / "parameter_probe.json")
    if probe["parsed"] != change["parameter_names"] or probe["params"] != change["before"]:
        raise ValueError("Sondeo de parámetros y cambio PF no coinciden")
    for row in parameters:
        row.update(ident)
    restore = read_json(case / "restore.json")
    if not restore["restored"] or restore["actual"] != change["before"]:
        raise ValueError("Restauración PF no coincide")
    controllers = read_json(root / "modelo/controllers.json")
    avr = next(row for row in controllers if row["path"] == change["object"]["path"])
    if avr["fields"]["params"]["value"] != change["before"]:
        raise ValueError("AVR del inventario y corrida base no coinciden")
    options = solver_options(case, "effective_command_options.json")
    base_options = solver_options(base, "effective_command_options.json")
    if any(options[k] != base_options[k] for k in ("step_s", "start_s", "stop_s", "output_step_s")):
        raise ValueError("Opciones temporales PF distintas entre las dos corridas")
    reports = [read_json(p / "report.json") for p in (base, case)]
    event_fields = ("time", "outserv", "p_target", "iopt_type", "dP", "dQ")
    event_profiles = []
    for report in reports:
        if any(report[k] != 0 for k in ("ldf_code", "inc_code", "sim_code", "export_code")) or report["is_sim_valid"] != 1:
            raise ValueError("Corrida PF inválida")
        event_profiles.append([{k: e["attributes"][k] for k in event_fields} for e in report["events"]])
    if event_profiles[0] != event_profiles[1]:
        raise ValueError("Eventos diferentes entre referencia y sensibilidad")
    t0, y0, channels0 = load_reference(base)
    t1, y1, channels1 = load_reference(case)
    if t0.shape != t1.shape or not np.allclose(t0, t1, rtol=0, atol=1e-10):
        raise ValueError("Calendarios PF diferentes, no se interpolan saltos")
    if [(c["signal"], c["relative_path"], c["variable"], c["unit"]) for c in channels0] != [(c["signal"], c["relative_path"], c["variable"], c["unit"]) for c in channels1]:
        raise ValueError("Canales PF incompatibles")
    deltas = {k: y1[k]-y0[k] for k in y0}
    metrics, trajectories, modes = [], [], []
    for channel in channels0:
        k = channel["signal"]
        finite = np.isfinite(deltas[k])
        metrics.append({"signal": k, "object_id": channel["object_id"], "relative_path": channel["relative_path"],
                        "unit": channel["unit"], "pf_max_abs_delta": float(np.max(abs(deltas[k][finite]))) if finite.any() else None,
                        "pf_end_delta": float(deltas[k][-1]) if finite[-1] else None,
                        "pf_base_excursion": float(np.ptp(y0[k][finite])) if finite.any() else None,
                        "missing_samples": int((~finite).sum()), "pm_max_abs_delta": None,
                        "pm_pf_normalized_error": None, "accepted": None, "status": "unsupported"})
        if k in ("G32_speed_pu", "G32_rotor_rad", "G32_P_MW", "G32_Q_Mvar", "G32_excitation_pu", "B4_U_pu"):
            original, reduced = dominant_mode(t0, y0[k]), dominant_mode(t1, y1[k])
            row = {"signal": k, "object_id": channel["object_id"], "relative_path": channel["relative_path"],
                   "base_status": original["status"], "changed_status": reduced["status"],
                   "pf_first_peak_base_s": first_peak(t0, y0[k]), "pf_first_peak_changed_s": first_peak(t1, y1[k]),
                   "pm_frequency_hz": None, "pm_damping_ratio": None}
            for key in ("frequency_hz", "damping_ratio", "fit_relative_error", "dominance"):
                a, b = original.get(key), reduced.get(key)
                row.update({"pf_base_"+key: a, "pf_changed_"+key: b, "pf_delta_"+key: b-a if a is not None and b is not None else None})
            modes.append(row)
    for i, t in enumerate(t0):
        trajectories.append({"time_s": float(t), **{f"{prefix}_{k}": float(data[k][i]) if np.isfinite(data[k][i]) else None
                             for prefix, data in (("PF_Ka5", y0), ("PF_Ka2_5", y1), ("PF_delta", deltas)) for k in y0}})
    gaps = ["No existe definition.json ni grafo BlkDef de avr_IEEET1 para DSL-02. El nombre no determina ecuaciones.",
            "Faltan ecuaciones, estados, inicialización, saturación, límites y cableado de marco IEEE39 del AVR.",
            "Faltan grafos y acoplamiento IEEEG1/IEEEG3 y carga RMS con memoria t1. La red completa no es reproducible con el motor actual.",
            "No existen run.json ni trajectories_full_precision.csv en DSL-02. Se usa ComRes signals_native.csv y base DIN-03.",
            "Los polos estimados de señales PF no son autovalores del sistema ni parámetros identificados del AVR."]
    write_csv(dest / "parameters.csv", parameters)
    write_csv(dest / "sensitivity_metrics.csv", metrics)
    write_csv(dest / "dominant_signal_parameters.csv", modes)
    write_csv(dest / "sensitivity_trajectories.csv", trajectories)
    summary = {"case": case.name, "status": "unsupported", "pm_simulation_completed": False,
               "accepted": False, "pf_sensitivity_audited": True, **ident,
               "type_fields_exported": avr["type_fields"], "parameter_change_reproduced_in_copy": True,
               "changed_parameters": ["Ka"], "base_case": base.name, "delta_definition": "Ka=2,5 menos Ka=5",
               "options": options, "events": event_profiles[1], "samples": len(t0), "gaps": gaps,
               "dominant_parameters_note": "Estimaciones de señal PF por pencil matricial en 3..10 s, con criterios y limitaciones heredados de PFC-DIN.",
               "restore": restore, "assessment": read_json(case / "assessment.json"),
               "comparator_sha256": digest(__file__),
               "dependency_inputs": provenance([ROOT / "power_machine/pfcompare/rms_replay.py"]),
               "inputs": provenance([case / k for k in ("avr_change.json", "parameter_probe.json", "restore.json", "signals_native.csv", "channel_units.json", "report.json", "effective_command_options.json", "assessment.json", "LEEME.txt")] +
                                    [base / k for k in ("signals_native.csv", "channel_units.json", "report.json", "effective_command_options.json")] + [root / "modelo/controllers.json"]),
               "sensitivity_metrics": metrics, "dominant_signal_parameters": modes}
    summary["result_files"] = provenance(sorted(p for p in dest.iterdir() if p.is_file() and p.name != "summary.json"))
    write_json(dest / "summary.json", summary)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--case", choices=("all", "E39-DSL-01", "E39-DSL-02"), default="all")
    args = parser.parse_args()
    if args.case in ("all", "E39-DSL-01"):
        result = compare_bench(args.pf_dir, args.output_dir)
        print(json.dumps({k: result[k] for k in ("case", "status", "accepted_excluding_exact_discontinuities", "accepted_all_samples", "pf_analytical_acceptance_independent")}, ensure_ascii=False))
    if args.case in ("all", "E39-DSL-02"):
        result = compare_sensitivity(args.pf_dir, args.output_dir)
        print(json.dumps({k: result[k] for k in ("case", "status", "pm_simulation_completed", "pf_sensitivity_audited")}, ensure_ascii=False))


if __name__ == "__main__":
    main()


def compare_signals(reference, predicted, limit=FINE_LIMIT):
    """Compara se?ales arbitrarias, alineadas por el usuario y en la misma unidad."""
    if set(reference) != set(predicted):
        raise ValueError("Las se?ales de referencia y c?lculo deben coincidir")
    return {key: error_metrics(reference[key], predicted[key], limit) for key in reference}
