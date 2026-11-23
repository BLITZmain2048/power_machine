"Explicit data, numerical methods and traceable results for sc sweep studies."
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
from pathlib import Path
from power_machine.resources import output_root

from power_machine.shortcircuit import execute_request as execute_shortcircuit
from power_machine.shortcircuit.data import from_dict
from power_machine.shortcircuit.results import confined_path, file_sha256, fingerprint
from .partial import (compare_pf_rows, contract_ideal_for_solver, load_pf_sc_export,
                           prepare_partial, transformer_phase_loops)


REQUEST_SCHEMA = "power_machine-sc-sweep-request-v1"
RESULT_SCHEMA = "power_machine-parity-result-v1"
MODULE_ID = "sc_sweep"


def _cancelled(cancel):
    return cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else bool(cancel()))


def _default_supplemental_path(model):
    """Known local path without inferred electrical parameters."""
    if model.get("meta", {}).get("benchmark") == "ieee39":
        return "cases/ieee39/results/CC/supplemental_data.json"
    raise ValueError('[invalid_input] ' + "Provide supplemental or supplemental_path")


def _source_document(root, request, model):
    source = request.get("input", {})
    if "supplemental" in source:
        document = copy.deepcopy(source["supplemental"])
        if fingerprint(document) != source.get("supplemental_sha256"):
            raise ValueError('[invalid_input] ' + "Inline short-circuit supplement hash mismatch")
        from_dict(document)
        return document, "snapshot/input/supplemental", source["supplemental_sha256"]
    relative = source.get("supplemental_path", _default_supplemental_path(model))
    if "supplemental_path" not in source and model.get("meta", {}).get("benchmark") == "ieee39":
        from ..resources import required_readonly_path
        path = required_readonly_path("cases", "ieee39", "results", "CC", "supplemental_data.json")
    else:
        path = confined_path(root, relative)
    if not path.is_file():
        raise ValueError('[invalid_input] ' + "The short-circuit supplement must be a regular JSON file within the study root")
    sha256 = file_sha256(path)
    wanted = source.get("supplemental_sha256")
    if wanted is not None and wanted != sha256:
        raise ValueError('[invalid_input] ' + "Short-circuit supplement hash mismatch")
    document = json.loads(path.read_text(encoding="utf-8-sig"))
    from_dict(document)
    return document, relative, sha256


def _bus_label(bus):
    return {"bus_id": bus["id"], "object_id": bus.get("object_id", bus["id"]),
            "name": bus.get("name", bus["id"]), "substation": bus.get("substation", bus.get("scope", "")),
            "un_kv": bus.get("un_kv"), "relative_path": bus.get("relative_path", bus.get("path"))}


def _complex_parts(value):
    if isinstance(value, dict):
        return float(value.get("real", 0.0)), float(value.get("imag", 0.0))
    return None, None


def _row(bus, response, include_contributions):
    payload = response.get("payload", {})
    zth = payload.get("z_th_ohm") or {}
    z1 = zth.get("1")
    r1, x1 = _complex_parts(z1)
    ik = payload.get("Ik_initial_sym_ka")
    sk = None if ik is None or bus.get("un_kv") is None else math.sqrt(3) * float(bus["un_kv"]) * ik
    raw_status = response.get("status", "error")
    row_status = raw_status
    if payload.get("energization") == "deenergized":
        evaluation = "not_evaluable"
        row_status = "unsupported"
    elif raw_status in ("completed", "partial"):
        evaluation = raw_status
    elif any(d.get("code") == "missing_sc_data" for d in response.get("diagnostics", ())):
        evaluation = "not_evaluable"
    else:
        evaluation = raw_status
    row = {**_bus_label(bus), "status": row_status, "evaluation_status": evaluation,
           "ik_initial_sym_ka": ik, "ip_peak_ka": payload.get("ip_peak_ka"),
           "sk_initial_mva": sk, "r_x": None if x1 in (None, 0.0) or r1 is None else r1 / x1,
           "z1_r_ohm": r1, "z1_x_ohm": x1,
           "diagnostics": response.get("diagnostics", [])}
    if include_contributions:
        row["contributions"] = payload.get("contributions", [])
    return row


def _inventory(model, supplemental, rows):
    """Coverage by source and bus without selectively excluding contributions."""
    source_records = supplemental.get("sources", {})
    sources = []
    for item in model.get("gens", ()):
        record = source_records.get(item["id"])
        kind = record.get("kind") if isinstance(record, dict) else None
        if kind == "current_limited":
            status, reason = "unsupported", "Current-limited converter injection is not implemented in shortcircuit"
        elif record is None:
            status, reason = "not_evaluable", "Short-circuit source model with impedance and provenance is missing"
        else:
            status, reason = "declared", "Source declared in the supplement. Its contribution depends on the fault"
        sources.append({"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                        "name": item.get("name", item["id"]), "class": item.get("class"), "bus_id": item.get("bus"),
                        "source_kind": kind, "coverage_status": status, "reason": reason})
    buses = [{"bus_id": row["bus_id"], "object_id": row["object_id"], "relative_path": row["relative_path"],
              "coverage_status": row["evaluation_status"], "status": row["status"],
              "reason": [d.get("message") for d in row["diagnostics"] if d.get("level") == "error"]}
             for row in rows]
    return {"sources": sources, "buses": buses}


def _status(rows):
    statuses = {row["status"] for row in rows}
    calculated = {"completed", "partial"}
    if rows and all(row.get("evaluation_status") == "not_evaluable" for row in rows):
        return "unsupported"
    if any(row.get("evaluation_status") not in {"completed"} for row in rows):
        return "partial" if rows else "invalid_input"
    if statuses and statuses <= {"completed"}:
        return "completed"
    if statuses & calculated:
        return "partial"
    if statuses and statuses <= {"unsupported", "invalid_input"}:
        return "unsupported"
    return "partial" if rows else "invalid_input"


def _reference_ieee39(root, rows):
    """Indicative CC-01 comparison without claiming case validation."""
    path = output_root() / "comparison_results/ieee39_pf/E39-CC-01/buses.csv"
    if not path.is_file():
        return {"status": "unavailable"}
    reference = {}
    with path.open(encoding="utf-8", newline="") as stream:
        for item in csv.DictReader(stream):
            raw = item.get("fault_bus", "")
            try:
                reference[f"B{int(raw):02d}"] = float(item["pf_Ikss_kA"])
            except (TypeError, ValueError):
                continue
    differences = []
    for row in rows:
        actual, expected = row.get("ik_initial_sym_ka"), reference.get(row["bus_id"])
        if actual is not None and expected not in (None, 0.0):
            differences.append({"bus_id": row["bus_id"], "pf_ikss_ka": expected,
                                "sweep_ik_initial_ka": actual,
                                "relative_error": abs(actual - expected) / abs(expected)})
    return {"status": "comparison_available" if differences else "no_comparable_rows",
            "source_path": str(path.relative_to(root)), "rows": differences,
            "note": "Local regression comparison, not PowerFactory validation of the case"}


def run_sweep(model, study, options, supplemental, *, selected_bus_ids=None,
              include_contributions=False, cancel=None, progress=None, root=None,
              blocked_bus_ids=None, blocked_bus_reasons=None, fault_bus_aliases=None, display_buses=None,
              thevenin_host_identity_bus_ids=None):
    "Solve one fault per selected bus and retain missing values as null.\n\nUnchanged sequence networks share factorizations. Generator-transformer\ncorrections use separate matrices whenever the fault changes the profile."
    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    active = [bus for bus in (display_buses if display_buses is not None else model.get("buses", []))
              if bus.get("outserv", 0) != 1]
    available = {bus["id"] for bus in active}
    selected = list(selected_bus_ids or [bus["id"] for bus in active])
    unknown = [bus_id for bus_id in selected if bus_id not in available]
    if unknown:
        raise ValueError('[invalid_input] ' + "Selected bus is unknown or out of service: " + ", ".join(unknown))
    by_id = {bus["id"]: bus for bus in active}
    blocked = set(blocked_bus_ids or ())
    from ..shortcircuit import FaultSpec, ShortCircuitOptions
    from ..shortcircuit.solver import _run_shortcircuit
    from ..shortcircuit.compiled import ShortCircuitWorkspace
    from ..shortcircuit.results import json_value
    from ..shortcircuit import _apply_scenario
    fast = not options.get("normative_evidence")
    if fast:
        electrical_model = _apply_scenario(model, study.get("scenario", {}))
        data = from_dict(supplemental)
        sc_options = ShortCircuitOptions(**{k:v for k,v in options.items() if k not in ("fault_kind","r_fault_ohm","x_fault_ohm")})
        workspace = ShortCircuitWorkspace(electrical_model,data,sc_options,options.get("fault_kind","3ph"))
    rows = []
    for index, bus_id in enumerate(selected, start=1):
        if _cancelled(cancel):
            return {"status": "cancelled", "rows": rows}
        if bus_id in blocked:
            reasons = list((blocked_bus_reasons or {}).get(bus_id, ()))
            detail = ""
            if reasons:
                detail = " Missing data: " + " | ".join(reasons)
            rows.append({**_bus_label(by_id[bus_id]), "status": "unsupported", "evaluation_status": "not_evaluable",
                         "ik_initial_sym_ka": None, "ip_peak_ka": None, "sk_initial_mva": None,
                         "r_x": None, "z1_r_ohm": None, "z1_x_ohm": None,
                         "diagnostics": [{"code": "unsupported_three_winding_route", "level": "error",
                                          "message": "The bus route requires an ElmTr3 that cannot be derived for the short-circuit solver" + detail}]})
            if progress:
                progress({"completed": index, "total": len(selected), "bus_id": bus_id})
            continue
        fault_bus_id = (fault_bus_aliases or {}).get(bus_id, bus_id)
        request_model, request_supplemental = model, supplemental
        if fast:
            fault = FaultSpec(fault_bus_id, options.get("fault_kind","3ph"),
                              options.get("r_fault_ohm",0.0), options.get("x_fault_ohm",0.0))
            result = _run_shortcircuit(electrical_model,fault,sc_options,data,workspace=workspace,cancel=cancel)
            payload = json_value({key:result.quantities.get(key) for key in
                                  ("z_th_ohm", "Ik_initial_sym_ka", "ip_peak_ka", "energization")})
            if include_contributions: payload["contributions"] = json_value(result.contributions)
            response = {"status":result.status,"payload":payload,"diagnostics":json_value(result.diagnostics)}
        else:
            request = {"schema": "power_machine-parity-request-v1", "module": "shortcircuit", "root": str(root),
                       "snapshot": {"model": request_model, "study": study,
                                    "fingerprints": {"model_sha256": fingerprint(request_model),
                                                     "study_sha256": fingerprint(study),
                                                     "variant_sha256": None}},
                       "options": copy.deepcopy(options),
                       "input": {"fault": {"bus_id": fault_bus_id, "kind": options.get("fault_kind", "3ph"),
                                            "r_fault_ohm": options.get("r_fault_ohm", 0.0),
                                            "x_fault_ohm": options.get("x_fault_ohm", 0.0)},
                                 "supplemental": request_supplemental,
                                 "supplemental_sha256": fingerprint(request_supplemental)}}
            request["options"].pop("fault_kind", None)
            request["options"].pop("r_fault_ohm", None)
            request["options"].pop("x_fault_ohm", None)
            response = execute_shortcircuit(request, cancel=cancel)
        rows.append(_row(by_id[bus_id], response, include_contributions))
        if progress:
            progress({"completed": index, "total": len(selected), "bus_id": bus_id})
    return {"status": _status(rows), "rows": rows}


def execute_request(request, *, cancel=None, progress=None):
    "Execute a serialized study request after validating snapshot schemas and hashes.\n\nNo GUI is imported and results are persisted only when explicitly requested."
    model, study, options, source_path, source_sha = {}, {}, {}, None, None
    try:
        request = copy.deepcopy(request)
        json.dumps(request, allow_nan=False)
        if request.get("schema") != REQUEST_SCHEMA or request.get("module") != MODULE_ID:
            raise ValueError('[invalid_input] ' + "Short-circuit sweeps require power_machine-sc-sweep-request-v1")
        root = Path(request["root"]).resolve()
        snapshot = request["snapshot"]
        model, study = snapshot["model"], snapshot["study"]
        hashes = snapshot["fingerprints"]
        if hashes.get("model_sha256") != fingerprint(model) or hashes.get("study_sha256") != fingerprint(study):
            raise ValueError('[invalid_input] ' + "Snapshot hash mismatch")
        options = copy.deepcopy(request["options"])
        input_data = request.get("input", {})
        boundary_coverage = None
        if input_data.get("boundary_mode") is not None:
            if options.get("fault_kind", "3ph") != "3ph":
                raise ValueError('[invalid_input] ' + "Partial short-circuit studies support three-phase faults only")
            calculation_model, supplemental, boundary_coverage = prepare_partial(
                model, mode=input_data["boundary_mode"], case=options.get("case", "maximum"),
                thevenin=input_data.get("thevenin"), bus_ids=input_data.get("bus_ids"),
                host_bus_id=input_data.get("host_bus_id"))
            export_folder = input_data.get("pf_export_folder")
            if export_folder:
                supplemental = load_pf_sc_export(confined_path(root, export_folder), supplemental)
            inventory_model, inventory_supplemental = calculation_model, supplemental
            calculation_model, solver_supplemental, aliases, nodes = contract_ideal_for_solver(calculation_model, supplemental)
            boundary_coverage["contracted_nodes"] = [{"node_bus_id": node_id, "member_bus_ids": bus_ids}
                                                   for node_id, bus_ids in sorted(nodes.items())]
            boundary_coverage["transformer_phase_loops"] = transformer_phase_loops(calculation_model, solver_supplemental)
            if boundary_coverage.get("mode") == "thevenin":
                host_node = aliases[boundary_coverage["host_bus_id"]]
                boundary_coverage["thevenin_host_fault"] = {
                    "bus_id": boundary_coverage["host_bus_id"], "node_bus_id": host_node,
                    "method": "complete network with nominal ElmTr2 ratio and vector group",
                    "sc_tap_treatment": "tap_pu preserves nominal ratio and nt2ag. phitr only orients the LF tap increment",
                    "reason": "The two-winding tap applies phitr to the tap increment, not as a fixed nominal-ratio phase shift"}
            source_path, source_sha = "derived/partial", fingerprint(supplemental)
        else:
            calculation_model = model
            supplemental, source_path, source_sha = _source_document(root, request, model)
            inventory_model, inventory_supplemental, solver_supplemental, aliases = calculation_model, supplemental, supplemental, None
        selected_bus_ids = request.get("input", {}).get("selected_bus_ids")
        if aliases is not None and selected_bus_ids is None:
            selected_bus_ids = [bus["id"] for bus in inventory_model.get("buses", ()) if bus.get("outserv", 0) != 1]
        result = run_sweep(calculation_model, study, options, solver_supplemental,
                           selected_bus_ids=selected_bus_ids,
                           include_contributions=bool(request.get("input", {}).get("include_contributions")),
                           cancel=cancel, progress=progress, root=root,
                           blocked_bus_ids=(boundary_coverage or {}).get("blocked_three_winding_bus_ids"),
                           blocked_bus_reasons=(boundary_coverage or {}).get("blocked_three_winding_reasons"),
                           fault_bus_aliases=aliases, display_buses=inventory_model.get("buses") if aliases is not None else None)
        rows = result["rows"]
        comparison = _reference_ieee39(root, rows) if model.get("meta", {}).get("benchmark") == "ieee39" else {
            "status": "no aplicable", "note": "power_machine not validated against PowerFactory"}
        if input_data.get("pf_reference_csv"):
            comparison = compare_pf_rows(rows, confined_path(root, input_data["pf_reference_csv"]))
        return {"schema": RESULT_SCHEMA, "module": MODULE_ID,
                "run_id": "scs_" + hashlib.sha256(fingerprint(request).encode()).hexdigest()[:16],
                "model_sha256": fingerprint(model), "study_sha256": fingerprint(study),
                "variant_sha256": hashes.get("variant_sha256"), "status": result["status"],
                "options": options,
                "units": {"ik_initial_sym_ka": "kA", "ip_peak_ka": "kA", "sk_initial_mva": "MVA",
                          "r_x": "1", "z1_r_ohm": "ohm", "z1_x_ohm": "ohm"},
                "provenance": {"inputs": [{"path": source_path, "sha256": source_sha, "role": "supplemental_sc"}],
                               "formulas": [{"standard": "IEC 60909-0", "edition": "2016",
                                             "engine": "power_machine.shortcircuit"}],
                               "pf_comparison": "not validated against PowerFactory", "comparison": comparison},
                "diagnostics": [], "payload": {"rows": rows, "inventory": _inventory(inventory_model, inventory_supplemental, rows),
                                                   "boundary_coverage": boundary_coverage, "total": len(rows),
                                                   "completed": sum(r["status"] == "completed" for r in rows),
                                                   "partial": sum(r["status"] == "partial" for r in rows)}}
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        return {"schema": RESULT_SCHEMA, "module": MODULE_ID, "run_id": "scs_error",
                "model_sha256": fingerprint(model), "study_sha256": fingerprint(study), "status": "invalid_input",
                "options": options, "units": {}, "provenance": {"pf_comparison": "not validated against PowerFactory"},
                "diagnostics": [{"code": "invalid_request", "level": "error", "message": str(exc)}],
                "payload": {"rows": [], "total": 0, "completed": 0, "partial": 0}}
