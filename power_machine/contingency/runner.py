"Explicit data, numerical methods and traceable results for contingency studies."
from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from ..elements import MissingParameter
from ..loadflow import _scenario, run_loadflow
from ..compiled import compile_network, scenario_view
from .catalog import case_id, enumerate_cases, enumerate_outages, index_model, total_cases
from .metrics import collect_metrics, normalize_loadflow, energized_status
from .results import diagnostic, envelope, export_run, fingerprint, json_data
from .schema import BudgetExceeded, ContingencyCaseResult, ContingencyOptions
from .references import choose_references
from ..topology import build_topology

WORK_ROOT = Path(__file__).resolve().parents[2]


def materialize_scenario(model: dict, scenario: dict) -> dict:
    "Apply operating deltas before physical outages without modifying the source model."
    if not isinstance(scenario, dict):
        raise ValueError('[invalid_input] ' + "scenario must be a mapping")
    for field, changes in scenario.items():
        if not isinstance(changes, dict):
            raise ValueError('[invalid_input] ' + f"{field} requires a mapping by ID")
        for value in changes.values():
            if field in ("outserv", "closed"):
                if value not in (0, 1, False, True):
                    raise ValueError('[invalid_input] ' + f"{field} requires 0 or 1")
            elif field == "params" or (field == "tap_pos" and isinstance(value, dict)):
                json.dumps(value, allow_nan=False)
            elif not np.isfinite(float(value)) or (field == "load_scale" and float(value) < 0) or (field == "v_set_pu" and float(value) <= 0):
                raise ValueError('[invalid_input] ' + f"Invalid operating value: {field}")
    operational = dict(scenario_view(model, scenario))
    # El resto del motor exige todas las colecciones aunque estén vacías.
    for group in ("branches", "branches3", "switches", "gens", "loads", "shunts"):
        operational.setdefault(group, [])
    index_model(operational)
    s_base_mva = operational["meta"]["s_base_mva"]
    if not np.isfinite(float(s_base_mva)) or float(s_base_mva) <= 0:
        raise ValueError('[invalid_input] ' + "s_base_mva must be positive and finite")
    return operational


def loadflow_adapter(model: dict, scenario: dict, ldf_options: dict) -> dict:
    "Normalize the existing load-flow engine for contingency study consumers."
    return normalize_loadflow(run_loadflow(model, scenario=scenario, options=ldf_options), model)


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _nonfinite(value):
    if isinstance(value, dict):
        return any(_nonfinite(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_nonfinite(v) for v in value)
    if isinstance(value, (float, complex, np.number)):
        return not np.isfinite(value)
    return False


def _engine_nonfinite(raw):
    # The built-in engine returns scalar electrical tables, not arbitrary trees.
    # One NumPy finite check avoids serializing all row names and paths per case.
    scalar_types = (float, complex, np.number)
    values = [v for v in raw.values() if isinstance(v, scalar_types)]
    values.extend(v for v in raw.get('totals', {}).values() if isinstance(v, scalar_types))
    for group in ('islands', 'buses', 'branches', 'gens', 'loads', 'diagnostics'):
        values.extend(v for row in raw.get(group, ()) for v in row.values()
                      if isinstance(v, scalar_types))
    return bool(values) and not bool(np.all(np.isfinite(values)))


def _outage_record(outage):
    return {**asdict(outage), 'member_ids':list(outage.member_ids)}


def _case_record(case, fast):
    if not fast:
        return json_data(case)
    # The engine owns its result rows. Copy only topology lists borrowed by metrics.
    metrics = case.metrics
    if 'islands' in metrics:
        metrics['islands'] = [{**row, 'bus_ids':list(row['bus_ids']),
                              'load_ids':list(row['load_ids'])} for row in metrics['islands']]
    try:
        json.dumps(metrics, allow_nan=False)
    except (TypeError, ValueError, OverflowError):
        return json_data(case)
    return {'case_id':case.case_id, 'outages':[_outage_record(outage) for outage in case.outages],
            'status':case.status, 'metrics':metrics, 'violations':list(case.violations),
            'loadflow':case.loadflow}


def _solve_case(model, outages, options, ldf_options, service, base=None, *, prepared=None, base_result=None):
    delta = {"outserv":{}, "closed":{}}
    index = index_model(model)
    fast = prepared is not None and service is loadflow_adapter
    work = model if fast else copy.deepcopy(model)
    if not fast: index = index_model(work)
    retired_refs = []
    for outage in outages:
        for key in outage.physical_ids:
            item = index[key]
            if item.get("ref"):
                retired_refs.append(key)
            if item.get("class") in ("ElmCoup", "StaSwitch"):
                # Un StaSwitch fuera de servicio sería ignorado por topología.
                if fast: delta["closed"][key] = False
                else: item["closed"] = False
            else:
                if fast: delta["outserv"][key] = 1
                else: item["outserv"] = 1
    if fast: work = prepared.scenario(delta)
    reference_assignments, reference_diagnostics = [], []
    topology_hash = fingerprint({"model_sha256":prepared.fingerprint,"scenario_delta":delta,"ldf_options":ldf_options}) if fast else fingerprint({"model":work,"scenario":{},"ldf_options":ldf_options})
    cid = case_id(outages) if outages else "BASE"
    try:
        if not isinstance(model, dict):
            raise ValueError('[invalid_input] ' + "model must be a mapping")
        if getattr(options, 'island_reference', 'none') == 'largest_generator':
            topology = prepared.state(work, delta, ldf_options)['topology'] if fast else build_topology(work)
            reference_changes, reference_assignments, reference_diagnostics = choose_references(topology, index_model(work))
            if reference_changes:
                if fast:
                    delta['ref'] = reference_changes
                    work = prepared.scenario(delta)
                else:
                    changed_index = index_model(work)
                    for ident, enabled in reference_changes.items():changed_index[ident]['ref'] = enabled
            topology_hash = fingerprint({"model_sha256":prepared.fingerprint,"scenario_delta":delta,"ldf_options":ldf_options}) if fast else fingerprint({"model":work,"scenario":{},"ldf_options":ldf_options})
        topology = None
        if fast:
            solve_options = dict(ldf_options)
            if base_result is not None and solve_options.get("warm_start", True):
                solve_options["base_result"] = base_result
                solve_options["warm_start"] = True
            if not outages and delta.get("ref"):
                solve_options["warm_start"] = False
            raw = run_loadflow(prepared, delta if outages or delta.get("ref") else None, solve_options)
            topology = prepared.state(work, delta, solve_options)["topology"]
        else:
            raw = service(copy.deepcopy(work), {}, copy.deepcopy(ldf_options))
        if not isinstance(raw, dict) or type(raw.get("converged")) is not bool:
            raise ValueError('[invalid_input] ' + "The load-flow service must return a mapping with a boolean converged field")
        try:
            if (_engine_nonfinite(raw) if fast else _nonfinite(raw)):
                raise FloatingPointError("The load-flow service returned nonfinite quantities")
        except (ValueError, OverflowError) as exc:
            raise FloatingPointError("The load-flow service returned nonfinite quantities") from exc
        normalized = normalize_loadflow(raw, work)
        normalized["energized_status"] = energized_status(normalized)
        metrics, violations, diagnostics, status = collect_metrics(
            work, normalized, options, base=base, retired_reference_ids=retired_refs, topology=topology, base_result=base_result)
        if getattr(options, 'island_reference', 'none') == 'largest_generator':
            metrics['reference_assignments'] = reference_assignments
            diagnostics.extend(reference_diagnostics)
            normalized['all_islands_converged'] = normalized['converged']
            normalized['energized_status'] = metrics['energized_status']
            generator_islands = {i['island_id'] for i in metrics['islands'] if i['status'] == 'generation_disconnected'}
            for row in normalized.get('islands', ()):
                if row['id'] in generator_islands:row['status_code'] = 'generation_disconnected'
            disconnected_buses = set(metrics['disconnected_generator_bus_ids'])
            for row in normalized.get('buses', ()):
                if row['id'] in disconnected_buses:row['status_code'] = 'generation_disconnected'
            for row in normalized.get('gens', ()):
                if row['id'] in metrics['disconnected_generator_ids']:row['status_code'] = 'generation_disconnected'
            normalized['converged'] = metrics['energized_status']['ok']
        metrics["topology_sha256"] = topology_hash
        return ContingencyCaseResult(cid, outages, status, metrics, violations, normalized), diagnostics
    except (np.linalg.LinAlgError, ArithmeticError) as exc:
        code, status = "numerical_failure", "not_converged"
        error_message = str(exc)
    except (MissingParameter, ValueError, KeyError, TypeError) as exc:
        code, status = "invalid_input", "invalid_input"
        error_message = str(exc)
    except Exception as exc:
        code, status = "solver_failure", "not_converged"
        error_message = f"{type(exc).__name__}: {exc}"
    # La excepción se conserva fuera del bloque except mediante el diagnóstico.
    return ContingencyCaseResult(cid, outages, status, {"topology_sha256": topology_hash}, (), None), [
        diagnostic(code, error_message, level="error")]


def run_contingencies(model: dict, scenario: dict, options: ContingencyOptions, *,
                      loadflow_service=loadflow_adapter, cancel=None, progress=None,
                      groups=None, selected_ids=None, filter_reason=None,
                      ldf_options=None, study=None, variant_sha256=None) -> dict:
    """Enumerate requested physical outages with a budget and cooperative cancellation.

    Reuse preparation and the base solution. largest_generator assigns eligible
    references to loaded islands and separates generator-only islands. none keeps
    the historical reference and metric policy. External solvers receive isolated
    inputs. Fingerprints include the effective model and LF options.
    """
    effective_options = {"q_limits": options.q_limits, **copy.deepcopy(ldf_options or {})}
    effective_options["q_limits"] = options.q_limits
    try:
        if not isinstance(model, dict):
            raise ValueError('[invalid_input] ' + "model must be a mapping")
        if effective_options.get("overrides"):
            raise ValueError('[invalid_input] ' + "Materialize overrides before running contingencies")
        result = envelope(model, scenario, options, study=study,
                          variant_sha256=variant_sha256, ldf_options=effective_options)
        operational = materialize_scenario(model, scenario)
        catalog = enumerate_outages(operational, groups=groups)
        selected = catalog
        excluded = []
        if selected_ids is not None:
            requested = set(selected_ids)
            unknown = requested - {o.object_id for o in catalog}
            if unknown:
                raise ValueError('[invalid_input] ' + f"Unknown filter IDs: {sorted(unknown)}")
            if not filter_reason or not isinstance(filter_reason, str):
                raise ValueError('[invalid_input] ' + "An explicit filter requires a reason")
            selected = tuple(o for o in catalog if o.object_id in requested)
            excluded = [{**_outage_record(o), "reason": filter_reason} for o in catalog if o.object_id not in requested]
        payload = result["payload"]
        expected = total_cases(len(selected), options)
        payload.update(catalog=[_outage_record(o) for o in catalog], groups=json_data(groups or []), excluded=excluded,
                       filter_reason=filter_reason, exhaustive=False, enumeration_complete=False,
                       total_expected=expected, total_unfiltered=total_cases(len(catalog), options),
                       completed_count=0, cases=[], base=None, base_unresolved_islands=[],
                       missing_case_ids=[], materialized_model_sha256=fingerprint(operational),
                       configuration_sha256=fingerprint({"model": operational, "scenario": scenario,
                                                         "options": asdict(options), "ldf_options": effective_options,
                                                         "variant_sha256": variant_sha256,
                                                         "groups": json_data(groups or []),
                                                         "selected_ids": sorted(selected_ids) if selected_ids is not None else None}),
                       coverage={"catalog_ids": [o.object_id for o in catalog]})
        physical_index = index_model(operational)
        payload["physical_members"] = [{"group_id": o.object_id, "member_id": key,
                                         "object_id": physical_index[key].get("object_id", key),
                                         "relative_path": physical_index[key].get("relative_path", physical_index[key].get("path")),
                                         "kind": physical_index[key]["class"],
                                         "n_units": physical_index[key].get("n_units"),
                                         "nlnum": physical_index[key].get("params", {}).get("nlnum")}
                                        for o in catalog for key in o.physical_ids]
        result["provenance"]["inputs"] = [
            {"path": None, "kind": "model_snapshot", "sha256": result["model_sha256"]},
            {"path": None, "kind": "study_snapshot", "sha256": result["study_sha256"]},
            {"path": None, "kind": "scenario_snapshot", "sha256": fingerprint(scenario)}]
        cases = enumerate_cases(selected, options)
        payload["enumeration_complete"] = True
        payload["missing_case_ids"] = [case_id(case) for case in cases]
        def emit(phase):
            if progress:
                progress({"phase": phase, "completed": len(payload["cases"]), "total": expected,
                          "fraction": len(payload["cases"]) / expected if expected else 1.0})
        emit("base")
        if _cancelled(cancel):
            result["status"] = "cancelled"
            return json_data(result)
        prepared = compile_network(operational, backend=effective_options.get("backend", "auto")) if loadflow_service is loadflow_adapter else None
        base_case, diagnostics = _solve_case(operational, (), options, effective_options, loadflow_service, prepared=prepared)
        payload["base"] = _case_record(base_case, prepared is not None)
        result["diagnostics"].extend(diagnostics)
        payload["base_unresolved_islands"] = [i for i in base_case.metrics.get("islands", []) if i["status"] not in ("solved", "generation_disconnected")]
        payload["base_energized_status"] = base_case.metrics.get("energized_status")
        if base_case.loadflow is None:
            result["status"] = "invalid_input" if base_case.status == "invalid_input" else "numerical_failure"
            return json_data(result)
        for outages in cases:
            if _cancelled(cancel):
                result["status"] = "cancelled"
                break
            case, diagnostics = _solve_case(operational, outages, options, effective_options,
                                            loadflow_service, base_case.metrics, prepared=prepared, base_result=base_case.loadflow)
            payload["cases"].append(_case_record(case, prepared is not None))
            for row in diagnostics:
                row["case_id"] = case.case_id
            result["diagnostics"].extend(diagnostics)
            payload["completed_count"] = len(payload["cases"])
            emit("cases")
            if case.status in ("invalid_input", "not_converged") and not options.continue_on_failure:
                result["status"] = "partial"
                break
        if _cancelled(cancel):
            result["status"] = "cancelled"
        done = {c["case_id"] for c in payload["cases"]}
        payload["missing_case_ids"] = [case_id(case) for case in cases if case_id(case) not in done]
        payload["exhaustive"] = not excluded and selected_ids is None and not payload["missing_case_ids"] and result["status"] != "cancelled"
        all_cases = [payload["base"], *payload["cases"]]
        payload["coverage"].update(
            selected_ids=[o.object_id for o in selected],
            rating_missing_ids=sorted({key for c in all_cases for key in c["metrics"].get("rating_missing_ids", [])}),
            q_limits_missing_ids=sorted({key for c in all_cases for key in c["metrics"].get("q_limits_missing_ids", [])}),
            unknown_supply_load_ids=sorted({key for c in all_cases for key in c["metrics"].get("unknown_supply_load_ids", [])}),
            omitted_branch_ids=sorted({key for c in all_cases if c["loadflow"] for key in c["loadflow"].get("omitted_branches", [])}))
        payload["alert_order"] = [c["case_id"] for c in sorted(payload["cases"], key=lambda c: (
            c["metrics"].get("alert_priority", 2), -c["metrics"].get("newly_unsupplied_p_mw", 0),
            -c["metrics"].get("severity", 0), c["case_id"]))]
        if result["status"] == "completed" and (payload["missing_case_ids"] or excluded
                or any(c["status"] not in ("completed", "limit_violation") for c in all_cases)
                or not payload["base_energized_status"]["ok"] or payload["coverage"]["rating_missing_ids"]
                or payload["coverage"]["q_limits_missing_ids"] or payload["coverage"]["omitted_branch_ids"]):
            result["status"] = "partial"
        emit("done")
        # Default-engine records were independently sanitized as each case finished.
        # Re-encoding the whole tree repeats the largest materialization in N-1.
        return result if prepared is not None else json_data(result)
    except BudgetExceeded as exc:
        result["status"] = "invalid_input"
        result["diagnostics"].append(diagnostic("budget_exceeded", exc, level="error"))
        result["payload"]["missing_case_ids_not_enumerated"] = True
        return json_data(result)
    except (ValueError, KeyError, TypeError) as exc:
        if "result" not in locals():
            result = envelope({}, {}, ContingencyOptions())
            result["model_sha256"] = None
        result["status"] = "invalid_input"
        result["diagnostics"].append(diagnostic("invalid_input", exc, level="error"))
        return json_data(result)


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    "Execute a serialized study request after validating snapshot schemas and hashes.\n\nNo GUI is imported and results are persisted only when explicitly requested."
    try:
        if not isinstance(request, dict):
            raise ValueError('[invalid_input] ' + "request must be a mapping")
        if not str(request.get("schema")).endswith("-parity-request-v1") or request.get("module") != "contingency":
            raise ValueError('[invalid_input] ' + "Incompatible request schema or module")
        root = Path(request["root"]).resolve()
        if root == Path(root.anchor):
            raise ValueError('[invalid_input] ' + "The root must be a study directory")
        snapshot = copy.deepcopy(request["snapshot"])
        if not isinstance(snapshot, dict):
            raise ValueError('[invalid_input] ' + "snapshot must be a mapping")
        model, study = snapshot["model"], snapshot["study"]
        if not isinstance(model, dict) or not isinstance(study, dict):
            raise ValueError('[invalid_input] ' + "Model and study must be mappings")
        hashes = snapshot.get("fingerprints", {})
        if not isinstance(hashes, dict):
            raise ValueError('[invalid_input] ' + "fingerprints must be a mapping")
        for name, value in (("model_sha256", model), ("study_sha256", study)):
            if hashes.get(name) != fingerprint(value):
                raise ValueError('[invalid_input] ' + f"Fingerprint mismatch: {name}")
        inputs = request.get("input", {})
        if not isinstance(inputs, dict):
            raise ValueError('[invalid_input] ' + "input must be a mapping")
        target = None
        if inputs.get("output_dir") is not None:
            target = (root / inputs["output_dir"]).resolve()
            if not target.is_relative_to(root):
                raise ValueError('[invalid_input] ' + "Output is outside the study root")
        if snapshot.get("variant") is not None and hashes.get("variant_sha256") != fingerprint(snapshot["variant"]):
            raise ValueError('[invalid_input] ' + "Fingerprint mismatch: variant_sha256")
        options = ContingencyOptions(**request.get("options", {}))
        result = run_contingencies(model, study.get("scenario", {}), options,
                                  cancel=cancel, progress=progress, study=study,
                                  variant_sha256=hashes.get("variant_sha256"),
                                  ldf_options=study.get("ldf_options", {}), groups=inputs.get("groups"),
                                  selected_ids=inputs.get("selected_ids"), filter_reason=inputs.get("filter_reason"))
        if target is not None:
            result["payload"]["exports"] = [str(path.relative_to(root)) for path in export_run(result, target, root=root)]
        return result
    except (ValueError, KeyError, TypeError, OSError) as exc:
        result = envelope({}, {}, ContingencyOptions())
        result["status"] = "invalid_input"
        result["model_sha256"] = None
        result["diagnostics"].append(diagnostic("invalid_request", exc, level="error"))
        return result
