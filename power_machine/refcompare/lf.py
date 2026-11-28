"Offline REF load-flow comparisons with explicit data and coverage."
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from ..resources import output_root, writable_root

TOLERANCES = {"u_pu": 1e-4, "angle_deg": 0.01, "p_mw": 0.05,
              "q_mvar": 0.05, "loss_p_mw": 0.01}
WORKSPACE = output_root()


def number(value):
    "Return a finite number, or None for a blank cell."
    if value is None or str(value).strip() == "":
        return None
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("[nonfinite_reference] Numeric reference data must be finite")
    return result


def read_csv(path):
    "Read optional-BOM UTF-8 CSV, preserving empty cells and exact paths."
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_reference(case_dir):
    "Read load-flow tables and metadata supplied in a reference folder."
    root = Path(case_dir)
    result = {g: read_csv(root / (g + ".csv")) if (root / (g + ".csv")).exists() else []
              for g in ("buses", "branches", "generators", "loads")}
    result["report"] = json.loads((root / "report.json").read_text(encoding="utf-8-sig"))
    if result["report"].get("status") != "sin_solucion_referencia_aislada":
        for group in ("buses", "branches", "generators", "loads"):
            if not result[group]:
                raise ValueError("[missing_reference_table] Reference table is missing or empty: " + group)
    return result


def canonical_reference_model(model, reference):
    "Map the included MATPOWER case by explicit bus numbers without changing electrical parameters. Reject ambiguous mappings."
    from copy import deepcopy
    if not model.get("buses") or not all(b.get("origin") == "matpower" for b in model["buses"]):
        return model
    view = deepcopy(model)
    numbers = {b["id"]: int(b["source_row"]["values"]["bus"]) for b in view["buses"]}
    for group, table in (("buses", "buses"), ("branches", "branches"), ("gens", "generators"), ("loads", "loads")):
        for row in view[group]:
            if group == "branches":
                key = {numbers[row["bus_from"]], numbers[row["bus_to"]]}
                matches = [p for p in reference[table] if {int(p["from_bus"]), int(p["to_bus"])} == key]
            else:
                key = numbers[row["id"] if group == "buses" else row["bus"]]
                matches = [p for p in reference[table] if int(p["bus"]) == key]
            if not matches and group == "loads":
                continue
            if len(matches) != 1:
                raise ValueError("[ambiguous_reference_mapping] REF mapping is missing or ambiguous: " + row["id"])
            row["path"] = matches[0]["path"]
    return view


def compare_loadflow(model, result, reference):
    "Compare outgoing MW/Mvar, voltage pu and aligned angles by exact REF path. Preserve missing data and report unreferenced islands separately."
    if isinstance(reference, (str, Path)):
        reference = read_reference(reference)
    model = canonical_reference_model(model, reference)
    index = {row["id"]: row for g in ("buses", "branches", "loads", "gens") for row in model[g]}
    buses_by_path = {index[r["id"]]["path"]: r for r in result["buses"]}
    ref_buses = {r["path"]: r for r in reference["buses"]}
    bus_numbers = {r["id"]: int(ref_buses[r["path"]]["bus"]) if r["path"] in ref_buses
                   else -(i + 1) for i, r in enumerate(model["buses"])}
    refs = [g for g in model["gens"] if g["ref"]]
    ref_bus = index[refs[0]["bus"]]["path"] if refs else None
    ref_ref = ref_buses.get(ref_bus)
    power_machine_ref = buses_by_path.get(ref_bus)
    offset = 0.0
    if ref_ref and power_machine_ref and number(ref_ref["angle_deg"]) is not None and power_machine_ref["angle_deg"] is not None:
        offset = number(ref_ref["angle_deg"]) - power_machine_ref["angle_deg"]
    rows = []

    def metric(group, item, field, ref_value, power_machine_value, tolerance, cause):
        ref_value, power_machine_value = number(ref_value), number(power_machine_value)
        delta = power_machine_value - ref_value if ref_value is not None and power_machine_value is not None else None
        if delta is not None and field == "angle_deg":
            delta = (delta + 180) % 360 - 180
        rows.append({"group": group, "element": item, "field": field,
                     "ref": ref_value, "power_machine": power_machine_value, "delta": delta,
                     "abs_delta": abs(delta) if delta is not None else None,
                     "tolerance": tolerance, "pass": delta is not None and abs(delta) <= tolerance,
                     "probable_cause": cause})

    unresolved = {b for i in result["islands"] if i["status"] == "sin_referencia" for b in i["buses"]}
    for path, ref_entry in ref_buses.items():
        power_machine = buses_by_path[path]
        if power_machine["id"] in unresolved:
            continue
        metric("buses", ref_entry["bus"], "u_pu", ref_entry["u_pu"], power_machine["u_pu"], TOLERANCES["u_pu"], "Numerical precision or network parameters")
        metric("buses", ref_entry["bus"], "angle_deg", ref_entry["angle_deg"], power_machine["angle_deg"] + offset if power_machine["angle_deg"] is not None else None, TOLERANCES["angle_deg"], "Numerical precision, tap or phase shift")
    branches_by_path = {index[r["id"]]["path"]: r for r in result["branches"]}
    for ref_entry in reference["branches"]:
        if int(ref_entry["outserv"]):
            continue
        power_machine = branches_by_path.get(ref_entry["path"])
        if power_machine and power_machine["bus_from"] in unresolved:
            continue
        reverse = power_machine and bus_numbers[power_machine["bus_from"]] != int(ref_entry["from_bus"])
        for end in ("from", "to"):
            power_machine_end = ("to" if end == "from" else "from") if reverse else end
            for kind, unit in (("P", "MW"), ("Q", "Mvar")):
                field = f"{kind.lower()}_{end}_{unit.lower()}"
                value = power_machine.get(f"{kind.lower()}_{power_machine_end}_{unit.lower()}") if power_machine else None
                metric("branches", ref_entry["branch"], field, ref_entry[f"{kind}_{end}_{unit}"], value,
                       TOLERANCES["p_mw" if kind == "P" else "q_mvar"],
                       "REF constant precision, tap, B or terminal sign")
    gens_by_path = {index[r["id"]]["path"]: r for r in result["gens"]}
    for ref_entry in reference["generators"]:
        power_machine = gens_by_path.get(ref_entry["path"])
        if power_machine and power_machine["bus"] in unresolved:
            continue
        for kind, unit in (("P", "MW"), ("Q", "Mvar")):
            field = f"{kind.lower()}_{unit.lower()}"
            metric("reference" if int(ref_entry["reference"]) else "generators", index[power_machine["id"]]["name"] if power_machine else ref_entry["path"], field,
                   ref_entry[f"{kind}_{unit}"], power_machine[field] if power_machine else None, 0.05, "Network balance and input precision")
    report = reference["report"]
    summary = report.get("summary") or {}
    loss_ref = summary.get("loss_MW")
    if loss_ref is None and reference["branches"] and not unresolved:
        losses = [number(r["loss_MW"]) for r in reference["branches"] if not int(r["outserv"])]
        loss_ref = sum(losses) if all(v is not None for v in losses) else None
    if loss_ref is not None:
        metric("totals", "network", "loss_p_mw", loss_ref, result["totals"]["branch_loss_p_mw"], 0.01, "Resistance and numerical balance")
    components = sorted(sorted(bus_numbers[b] for b in i["buses"]) for i in result["islands"]) if bus_numbers else []
    without_ref = sorted(sorted(bus_numbers[b] for b in i["buses"]) for i in result["islands"] if i["status"] == "sin_referencia") if bus_numbers else []
    demand_mw = sum(r["p_mw"] for r in model["loads"] if r["bus"] in unresolved)
    topology_pass = ("components" not in report or components == sorted(sorted(c) for c in report["components"]))
    topology_pass &= ("islands_without_reference" not in report or without_ref == sorted(sorted(c) for c in report["islands_without_reference"]))
    demand_pass = "unsupplied_load_MW" not in report or abs(demand_mw - report["unsupplied_load_MW"]) <= 1e-6
    expected_no_solution = report.get("status") == "sin_solucion_referencia_aislada"
    electrical_pass = all(r["pass"] for r in rows) if not expected_no_solution else not result["converged"]
    maxima = {field: max((r["abs_delta"] for r in rows if r["field"] == field and r["abs_delta"] is not None), default=None)
              for field in sorted({r["field"] for r in rows})}
    counts = {}
    for group in sorted({r["group"] for r in rows}):
        ids = {r["element"] for r in rows if r["group"] == group}
        counts[group] = {"pass": sum(all(r["pass"] for r in rows if r["group"] == group and r["element"] == key) for key in ids), "total": len(ids)}
    worst = sorted((r for r in rows if r["abs_delta"] is not None), key=lambda r: r["abs_delta"] / r["tolerance"], reverse=True)[:10]
    return {"pass": electrical_pass and topology_pass and demand_pass,
            "rows": rows, "maxima": maxima, "counts": counts, "worst": worst,
            "components": components, "islands_without_reference": without_ref,
            "demand_without_reference_mw": demand_mw, "topology_pass": topology_pass,
            "demand_pass": demand_pass, "expected_no_solution": expected_no_solution,
            "power_machine_converged": result["converged"], "angle_offset_deg": offset}


def write_comparison(comparison, output_dir):
    "Write comparison tables and summaries in the requested directory."
    target = Path(output_dir).resolve()
    target.mkdir(parents=True, exist_ok=True)
    for name, rows in (("differences", comparison["rows"]), ("worst10", comparison["worst"])):
        with (target / (name + ".csv")).open("w", encoding="utf-8-sig", newline="") as handle:
            fields = list(rows[0]) if rows else ["group", "element", "field", "ref", "power_machine", "delta", "pass"]
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
    summary_rows = [{"metric": k, "value": json.dumps(v, ensure_ascii=False)}
                    for k, v in comparison.items() if k not in ("rows", "worst")]
    with (target / "summary.csv").open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["metric", "value"])
        writer.writeheader()
        writer.writerows(summary_rows)


def run_comparisons(ref_dir, output_dir=None):
    "Compare LF and included historical contingency campaigns. Return explicit not_included records for absent campaigns. Historical contingency comparisons use island_reference=none."
    from ..benchmarks.ieee39_ref import build_model, LDF_OPTIONS
    from ..loadflow import run_loadflow
    from ..contingency import run_contingencies, ContingencyOptions
    model = build_model(ref_dir)
    source, target = Path(ref_dir), Path(output_dir).resolve() if output_dir is not None else writable_root() / "comparacion_ref" / "flujo"
    comparisons = {}
    base = compare_loadflow(model, run_loadflow(model, options=LDF_OPTIONS), read_reference(source / "E39-LF-01"))
    comparisons["E39-LF-01"] = base
    write_comparison(base, target / "E39-LF-01")
    for group in ("E39-CT-01", "E39-CT-02"):
        if not (source / group).is_dir():
            comparisons[group] = {"status":"not_included", "campaign":group, "included":False,
                                  "pass":None, "maxima":{}, "demand_without_reference_mw":None,
                                  "message":"The reference export does not include this campaign."}
    if not base["pass"]:
        return comparisons
    for group, order in (("E39-CT-01", 1), ("E39-CT-02", 2)):
        if group in comparisons:
            continue
        cases = sorted(p for p in (source / group).iterdir() if p.is_dir() and p.name.startswith("N"))
        if not cases:
            comparisons[group] = {"status":"not_included", "campaign":group, "included":False,
                                  "pass":None, "maxima":{}, "demand_without_reference_mw":None,
                                  "message":"The reference campaign contains no exported cases."}
            continue
        batch = None
        if order == 1:
            batch = run_contingencies(model, {}, ContingencyOptions(orders=(1,), q_limits=False, island_reference="none"),
                selected_ids=[r["id"] for r in model["branches"]], filter_reason="46 ramas del protocolo REF",
                ldf_options=LDF_OPTIONS)
        for case in cases:
            reference = read_reference(case)
            paths = {o["path"] for o in reference["report"]["objects"]}
            ids = {r["id"] for r in model["branches"] if r["path"] in paths}
            if len(ids) != order:
                raise ValueError("[unmapped_reference_outage] REF outages could not be mapped: " + case.name)
            if order == 2:
                batch = run_contingencies(model, {}, ContingencyOptions(orders=(2,), q_limits=False, island_reference="none"),
                    selected_ids=ids, filter_reason="Par N-2 del protocolo REF", ldf_options=LDF_OPTIONS)
            raw = next(c for c in batch["payload"]["cases"] if {o["object_id"] for o in c["outages"]} == ids)
            if raw["loadflow"] is None:
                raise RuntimeError(str(batch["diagnostics"]))
            comparison = compare_loadflow(model, raw["loadflow"], reference)
            comparison["contingency_status"] = raw["status"]
            comparisons[group + "/" + case.name] = comparison
            write_comparison(comparison, target / group / case.name)
        overview = []
        for key, comparison in comparisons.items():
            if not key.startswith(group + "/"):
                continue
            overview.append({"case": key.split("/", 1)[1], "pass": comparison["pass"],
                             "topology_pass": comparison["topology_pass"],
                             "demand_without_reference_mw": comparison["demand_without_reference_mw"],
                             **comparison["maxima"]})
        fields = sorted({k for r in overview for k in r})
        with (target / group / "cases.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(overview)
    return comparisons


if __name__ == "__main__":
    import argparse
    from ..benchmarks.ieee39_ref import DEFAULT_REF_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref-dir", type=Path, default=DEFAULT_REF_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    for case, comparison in run_comparisons(args.ref_dir, args.output_dir).items():
        print(case, comparison["pass"], comparison["maxima"], comparison["demand_without_reference_mw"])


def compare_results(model, result, ref_results_dir):
    "Compare a load-flow result with the supplied REF table folder."
    return compare_loadflow(model, result, read_reference(ref_results_dir))
