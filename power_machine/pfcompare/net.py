"Offline PF network-variant comparisons with explicit inputs and missing-reference states."
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import csv
import hashlib
import json
import math
from pathlib import Path
from ..resources import output_root, writable_root
from ..resources import readonly_path
import uuid

from ..benchmarks.ieee39_pf import DEFAULT_PF_DIR, LDF_OPTIONS
from ..loadflow import run_loadflow
from ..netedit import (VariantHistory, create_command, edit_command, new_variant,
                      materialize_variant, save_variant, load_variant, canonical_json)
from ..netedit.storage import safe_path
from .lf import canonical_reference_model, compare_loadflow, read_reference, number, write_comparison

WORKSPACE = output_root()
MODEL_PATH = readonly_path("cases", "ieee39_pf", "model.json")
OUTPUT_DIR = WORKSPACE / "comparison_results/ieee39_pf/E39-NET-01"
FIELDS = {
    "ElmLod": "bus1 typ_id outserv plini qlini scale0",
    "ElmLne": "bus1 bus2 typ_id outserv R1 X1 B1 G1 dline nlnum i_dist",
    "ElmSym": "bus1 typ_id outserv pgini qgini usetp ngnum ip_ctrl av_mode scale0 q_min q_max",
}


class RepresentationGap(ValueError):
    """Missing input or capability, reported without substituting electrical parameters."""


def read_description(case_dir):
    "Read exported input attributes, taps and descriptions without using resolved results."
    path = Path(case_dir) / "report.json"
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    objects = []
    for obj in raw.get("extra_objects", []):
        if obj["cls"] not in FIELDS:
            raise RepresentationGap("[unsupported_reference_class] Additional class has no LF profile: " + obj["cls"])
        objects.append({**{k: obj[k] for k in ("name", "cls", "path")},
                        "attrs": {k: v for k, v in obj["attrs"].items()
                                  if k in FIELDS[obj["cls"]].split()}})
    return {**{k: raw[k] for k in ("variant", "description", "original_tap", "new_tap") if k in raw},
            "extra_objects": objects,
            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


def _values(obj):
    return {k: a["value"] for k, a in obj["attrs"].items() if a["value"] is not None}


def _require(values, keys):
    missing = [k for k in keys.split() if k not in values]
    if missing:
        raise RepresentationGap("[missing_reference_parameters] Native parameters are missing: " + ", ".join(missing))


def _branch(base, a, b):
    buses = {int(row["name"].removeprefix("Bus ")): row["id"] for row in base["buses"]}
    return next(row for row in base["branches"]
                if row["bus_from"] == buses[a] and row["bus_to"] == buses[b])


def _create(base, variant_id, obj, group):
    """Crea desde entradas PF, conserva correspondencia sin fabricar origen PF."""
    p = _values(obj)
    terminals = {b["path"]: b["id"] for b in base["buses"]}
    def bus(port):
        return terminals[p[port].rsplit("\\", 1)[0]]
    _require(p, "outserv bus1 typ_id")
    data = {"name": obj["name"], "class": obj["cls"], "scope": "ieee39_pf",
            "outserv": p["outserv"], "params": deepcopy(p),
            "units": {k: obj["attrs"][k]["unit"] for k in p},
            "type_id": p["typ_id"], "type_params": {}, "type_units": {}}
    notes = []
    if group == "loads":
        _require(p, "plini qlini scale0")
        data.update(bus=bus("bus1"), p_mw=p["plini"], q_mvar=p["qlini"], scale=p["scale0"],
                    type_class="TypLod")
        data["units"].update(p_mw="MW", q_mvar="Mvar", scale="1")
        notes.append("Constant PQ load using the PFC-MODEL LF profile")
    elif group == "branches":
        _require(p, "bus2 R1 X1 G1 B1 dline nlnum i_dist")
        if p["i_dist"] != 0:
            raise RepresentationGap("[unsupported_distributed_line] Distributed lines are outside this profile")
        template = next((r for r in base["branches"] if r.get("type_id") == p["typ_id"]), None)
        if template is None:
            raise RepresentationGap("[missing_line_type] TypLne is unavailable in the base model: " + p["typ_id"])
        data.update(bus_from=bus("bus1"), bus_to=bus("bus2"), type_class="TypLne",
                    type_params=deepcopy(template["type_params"]),
                    type_units=deepcopy(template["type_units"]))
        # Mismo criterio de PFC-MODEL, tipo por longitud, sin redondeo de totales.
        for total, per_km in (("R1", "rline"), ("X1", "xline"), ("B1", "bline"), ("G1", "gline")):
            effective = data["type_params"][per_km] * p["dline"]
            if not math.isclose(effective, p[total], rel_tol=2e-6, abs_tol=1e-6):
                raise RepresentationGap("[inconsistent_line_total] Total does not match type times length: " + total)
            data["params"][total] = effective
        data["params"]["f_nom_hz"] = base["meta"]["f_nom_hz"]
        data["units"].update(nlnum="1", f_nom_hz="Hz")
        notes.append("R/X/G/B from TypLne times dline, checked against native totals")
    else:
        _require(p, "pgini qgini ngnum usetp av_mode ip_ctrl")
        # PF no exporta escala para ElmSym en este paquete. Misma regla explícita
        # de PFC-MODEL, sin usar P/Q resueltos como entradas del generador.
        scale = p.get("scale0", 1.0)
        data["params"]["scale0"] = scale
        data.update(bus=bus("bus1"), p_mw=p["pgini"] * p["ngnum"] * scale,
                    q_mvar=p["qgini"] * p["ngnum"] * scale,
                    mode=p["av_mode"], ref=bool(p["ip_ctrl"]), v_set_pu=p["usetp"],
                    n_units=p["ngnum"], s_rated_mva=None, q_min_mvar=None,
                    q_max_mvar=None, type_class="TypSym")
        data["units"].update(p_mw="MW", q_mvar="Mvar", v_set_pu="p.u.")
        notes.append("Missing scale0 explicitly means 1 under the PFC-MODEL profile")
        notes.append("Additional TypSym was not exported. Rated S and absolute Q limits are unknown. LF uses free Q")
    command = create_command(variant_id, group, data)
    relative = obj["path"].split(".IntPrj\\", 1)[1]
    # UUID estable para regenerar los artefactos sin modificar create_command.
    power_machine_id = "power_machine_" + str(uuid.uuid5(uuid.NAMESPACE_URL, variant_id + "/" + relative))
    command_data = deepcopy(command.data)
    command_data.update(id=power_machine_id, path=f"power_machine://variants/{variant_id}/{power_machine_id}")
    command = replace(command, object_id=power_machine_id, data=command_data)
    correspondence = {"power_machine_id": power_machine_id, "object_id": "pf_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20],
                      "relative_path": relative, "path": obj["path"], "group": group}
    return command, correspondence, notes


def build_variant(base, description):
    "Build an independent variant from declared inputs before solving load flow."
    tag = description["variant"]
    variant = new_variant(base, description["description"], variant_id="v_pfc_net_" + tag)
    mappings, notes = [], []
    if tag == "A":
        command = edit_command(base, "retire", "branches", _branch(base, 16, 21)["id"])
    elif tag == "C":
        tr = _branch(base, 19, 20)
        old, new = description["original_tap"], description["new_tap"]
        observed = {"position": tr["params"]["nntap"], "neutral": tr["type_params"]["nntap0"],
                    "step_pct": tr["type_params"]["dutap"], "side": tr["type_params"]["tap_side"]}
        if old != observed:
            raise RepresentationGap("[reference_tap_mismatch] Original PF tap differs from the base model")
        command = edit_command(base, "update", "branches", tr["id"],
                               {"params": {"nntap": new["position"]},
                                "type_params": {"nntap0": new["neutral"], "dutap": new["step_pct"], "tap_side": new["side"]}})
    elif tag in "BDE" and len(description["extra_objects"]) == 1:
        command, mapping, notes = _create(base, variant.variant_id,
                                         description["extra_objects"][0], {"B": "loads", "D": "branches", "E": "gens"}[tag])
        mappings.append(mapping)
    else:
        raise RepresentationGap("[unsupported_reference_variant] Variant is outside the protocol: " + tag)
    variant = replace(variant, metadata={"source_sha256": description["source_sha256"],
                                        "pf_correspondence": mappings, "assumptions": notes,
                                        "pf_comparison": "pending"})
    history = VariantHistory(base, variant)
    history.apply(command)
    return history


def comparison_model(model, mappings):
    "Map PF comparison paths without changing electrical parameters or model IDs."
    view = deepcopy(model)
    for mapping in mappings:
        row = next(r for r in view[mapping["group"]] if r["id"] == mapping["power_machine_id"])
        row.update(path=mapping["path"], object_id=mapping["object_id"], relative_path=mapping["relative_path"])
    return view


def compare_variant(model, result, reference, mappings):
    "Compare load flow, inventory, operational status and load coverage."
    view = canonical_reference_model(comparison_model(model, mappings), reference)
    comparison = compare_loadflow(view, result, reference)
    audits = []
    for group, pf_group in (("buses", "buses"), ("branches", "branches"), ("gens", "generators"), ("loads", "loads")):
        expected = {r["path"] for r in reference[pf_group]}
        actual = {r["path"] for r in view[group]}
        audits.append({"check": group + "_paths", "pass": expected == actual,
                       "detail": canonical_json({"missing": sorted(expected - actual), "extra": sorted(actual - expected)})})
    by_path = {r["path"]: r for r in view["branches"]}
    for pf in reference["branches"]:
        row = by_path.get(pf["path"])
        audits.append({"check": "branch_state", "pass": row is not None and row["outserv"] == int(pf["outserv"]), "detail": pf["path"]})
    loads = {r["path"]: r for r in view["loads"]}
    for pf in reference["loads"]:
        row = loads.get(pf["path"])
        for field, native, unit in (("p_mw", "plini", "MW"), ("q_mvar", "qlini", "Mvar")):
            power_machine = row["params"][native] * row["params"]["scale0"] if row else None
            measured = number(pf["P_MW" if field == "p_mw" else "Q_Mvar"])
            delta = power_machine - measured if power_machine is not None and measured is not None else None
            comparison["rows"].append({"group": "loads", "element": pf["path"], "field": field,
                                       "pf": measured, "power_machine": power_machine, "delta": delta,
                                       "abs_delta": abs(delta) if delta is not None else None, "tolerance": 0.05,
                                       "pass": delta is not None and abs(delta) <= 0.05,
                                       "probable_cause": "Entrada PQ constante en " + unit})
    comparison["inventory_pass"] = all(a["pass"] for a in audits)
    comparison["loads_pass"] = all(r["pass"] for r in comparison["rows"] if r["group"] == "loads")
    comparison["pass"] &= comparison["inventory_pass"] and comparison["loads_pass"] and result["converged"]
    comparison["counts"]["loads"] = {"pass": sum(all(r["pass"] for r in comparison["rows"] if r["group"] == "loads" and r["element"] == p) for p in loads), "total": len(loads)}
    comparison["worst"] = sorted((r for r in comparison["rows"] if r["abs_delta"] is not None),
                                  key=lambda r: r["abs_delta"] / r["tolerance"], reverse=True)[:10]
    return comparison, audits


def bit_fingerprint(value):
    "Return a deterministic fingerprint of the serialized value."
    if isinstance(value, float):
        return ("float", value.hex())
    if isinstance(value, dict):
        return tuple((k, bit_fingerprint(v)) for k, v in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(bit_fingerprint(v) for v in value)
    return (type(value).__name__, value)


def export_roundtrip(model, mappings, path):
    "Export and reimport a model, then verify numerical and identity consistency."
    target = safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    envelope = {"schema": "power_machine-pfc-net-export-v1", "model": model, "pf_correspondence": mappings}
    target.write_text(canonical_json(envelope) + "\n", encoding="utf-8")
    imported = json.loads(target.read_text(encoding="utf-8"))
    if imported["schema"] != envelope["schema"]:
        raise ValueError("Esquema de exportación incompatible")
    return {"model_bits_pass": bit_fingerprint(imported) == bit_fingerprint(envelope),
            "lf_bits_pass": bit_fingerprint(run_loadflow(imported["model"], options=LDF_OPTIONS)) ==
                            bit_fingerprint(run_loadflow(model, options=LDF_OPTIONS))}


def compare_pfd_initial_lf(base, result, path):
    "Compare declared initial load-flow observables without evaluating RMS dynamics."
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        names, variables, values = next(reader), next(reader), next(reader)
    if number(values[0]) != 0.0 or not len(names) == len(variables) == len(values):
        raise ValueError("Fila inicial PFD incompleta o diferente de t=0")
    index = {r["name"]: (group, r) for group in ("buses", "branches", "gens", "loads") for r in base[group]}
    solved = {r["id"]: r for group in ("buses", "branches", "gens") for r in result[group]}
    rows = []
    for name, variable, raw in zip(names[1:], variables[1:], values[1:]):
        if name not in index:
            continue
        group, item = index[name]
        fields = {"buses": {"m:u in p.u.": ("u_pu", 1e-4), "m:phiu in deg": ("angle_deg", 0.01)},
                  "gens": {"m:P:bus1 in MW": ("p_mw", 0.05), "m:Q:bus1 in Mvar": ("q_mvar", 0.05)},
                  "branches": {"m:P:bus1 in MW": ("p_from_mw", 0.05), "m:P:bus2 in MW": ("p_to_mw", 0.05)},
                  "loads": {"m:P:bus1 in MW": ("plini", 0.05), "m:Q:bus1 in Mvar": ("qlini", 0.05)}}
        if variable not in fields[group]:
            continue
        field, tolerance = fields[group][variable]
        actual = item["params"][field] * item["params"]["scale0"] if group == "loads" else solved[item["id"]][field]
        expected = number(raw)
        delta = actual - expected if actual is not None and expected is not None else None
        if delta is not None and field == "angle_deg":
            delta = (delta + 180) % 360 - 180
        rows.append({"element": name, "object_id": item["object_id"], "relative_path": item["relative_path"],
                     "variable": variable, "pf": expected, "power_machine": actual, "delta": delta,
                     "tolerance": tolerance, "pass": delta is not None and abs(delta) <= tolerance})
    if not rows:
        raise ValueError("No hay observables LF comparables en la fila inicial PFD")
    return rows


def _write_rows(path, rows):
    target = safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(k for row in rows for k in row))
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_comparisons(pf_dir, output_dir=None, *, model=None):
    "Compare five variants, or return an explicit reference_not_included record before creating outputs."
    source = Path(pf_dir)
    if not (source / "E39-NET-01").is_dir():
        return {"E39-NET-01":{"status":"reference_not_included", "campaign":"E39-NET-01",
                              "included":False, "pass":None,
                              "limitation":"The reference export does not include E39-NET-01."}}
    target = Path(output_dir).resolve() if output_dir is not None else writable_root() / "comparacion_pf" / "red"
    base_bytes = (source / "model.json").read_bytes() if model is None else canonical_json(model).encode("utf-8")
    base = json.loads(base_bytes.decode("utf-8-sig"))
    base_result = run_loadflow(base, options=LDF_OPTIONS)
    base_comparison = compare_loadflow(base, base_result, read_reference(source / "E39-LF-01"))
    if not base_comparison["pass"] or not base_result["converged"]:
        raise ValueError("[base_reference_mismatch] Base model does not satisfy the LF reference")
    verification = [{"variant": "base", "check": k, "pass": v, "detail": "JSON power_machine"}
                    for k, v in export_roundtrip(base, [], target / "base_model_export.json").items()]
    descriptions, overview, mappings_table, comparisons = [], [], [], {}
    for tag in "ABCDE":
        folder = source / "E39-NET-01" / tag
        description = read_description(folder)
        descriptions.append({"variant": tag, "description": description["description"],
                             "inputs": canonical_json(description), "sha256": description["source_sha256"]})
        try:
            history = build_variant(base, description)
        except RepresentationGap as exc:
            overview.append({"variant": tag, "pass": False, "status": "representation_gap", "limitation": str(exc)})
            comparisons[tag] = {"pass": False, "limitation": str(exc)}
            continue
        model = history.model
        mappings = history.variant.metadata["pf_correspondence"]
        mappings_table.extend({"variant": tag, **r} for r in mappings)
        result = run_loadflow(model, options=LDF_OPTIONS)
        reference = read_reference(folder)
        comparison, audits = compare_variant(model, result, reference, mappings)
        checks = {}
        history.undo()
        checks["undo_model_bits"] = bit_fingerprint(history.model) == bit_fingerprint(base)
        checks["undo_lf_bits"] = bit_fingerprint(run_loadflow(history.model, options=LDF_OPTIONS)) == bit_fingerprint(base_result)
        history.redo()
        checks["redo_model_bits"] = bit_fingerprint(history.model) == bit_fingerprint(model)
        checks["redo_lf_bits"] = bit_fingerprint(run_loadflow(history.model, options=LDF_OPTIONS)) == bit_fingerprint(result)
        save_variant(history.variant, target / tag / "variant.json")
        restored = materialize_variant(base, load_variant(target / tag / "variant.json"))
        checks["variant_storage_bits"] = bit_fingerprint(restored) == bit_fingerprint(model)
        checks.update(export_roundtrip(model, mappings, target / tag / "model_export.json"))
        checks["base_file_unchanged"] = (source / "model.json").read_bytes() == base_bytes
        if tag == "B":
            completion = json.loads((folder / "export_completion.json").read_text(encoding="utf-8-sig"))
            view = canonical_reference_model(comparison_model(model, mappings), reference)
            loads = {r["path"]: r for r in view["loads"]}
            matches = []
            for pf in completion["new_load"]:
                row = loads.get(pf["path"])
                matches.append(row is not None and row["p_mw"] == pf["plini_MW"] and
                               row["q_mvar"] == pf["qlini_Mvar"] and row["scale"] == pf["scale"] and
                               next(b for b in base["buses"] if b["id"] == row["bus"])["name"] == f"Bus {pf['bus']:02d}")
            checks["pf_export_completion"] = completion["code"] == 0 and completion["load_rows"] == len(loads) and len(matches) == 1 and all(matches)
        comparison["reversibility_pass"] = all(checks.values())
        comparison["pass"] &= comparison["reversibility_pass"]
        comparisons[tag] = comparison
        _write_rows(target / tag / "inventory.csv", audits)
        verification.extend({"variant": tag, "check": k, "pass": v, "detail": "power_machine"} for k, v in checks.items())
        overview.append({"variant": tag, "pass": comparison["pass"], "status": "compared",
                         "reversibility_pass": comparison["reversibility_pass"],
                         "loss_power_machine_mw": result["totals"]["branch_loss_p_mw"],
                         "loss_pf_mw": reference["report"]["summary"]["loss_MW"],
                         "limitation": " | ".join(history.variant.metadata["assumptions"]),
                         **comparison["maxima"]})
    pfd = json.loads((source / "verification/pfd_import_validation.json").read_text(encoding="utf-8-sig"))
    pf_base = next(check for check in pfd["checks"] if check["name"] == "Base load flow")
    verification.append({"variant": "PF_PFD", "check": "reported_base_lf", "pass": pf_base["code"] == 0 and pf_base["max_voltage_difference_pu"] == 0,
                         "detail": "PF evidence was read, not executed. DIN03 RMS, DSL and protection are outside scope"})
    pfd_initial = compare_pfd_initial_lf(base, base_result, source / "verification/PFD_import_DIN03.csv")
    _write_rows(target / "pfd_initial_lf.csv", pfd_initial)
    verification.append({"variant": "PF_PFD", "check": "initial_lf_observables", "pass": all(r["pass"] for r in pfd_initial),
                         "detail": f"{len(pfd_initial)} LF observables at t=0 without evaluating RMS"})
    verification_pass = all(r["pass"] for r in verification)
    for tag, comparison in comparisons.items():
        comparison["verification_pass"] = verification_pass
        comparison["pass"] &= verification_pass
        if "rows" in comparison:
            write_comparison(comparison, target / tag)
    for row in overview:
        row["pass"] &= verification_pass
    _write_rows(target / "cases.csv", overview)
    _write_rows(target / "descriptions.csv", descriptions)
    _write_rows(target / "correspondence.csv", mappings_table)
    _write_rows(target / "verification.csv", verification)
    return comparisons


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    comparisons = run_comparisons(args.pf_dir, args.output_dir)
    for tag, comparison in comparisons.items():
        print(tag, comparison["pass"], comparison.get("maxima", comparison.get("limitation")))
    raise SystemExit(0 if all(c["pass"] for c in comparisons.values()) else 1)


def compare_network(model, result, pf_results_dir, mappings=None):
    "Compare a network variant against user-supplied PF tables."
    return compare_variant(model, result, read_reference(pf_results_dir), mappings or [])
