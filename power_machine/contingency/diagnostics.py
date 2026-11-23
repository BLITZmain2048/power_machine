"Explicit data, numerical methods and traceable results for contingency studies."
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
from pathlib import Path

from ..elements import load_pq_mw
from .metrics import energized_status
from ..loading import branch_loading
from ..loadflow import run_loadflow


FRACTIONS = (1.0, 0.75, 0.5, 0.25, 0.1, 0.05, 0.01, 0.0)
LOAD_REDUCTIONS = (0.10, 0.20, 0.30)


def _index(model):
    return {item["id"]: item for group in ("buses", "branches", "branches3", "loads", "gens")
            for item in model.get(group, ())}


def _identity(item):
    return {"model_id": item["id"], "object_id": item.get("object_id", item["id"]),
            "relative_path": item.get("relative_path", item.get("path")), "name": item.get("name", item["id"])}


def model_sha256(model):
    "Return the canonical hash of the diagnostic model snapshot."
    encoded = json.dumps(model, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def read_raw_model(root):
    "Read model.json without applying operational overrides."
    path = Path(root).resolve() / "model.json"
    model = json.loads(path.read_text(encoding="utf-8-sig"))
    if not str(model.get("schema", "")).endswith("-model-v1"):
        raise ValueError('[invalid_input] ' + "Incompatible model schema")
    return model, {"model_path": "model.json", "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                   "overrides_applied": False}


def _valid_fraction(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError('[invalid_input] ' + f"{name} must be between 0 and 1")
    return float(value)


def scale_line_admittance(model, branch_id, fraction):
    "Copy the model and scale an ElmLne by the requested admittance fraction."
    fraction = _valid_fraction(fraction, "fraction")
    result = copy.deepcopy(model)
    item = _index(result).get(branch_id)
    if item is None:
        raise ValueError('[invalid_input] ' + f"Unknown branch: {branch_id}")
    if item.get("class") != "ElmLne":
        raise ValueError('[invalid_input] ' + "Homotopy supports ElmLne only")
    if item.get("outserv", 0):
        raise ValueError('[invalid_input] ' + "The branch must be in service in the base model")
    if fraction == 0.0:
        item["outserv"] = 1
        return result
    params = item.setdefault("params", {})
    for key in ("R1", "X1"):
        value = params.get(key)
        if value is None or not math.isfinite(float(value)):
            raise ValueError('[invalid_input] ' + f"{branch_id} is missing finite {key}")
        params[key] = float(value) / fraction
    for key in ("G1", "B1", "C1"):
        if params.get(key) is not None:
            value = float(params[key])
            if not math.isfinite(value):
                raise ValueError('[invalid_input] ' + f"{branch_id} has nonfinite {key}")
            params[key] = value * fraction
    return result


def reduce_internal_positive_loads(model, reduction):
    "Copy the model and reduce positive demand in the explicitly tagged internal scope."
    reduction = _valid_fraction(reduction, "reduction")
    result = copy.deepcopy(model)
    changed, base_p_mw = [], 0.0
    for load in result.get("loads", ()):
        if load.get("scope") != "sistema_interno" or load.get("outserv", 0):
            continue
        try:
            p_mw, _ = load_pq_mw(load)
        except (KeyError, TypeError, ValueError):
            continue
        if not math.isfinite(p_mw) or p_mw <= 0:
            continue
        params = load.setdefault("params", {})
        if params.get("scale0") is None:
            continue
        params["scale0"] = float(params["scale0"]) * (1.0 - reduction)
        load["scale"] = params["scale0"]
        changed.append(load["id"])
        base_p_mw += p_mw
    return result, {"reduction_fraction": reduction, "remaining_fraction": 1.0 - reduction,
                    "load_ids": sorted(changed), "base_positive_internal_p_mw": base_p_mw}


def _parallel_branch_ids(model, branch_id):
    branch = _index(model)[branch_id]
    terminals = frozenset((branch.get("bus_from"), branch.get("bus_to")))
    return sorted(item["id"] for item in model.get("branches", ())
                  if item["id"] != branch_id and not item.get("outserv", 0)
                  and frozenset((item.get("bus_from"), item.get("bus_to"))) == terminals)


def _minimum_voltage(result, index):
    rows = [row for row in result.get("buses", ())
            if row.get("status") == "resuelto" and isinstance(row.get("u_pu"), (int, float))
            and math.isfinite(row["u_pu"])]
    if not rows:
        return None
    row = min(rows, key=lambda item: item["u_pu"])
    item = index.get(row["id"], row)
    return {**_identity(item), "u_pu": row["u_pu"], "u_kv": row.get("u_kv"),
            "angle_deg": row.get("angle_deg")}


def _critical_nodes(result, index):
    values = []
    for island in result.get("islands", ()):
        node = island.get("worst_mismatch_node")
        if node is not None:
            item = index.get(node, {"id": node})
            values.append({**_identity(item), "island_id": island.get("id"),
                           "reason": island.get("reason"),
                           "max_mismatch_mva": island.get("max_mismatch_mva")})
    return values


def _q_limits(result, index):
    rows = []
    for row in result.get("gens", ()):
        if row.get("q_limit") or row.get("at_q_limit"):
            item = index.get(row["id"], row)
            rows.append({**_identity(item), "bus_id": row.get("bus"), "q_mvar": row.get("q_mvar"),
                         "p_mw": row.get("p_mw")})
    return rows


def _branch_flow(model, result, branch_id):
    rows = {row.get("id"): row for row in result.get("branches", ())}
    loading = branch_loading(model, result)
    row = rows.get(branch_id)
    item = _index(model)[branch_id]
    if row is None:
        return {**_identity(item), "status": "sin_resultado", "loading_pct": None}
    return {**_identity(item), "status": row.get("status"),
            "p_from_mw": row.get("p_from_mw"), "q_from_mvar": row.get("q_from_mvar"),
            "i_from_ka": row.get("i_from_ka"), "p_to_mw": row.get("p_to_mw"),
            "q_to_mvar": row.get("q_to_mvar"), "i_to_ka": row.get("i_to_ka"),
            "loading_pct": loading.get(branch_id, {}).get("loading_pct"),
            "loading_cause": loading.get(branch_id, {}).get("cause")}


def _parallel_flows(model, result, parallel_ids):
    return [_branch_flow(model, result, branch_id) for branch_id in parallel_ids]


def _solve(model, marker_id, previous=None, options=None):
    """Use the public load-flow interface with a marker scenario to enable warm start."""
    options = {"q_limits": True, "tolerance_pu": 1e-8, "max_iter": 30, **(options or {})}
    if previous is None:
        return run_loadflow(model, options={**options, "warm_start": False})
    marker = _index(model)[marker_id]
    scenario = {"outserv": {marker_id: int(marker.get("outserv", 0))}}
    return run_loadflow(model, scenario=scenario, options={**options, "warm_start": True, "base_result": previous})


def _observation(model, result, branch_id, parallel_ids, *, fraction=None, reduction=None, warm_fraction=None):
    index = _index(model)
    summary = energized_status(result)
    return {"admittance_fraction": fraction, "load_reduction_fraction": reduction,
            "warm_start_fraction": warm_fraction, "energized_status": summary,
            "global_converged": bool(result.get("converged")), "iterations": result.get("iterations"),
            "max_mismatch_mva": result.get("max_mismatch_mva"),
            "minimum_voltage": _minimum_voltage(result, index),
            "critical_nodes": _critical_nodes(result, index), "q_limited_generators": _q_limits(result, index),
            "outage_branch": _branch_flow(model, result, branch_id),
            "parallel_branches": _parallel_flows(model, result, parallel_ids),
            "islands": [{key: island.get(key) for key in ("id", "status", "reason", "max_mismatch_mva",
                                                             "jacobian_condition", "worst_mismatch_node")}
                        for island in result.get("islands", ())]}


def _trend_evidence(homotopy):
    """Summarize numerical evidence beyond load relief.

An admittance path does not establish a physical limit. It supports a hypothesis
only when voltage consistently decreases, sensitivity rises near the last point
and another initial guess reaches the same operating point.
"""
    solved = [row for row in homotopy if row["admittance_fraction"] > 0
              and row["energized_status"]["ok"] and (row.get("minimum_voltage") or {}).get("u_pu") is not None]
    solved.sort(key=lambda row: row["admittance_fraction"], reverse=True)
    voltages = [row["minimum_voltage"]["u_pu"] for row in solved]
    sensitivities = []
    for previous, current in zip(solved, solved[1:]):
        delta_fraction = previous["admittance_fraction"] - current["admittance_fraction"]
        sensitivities.append((previous["minimum_voltage"]["u_pu"] - current["minimum_voltage"]["u_pu"])
                             / delta_fraction)
    monotonic = len(voltages) >= 3 and all(next_value <= value + 1e-9
                                           for value, next_value in zip(voltages, voltages[1:]))
    sensitivity_growing = (len(sensitivities) >= 2 and sensitivities[-1] > sensitivities[0] * 1.2
                            and sensitivities[-1] >= sensitivities[-2])
    repeat = solved[-1].get("cold_start_repeat") if solved else None
    repeat_u = (repeat or {}).get("minimum_voltage_pu")
    repeated = bool(repeat and repeat.get("energized_ok") and repeat_u is not None
                    and abs(repeat_u - voltages[-1]) <= 1e-7)
    return {"converged_points": len(solved), "voltage_by_fraction": [
                {"admittance_fraction": row["admittance_fraction"], "minimum_u_pu": row["minimum_voltage"]["u_pu"]}
                for row in solved], "d_u_d_removed_admittance_pu": sensitivities,
            "voltage_monotonic_nonincreasing": monotonic,
            "sensitivity_growing_near_limit": sensitivity_growing,
            "cold_start_repeat_agrees": repeated}


def _classification(homotopy, reductions):
    full = next(step for step in homotopy if step["admittance_fraction"] == 0.0)
    converged_reductions = [step for step in reductions if step["energized_status"]["ok"]]
    trend = _trend_evidence(homotopy)
    if full["energized_status"]["ok"]:
        return "indeterminado", "The full outage converges with homotopy. No convergence failure remains to classify."
    if converged_reductions:
        minimum = min(step["load_reduction_fraction"] for step in converged_reductions)
        if (trend["voltage_monotonic_nonincreasing"] and trend["sensitivity_growing_near_limit"]
                and trend["cold_start_repeat_agrees"]):
            return "probable_physical_limit", (
                'The outage converges after reducing internal demand by %.0f %%. Homotopy shows monotonic voltage decrease, growing sensitivity and a consistent cold-start repeat. This supports a transfer or voltage limit hypothesis without proving a physical bifurcation.' % (minimum * 100))
        missing = []
        if not trend["voltage_monotonic_nonincreasing"]:
            missing.append("monotonic voltage decrease")
        if not trend["sensitivity_growing_near_limit"]:
            missing.append("increasing sensitivity near the limit")
        if not trend["cold_start_repeat_agrees"]:
            missing.append("consistent cold-start repetition")
        return "indeterminado", (
            'Relief of %.0f %% restores convergence, but converging evidence for %s is missing. Isolated load relief does not distinguish a physical limit from numerical difficulty.' % (minimum * 100, ", ".join(missing)))
    intermediate = [step for step in homotopy if step["admittance_fraction"] > 0 and step["energized_status"]["ok"]]
    if intermediate:
        lowest = min(step["admittance_fraction"] for step in intermediate)
        limiting = next(step for step in intermediate if step["admittance_fraction"] == lowest)
        minimum = (limiting.get("minimum_voltage") or {}).get("u_pu")
        if (lowest >= 0.05 and minimum is not None and minimum >= 0.9
                and not limiting.get("q_limited_generators")):
            return "probable_numerical_problem", (
                'The path fails below 5 %% admittance without marked voltage drop or Q saturation, and load relief does not restore the outage. Evidence points to conditioning or the equivalent model.')
        return "indeterminado", (
            'The path converges up to %.0f %% admittance, but the outage and requested load reductions do not converge. PF comparison or robust continuation is needed.' % (lowest * 100))
    return "probable_numerical_problem", (
        'Neither reduced admittance nor the outage with requested load reductions converges. Evidence points to conditioning or equivalent-model limits and does not prove physical collapse.')


def diagnose_outage(model, branch_id, *, fractions=FRACTIONS, load_reductions=LOAD_REDUCTIONS,
                    options=None, provenance=None):
    "Diagnose an ElmLne outage using admittance homotopy and internal load relief."
    fractions = tuple(_valid_fraction(value, "fraction") for value in fractions)
    reductions = tuple(_valid_fraction(value, "load_reduction") for value in load_reductions)
    if fractions != tuple(sorted(set(fractions), reverse=True)) or 0.0 not in fractions:
        raise ValueError('[invalid_input] ' + "fractions must be descending, unique and include zero")
    if reductions != tuple(sorted(set(reductions))):
        raise ValueError('[invalid_input] ' + "load_reductions must be ascending and unique")
    source = _index(model).get(branch_id)
    if source is None or source.get("class") != "ElmLne":
        raise ValueError('[invalid_input] ' + "branch_id must identify an ElmLne")
    effective_options = {"q_limits": True, "tolerance_pu": 1e-8, "max_iter": 30, **(options or {})}
    parallel_ids = _parallel_branch_ids(model, branch_id)
    homotopy, last_solved, last_fraction = [], None, None
    for fraction in fractions:
        work = scale_line_admittance(model, branch_id, fraction)
        result = _solve(work, branch_id, previous=last_solved, options=effective_options)
        step = _observation(work, result, branch_id, parallel_ids, fraction=fraction, warm_fraction=last_fraction)
        homotopy.append(step)
        if step["energized_status"]["ok"]:
            last_solved, last_fraction = result, fraction
    solved_steps = [row for row in homotopy if row["admittance_fraction"] > 0 and row["energized_status"]["ok"]]
    if solved_steps:
        limiting = min(solved_steps, key=lambda row: row["admittance_fraction"])
        repeat_model = scale_line_admittance(model, branch_id, limiting["admittance_fraction"])
        repeat_result = _solve(repeat_model, branch_id, previous=None, options=effective_options)
        repeat_minimum = _minimum_voltage(repeat_result, _index(repeat_model))
        limiting["cold_start_repeat"] = {"energized_ok": energized_status(repeat_result)["ok"],
                                          "minimum_voltage_pu": (repeat_minimum or {}).get("u_pu"),
                                          "max_mismatch_mva": repeat_result.get("max_mismatch_mva"),
                                          "iterations": repeat_result.get("iterations"),
                                          "warm_start": False}
    load_steps, previous, previous_reduction = [], None, None
    for reduction in sorted(reductions, reverse=True):
        work = scale_line_admittance(model, branch_id, 0.0)
        work, load_info = reduce_internal_positive_loads(work, reduction)
        result = _solve(work, branch_id, previous=previous, options=effective_options)
        step = _observation(work, result, branch_id, parallel_ids, fraction=0.0, reduction=reduction,
                            warm_fraction=previous_reduction)
        step["internal_load"] = load_info
        load_steps.append(step)
        if step["energized_status"]["ok"]:
            previous, previous_reduction = result, reduction
    load_steps.sort(key=lambda row: row["load_reduction_fraction"])
    classification, evidence = _classification(homotopy, load_steps)
    return {"schema": "power_machine-n1-diagnostic-v1", "validation_status": "not validated against PowerFactory",
            "outage": _identity(source), "parallel_branch_ids": parallel_ids,
            "model_sha256": model_sha256(model), "provenance": copy.deepcopy(provenance or {}),
            "options": effective_options,
            "homotopy": homotopy, "load_reductions": load_steps,
            "trend_evidence": _trend_evidence(homotopy),
            "conclusion": {"classification": classification, "evidence": evidence}}


def write_diagnosis(diagnoses, directory):
    "Write JSON and summary CSV without overwriting an existing destination."
    directory = Path(directory)
    if directory.exists():
        raise FileExistsError(f"Destino existente: {directory}")
    directory.mkdir(parents=True)
    (directory / "diagnostico_n1.json").write_text(
        json.dumps(diagnoses, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    fields = ("outage_id", "outage_name", "phase", "admittance_fraction", "load_reduction_fraction",
              "energized_converged", "max_mismatch_mva", "minimum_u_pu", "minimum_bus_id",
              "q_limited_count", "classification")
    with (directory / "diagnostico_n1_resumen.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for diagnosis in diagnoses:
            outage = diagnosis["outage"]
            for phase, rows in (("homotopy", diagnosis["homotopy"]), ("load_reduction", diagnosis["load_reductions"])):
                for row in rows:
                    minimum = row.get("minimum_voltage") or {}
                    writer.writerow({"outage_id": outage["model_id"], "outage_name": outage["name"],
                                     "phase": phase, "admittance_fraction": row.get("admittance_fraction"),
                                     "load_reduction_fraction": row.get("load_reduction_fraction"),
                                     "energized_converged": row["energized_status"]["ok"],
                                     "max_mismatch_mva": row.get("max_mismatch_mva"),
                                     "minimum_u_pu": minimum.get("u_pu"),
                                     "minimum_bus_id": minimum.get("model_id"),
                                     "q_limited_count": len(row.get("q_limited_generators", ())),
                                     "classification": diagnosis["conclusion"]["classification"]})
    return directory / "diagnostico_n1.json", directory / "diagnostico_n1_resumen.csv"
