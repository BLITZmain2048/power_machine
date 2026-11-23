"Explicit data, numerical methods and traceable results for contingency studies."
from __future__ import annotations

import math

from ..elements import gen_pq_mw, gen_q_limits_mvar, load_pq_mw, MissingParameter
from ..lf_status import energized_status as _legacy_energized_status
from ..topology import build_topology
from .catalog import identity, index_model
from .results import diagnostic
from .references import generation_only, has_demand, disconnected_generation


def energized_status(result):
    """Return island convergence counts with an English message and stable code."""
    summary = _legacy_energized_status(result)
    code = 'energized_converged' if summary['ok'] else 'energized_not_converged'
    summary['code'] = code
    summary['message'] = ('Load flow converged' if summary['ok'] else 'Load flow did not converge') + ' in %d energized island(s), %d deenergized island(s)' % (summary['n_solved'] + summary['n_failed'], summary['n_deenergized'])
    return summary


def finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalize_loadflow(raw: dict, model: dict) -> dict:
    "Normalize raw load-flow fields and add identity metadata without changing electrical values."
    result = dict(raw)
    for group in ("buses", "branches", "gens", "loads"):
        result[group] = [dict(row) for row in raw.get(group, [])]
    index = index_model(model)
    for group in ("buses", "branches", "gens", "loads"):
        result.setdefault(group, [])
        for row in result[group]:
            item = index.get(row["id"], {})
            row.update(identity(item or row))
            if group == "buses":
                row["un_kv"] = float(item["un_kv"])
                row["energized"] = row.get("status") == "resuelto" and bool(row.get("u_pu"))
            elif group == "gens":
                row["at_q_limit"] = bool(row.get("q_limit", row.get("at_q_limit")))
            elif row.get("class") == "ElmTr3":
                for name, source in (("bus_from", "bus_hv"), ("bus_to", "bus_mv"),
                                     ("p_from_mw", "p_hv_mw"), ("q_from_mvar", "q_hv_mvar"),
                                     ("p_to_mw", "p_mv_mw"), ("q_to_mvar", "q_mv_mvar"),
                                     ("i_from_ka", "i_hv_ka")):
                    row[name] = row.get(source)
    present = {row["id"] for row in result["buses"]}
    for bus in model.get("buses", []):
        if bus["id"] not in present:
            result["buses"].append({"id": bus["id"], "name": bus["name"], **identity(bus),
                                    "un_kv": float(bus["un_kv"]), "u_pu": None,
                                    "u_kv": None, "angle_deg": None, "energized": False,
                                    "status": "fuera_de_servicio"})
    mismatch = result.get("max_mismatch_mva")
    result["max_mismatch_pu"] = mismatch / float(model["meta"]["s_base_mva"]) if finite(mismatch) else None
    return result


def _rating(item, label, un_kv):
    """Terminal current rating in kA and separate apparent-power rating in MVA.

ratings accepts terminal mappings with value, unit and source in A, kA or MVA.
Fields ending in _ka and _mva have explicit units. TypLne.sline is used only
with exported kA units. TypTr2.strn is rated MVA. Convert rated S to rated I
using nominal voltage, never calculated voltage or MW.
"""
    entry = item.get("ratings", {}).get(label, item.get("ratings", {}).get("all"))
    if entry is not None:
        value, unit, source = entry.get("value"), entry.get("unit"), entry.get("source")
    else:
        value, unit, source = None, None, None
        for field, field_unit in ((f"i_rated_{label}_ka", "kA"), ("i_rated_ka", "kA"),
                                  (f"s_rated_{label}_mva", "MVA"), ("rating_mva", "MVA"),
                                  ("rate_a_mva", "MVA"), ("s_rated_mva", "MVA")):
            if item.get(field) is not None:
                value, unit, source = item[field], field_unit, field
                break
        if value is None:
            tp, p = item.get("type_params", {}), item.get("params", {})
            units = item.get("type_units", {})
            if item.get("class") == "ElmLne" and tp.get("sline") is not None and units.get("sline") == "kA":
                value = float(tp["sline"]) * float(p.get("nlnum", 1)) * float(p.get("fline", 1))
                unit, source = "kA", "TypLne.sline[kA] × nlnum × fline"
            elif item.get("class") == "ElmTr2" and tp.get("strn") is not None:
                value = float(tp["strn"]) * float(p.get("ntnum", 1))
                unit, source = "MVA", "TypTr2.strn[MVA] × ntnum"
            elif item.get("class") == "ElmTr3":
                field = {"hv": "strn3_h", "mv": "strn3_m", "lv": "strn3_l"}[label]
                value, unit, source = tp.get(field), "MVA", f"TypTr3.{field}[MVA]"
    if not finite(value) or value <= 0 or unit not in ("A", "kA", "MVA"):
        return None
    if not finite(un_kv) or un_kv <= 0:
        return None
    i_ka = value / 1000 if unit == "A" else value
    s_mva = value if unit == "MVA" else None
    if unit == "MVA":
        i_ka = value / (math.sqrt(3) * un_kv)
    return {"i_rated_ka": i_ka, "s_rated_mva": s_mva, "source_value": value,
            "source_unit": unit, "source": source,
            "conversion": "S_MVA/(sqrt(3)*U_nom_kV)" if unit == "MVA" else "A/1000" if unit == "A" else "kA"}


def _source_ids(island, index):
    sources, unknown = [], []
    for key in island["elements"].get("gens", []):
        item = index[key]
        if item.get("ref") or item.get("mode") == "constv":
            sources.append(key)
            continue
        try:
            p_mw, _ = gen_pq_mw(item)
            if p_mw >= 0:
                sources.append(key)
        except (MissingParameter, ValueError, TypeError):
            unknown.append(key)
    return sources, unknown


def collect_metrics(model: dict, ldf: dict, options, *, base=None, retired_reference_ids=(), topology=None, base_result=None):
    "Return island metrics, violations, diagnostics and case status.\n\nDemand uses nominal-voltage MW/Mvar with operational scaling. Unknown supply\nremains unknown. Only solved energized buses enter voltage and severity metrics."
    index = index_model(model)
    lf_summary = energized_status(ldf)
    if topology is None:
        topology = build_topology(model, open_branches=ldf.get("omitted_branches", ()))
    automatic = getattr(options, 'island_reference', 'none') == 'largest_generator'
    generation_islands = {i['id'] for i in topology['islands'] if generation_only(i, index)} if automatic else set()
    generation_records, generation_unknown = disconnected_generation(model, topology['islands'], index, base_result) if automatic else ([], [])
    if automatic:
        operating_reports = [i for i in ldf.get('islands', ()) if i['id'] not in generation_islands]
        lf_summary = energized_status({**ldf, 'islands':operating_reports})
        if not operating_reports and generation_islands:
            lf_summary.update(ok=True, n_solved=0, n_failed=0, code='no_loaded_islands',
                              message='No loaded islands require a power-flow solution.')
        lf_summary['n_disconnected_generation_islands'] = len(generation_islands)
        lf_summary['message'] = ('Load flow converged' if lf_summary['ok'] else 'Load flow did not converge') + ' in loaded operating islands. %d generator-only island(s) are reported separately.' % len(generation_islands)
    reports = {frozenset(row.get("buses", [])): row for row in ldf.get("islands", [])}
    diagnostics, violations, islands, unresolved = [], [], [], []
    unsupplied_loads = {}
    deenergized_loads, deenergized_gens = {}, {}
    unknown_deenergized_loads, unknown_deenergized_gens = [], []
    unknown_loads = []
    if automatic:
        deenergized_gens.update({row['generator_id']:max(0.,row['p_mw']) for row in generation_records if row['p_mw'] is not None})
        unknown_deenergized_gens.extend(generation_unknown)
        diagnostics.extend(diagnostic("disconnected_generation_data_missing", "Disconnected generator base P/Q are unavailable.", object_id=ident) for ident in generation_unknown)
    solved_ids = set()
    for island in topology["islands"]:
        report = reports.get(frozenset(island["buses"]))
        # Sin informe por isla solo se acepta evidencia de convergencia global.
        evidence = report.get("status") if report else ("resuelto" if lf_summary["ok"]
                                                        and island["reference_gen"] else None)
        sources, unknown_sources = _source_ids(island, index)
        status = "solved" if evidence == "resuelto" else (
            "not_converged" if island["reference_gen"] else
            "unsolved" if sources or unknown_sources else "unsupplied")
        if island['id'] in generation_islands:
            status = 'generation_disconnected'
        row = {"island_id": island["id"], "bus_ids": island["buses"],
               "status": status, "solver_status": evidence, "reference_id": island["reference_gen"],
               "source_ids": sources, "source_availability_unknown_ids": unknown_sources,
               "load_ids": island["elements"]["loads"], "unsupplied_p_mw": None,
               "unsupplied_q_mvar": None,
               "max_mismatch_mva": report.get("max_mismatch_mva") if report else None}
        if status == 'generation_disconnected':
            row['unsupplied_p_mw'] = row['unsupplied_q_mvar'] = 0.0
            islands.append(row)
            continue
        if status == "solved":
            solved_ids.update(island["buses"])
        else:
            unresolved.extend(island["buses"])
            # Una isla sin referencia queda desenergizada aunque conserve una
            # fuente PQ. Se informa su demanda y generación programada por
            # separado, sin convertir una ausencia de solución en cero.
            if status in ("unsupplied", "unsolved"):
                for key in row["load_ids"]:
                    try:
                        p_mw, q_mvar = load_pq_mw(index[key])
                        if not all(finite(v) for v in (p_mw, q_mvar)):
                            raise ValueError('[invalid_input] ' + "Nonfinite demand")
                        deenergized_loads[key] = (max(0.0, p_mw), max(0.0, q_mvar))
                    except (MissingParameter, TypeError, ValueError) as exc:
                        unknown_deenergized_loads.append(key)
                        diagnostics.append(diagnostic("load_data_missing", exc, **identity(index[key])))
                for key in island["elements"].get("gens", []):
                    try:
                        p_mw, _ = gen_pq_mw(index[key])
                        if not finite(p_mw):
                            raise ValueError('[invalid_input] ' + "Nonfinite generation")
                        deenergized_gens[key] = max(0.0, p_mw)
                    except (MissingParameter, TypeError, ValueError) as exc:
                        unknown_deenergized_gens.append(key)
                        diagnostics.append(diagnostic("generation_data_missing", exc, **identity(index[key])))
            if status == "unsupplied":
                values = []
                for key in row["load_ids"]:
                    try:
                        p_mw, q_mvar = load_pq_mw(index[key])
                        if not all(finite(v) for v in (p_mw, q_mvar)):
                            raise ValueError('[invalid_input] ' + "Nonfinite demand")
                        demand = (max(0.0, p_mw), max(0.0, q_mvar))
                        unsupplied_loads[key] = demand
                        values.append(demand)
                    except (MissingParameter, TypeError, ValueError) as exc:
                        unknown_loads.append(key)
                        diagnostics.append(diagnostic("load_data_missing", exc, **identity(index[key])))
                if not any(key in unknown_loads for key in row["load_ids"]):
                    row["unsupplied_p_mw"] = sum(v[0] for v in values)
                    row["unsupplied_q_mvar"] = sum(v[1] for v in values)
            else:
                unknown_loads.extend(row["load_ids"])
            diagnostics.append(diagnostic(status, f"Island {island['id']}: {status}"))
        islands.append(row)
    base = base or {}
    old_unresolved = set(base.get("unresolved_bus_ids", ()))
    newly_unresolved = sorted(set(unresolved) - old_unresolved)
    new_loads = sorted(set(unsupplied_loads) - set(base.get("unsupplied_load_ids", ())))
    new_deenergized_loads = sorted(set(deenergized_loads) - set(base.get("deenergized_load_ids", ())))
    new_deenergized_gens = sorted(set(deenergized_gens) - set(base.get("deenergized_gen_ids", ())))
    known_p = sum(v[0] for v in unsupplied_loads.values())
    known_q = sum(v[1] for v in unsupplied_loads.values())
    has_unknown_supply = bool(unknown_loads) or any(i["status"] in ("unsolved", "not_converged") for i in islands)
    voltage_rows = []
    for row in ldf.get("buses", []):
        if row["id"] not in solved_ids or row.get("status") != "resuelto" or not finite(row.get("u_pu")) or row["u_pu"] <= 0:
            continue
        value = row["u_pu"]
        previous = base.get("voltage_by_bus", {}).get(row["id"])
        voltage_rows.append({**identity(index[row["id"]]), "u_pu": value,
                             "delta_u_pu": value - previous if previous is not None else None})
        if value < options.voltage_min_pu or value > options.voltage_max_pu:
            violations.append({"kind": "voltage", **identity(index[row["id"]]), "u_pu": value,
                               "min_pu": options.voltage_min_pu, "max_pu": options.voltage_max_pu})
    branches, missing_ratings = [], []
    for flow in ldf.get("branches", []):
        item = index.get(flow["id"], flow)
        labels = ("hv", "mv", "lv") if item.get("class") == "ElmTr3" else ("from", "to")
        end_rows = []
        for label in labels:
            bus_id = flow.get("bus_" + label)
            current = flow.get(f"i_{label}_ka")
            p_mw, q_mvar = flow.get(f"p_{label}_mw"), flow.get(f"q_{label}_mvar")
            # Un extremo interno abierto se considera resuelto por su isla de conexión.
            solved = (flow.get("status") in ("resuelto", "extremo_abierto", "malla_no_unica")
                      and any(flow.get("bus_" + end) in solved_ids for end in labels))
            rating = _rating(item, label, index.get(bus_id, {}).get("un_kv"))
            loading = 100 * abs(current) / rating["i_rated_ka"] if solved and rating and finite(current) else None
            apparent = (100 * math.hypot(p_mw, q_mvar) / rating["s_rated_mva"]
                        if solved and rating and rating["s_rated_mva"] and finite(p_mw) and finite(q_mvar) else None)
            end_rows.append({"end": label, "bus_id": bus_id, "p_mw": p_mw if solved else None,
                             "q_mvar": q_mvar if solved else None, "i_ka": abs(current) if solved and finite(current) else None,
                             "rating": rating, "loading_pct": loading, "apparent_loading_pct": apparent})
        loads = [r["loading_pct"] for r in end_rows if r["loading_pct"] is not None]
        apparent = [r["apparent_loading_pct"] for r in end_rows if r["apparent_loading_pct"] is not None]
        currents = [r["i_ka"] for r in end_rows if r["i_ka"] is not None]
        row = {**identity(item), "ends": end_rows, "max_current_ka": max(currents, default=None),
               "loading_pct": max(loads, default=None), "apparent_loading_pct": max(apparent, default=None),
               "rating_complete": all(r["rating"] is not None for r in end_rows)}
        branches.append(row)
        if not row["rating_complete"]:
            missing_ratings.append(flow["id"])
            diagnostics.append(diagnostic("rating_missing", "Thermal rating is missing or has no valid unit", **identity(item)))
        if loads and max(loads) > options.loading_limit_pct:
            violations.append({"kind": "loading", **identity(item), "loading_pct": max(loads),
                               "limit_pct": options.loading_limit_pct})
    q_limits, q_missing = [], []
    for row in ldf.get("gens", []):
        if row.get("bus") not in solved_ids:
            continue
        item = index[row["id"]]
        if row.get("at_q_limit", row.get("q_limit")):
            q_limits.append({**identity(item), "q_mvar": row.get("q_mvar")})
        if options.q_limits and item.get("mode") == "constv" and not item.get("ref"):
            try:
                lower, upper = gen_q_limits_mvar(item)
                q_mvar = row.get("q_mvar")
                if finite(q_mvar) and not lower - 1e-5 <= q_mvar <= upper + 1e-5:
                    violations.append({"kind": "q_limit", **identity(item), "q_mvar": q_mvar,
                                       "min_mvar": lower, "max_mvar": upper})
            except (MissingParameter, ValueError, TypeError):
                q_missing.append(row["id"])
                diagnostics.append(diagnostic("q_limits_missing", "Missing Q limits", **identity(item)))
    components = {
        "low_voltage_pu": sum(max(0.0, options.voltage_min_pu - r["u_pu"]) for r in voltage_rows),
        "high_voltage_pu": sum(max(0.0, r["u_pu"] - options.voltage_max_pu) for r in voltage_rows),
        "loading_excess": sum(max(0.0, r["loading_pct"] - options.loading_limit_pct) / 100
                              for r in branches if r["loading_pct"] is not None)}
    reference_rows = [row for row in ldf.get("gens", [])
                      if index[row["id"]].get("ref") and row.get("bus") in solved_ids
                      and finite(row.get("p_mw"))]
    accepted_losses = [row["p_loss_mw"] for row in ldf.get("branches", [])
                       if row.get("status") in ("resuelto", "extremo_abierto", "malla_no_unica")
                       and finite(row.get("p_loss_mw"))
                       and any(row.get("bus_" + end) in solved_ids for end in ("from", "to", "hv", "mv", "lv"))]
    metrics = {"islands": islands, "unresolved_bus_ids": sorted(unresolved),
               "newly_unresolved_bus_ids": newly_unresolved, "unsupplied_load_ids": sorted(unsupplied_loads),
               "unknown_supply_load_ids": sorted(set(unknown_loads)),
               "unsupplied_p_mw": None if has_unknown_supply else known_p,
               "unsupplied_q_mvar": None if has_unknown_supply else known_q,
               "known_unsupplied_p_mw": known_p, "known_unsupplied_q_mvar": known_q,
               "newly_unsupplied_load_ids": new_loads,
               "newly_unsupplied_p_mw": sum(unsupplied_loads[key][0] for key in new_loads),
               "newly_unsupplied_q_mvar": sum(unsupplied_loads[key][1] for key in new_loads),
               "energized_converged": lf_summary["ok"],
               "energized_status": lf_summary,
               "deenergized_load_ids": sorted(deenergized_loads),
               "deenergized_gen_ids": sorted(deenergized_gens),
               "unknown_deenergized_load_ids": sorted(set(unknown_deenergized_loads)),
               "unknown_deenergized_gen_ids": sorted(set(unknown_deenergized_gens)),
               "newly_deenergized_load_ids": new_deenergized_loads,
               "newly_deenergized_gen_ids": new_deenergized_gens,
               "newly_deenergized_p_mw": sum(deenergized_loads[key][0] for key in new_deenergized_loads),
               "newly_deenergized_q_mvar": sum(deenergized_loads[key][1] for key in new_deenergized_loads),
               "newly_deenergized_generation_p_mw": sum(deenergized_gens[key] for key in new_deenergized_gens),
               "min_u_pu": min((r["u_pu"] for r in voltage_rows), default=None),
               "max_u_pu": max((r["u_pu"] for r in voltage_rows), default=None),
               "voltage_by_bus": {r["model_id"]: r["u_pu"] for r in voltage_rows},
               "voltage_changes": voltage_rows, "branches": branches,
               "max_loading_pct": max((r["loading_pct"] for r in branches if r["loading_pct"] is not None), default=None),
               "max_apparent_loading_pct": max((r["apparent_loading_pct"] for r in branches if r["apparent_loading_pct"] is not None), default=None),
               "rating_missing_ids": sorted(set(missing_ratings)), "q_limits_missing_ids": sorted(q_missing),
               "q_limited_generators": q_limits, "severity_components": components,
               "severity": sum(components.values()), "solved_bus_count": len(voltage_rows),
               "unresolved_bus_count": len(unresolved),
               "p_loss_mw": sum(accepted_losses) if solved_ids else None,
               "reference_p_mw": sum(r["p_mw"] for r in reference_rows) if reference_rows else None,
               "reference_q_mvar": sum(r["q_mvar"] for r in reference_rows) if reference_rows else None,
               "retired_reference_ids": list(retired_reference_ids)}
    if automatic:
        disconnected_ids = [row['generator_id'] for row in generation_records]
        old_disconnected = set(base.get('disconnected_generator_ids', ()))
        new_generation = [row for row in generation_records if row['generator_id'] not in old_disconnected]
        new_unknown = [ident for ident in generation_unknown if ident not in old_disconnected]
        count_loaded = sum(has_demand(i,index) for i in topology['islands'])
        metrics.update(island_reference='largest_generator',
            disconnected_generator_ids=disconnected_ids,
            disconnected_generation=generation_records,
            disconnected_generation_unknown_ids=generation_unknown,
            disconnected_generation_p_mw=None if generation_unknown else sum(max(0.,row['p_mw']) for row in generation_records),
            disconnected_generation_q_mvar=None if generation_unknown else sum(row['q_mvar'] for row in generation_records),
            newly_disconnected_generator_ids=[row['generator_id'] for row in new_generation],
            newly_disconnected_generation_p_mw=None if new_unknown else sum(max(0.,row['p_mw']) for row in new_generation),
            newly_disconnected_generation_q_mvar=None if new_unknown else sum(row['q_mvar'] for row in new_generation),
            disconnected_generator_bus_ids=sorted({bus for island in topology['islands'] if island['id'] in generation_islands for bus in island['buses']}),
            operating_island_count=count_loaded)
        if any(i['status'] == 'unsolved' for i in islands):
            status = 'missing_reference'
        elif any(i['status'] == 'unsupplied' and set(i['bus_ids']) & set(newly_unresolved) for i in islands) or count_loaded > base.get('operating_island_count', count_loaded):
            status = 'islanded'
        elif not lf_summary['ok'] or any(i['status'] == 'not_converged' for i in islands):
            status = 'not_converged'
        elif violations:
            status = 'limit_violation'
        elif new_generation:
            status = 'generation_disconnected'
        else:
            status = 'completed'
        thermal = any(v['kind'] == 'loading' for v in violations)
        metrics['alert_priority'] = (0 if status == 'missing_reference' else 1 if status == 'islanded'
                                     else 2 if status == 'not_converged' else 3 if thermal
                                     else 4 if violations else 5 if new_generation else 6)
        return metrics, tuple(violations), diagnostics, status
    if retired_reference_ids:
        status = "missing_reference"
        for key in retired_reference_ids:
            diagnostics.append(diagnostic("missing_reference", "Angular reference was disconnected", **identity(index[key])))
    elif any(i["status"] == "unsolved" and set(i["bus_ids"]) & set(newly_unresolved) for i in islands):
        status = "missing_reference"
    elif any(i["status"] == "unsupplied" and set(i["bus_ids"]) & set(newly_unresolved) for i in islands):
        status = "islanded"
    elif not lf_summary["ok"] or any(i["status"] == "not_converged" for i in islands):
        status = "not_converged"
    elif violations:
        status = "limit_violation"
    else:
        status = "completed"
    metrics["alert_priority"] = (0 if status == "missing_reference" else 1 if status == "islanded"
                                 else 2 if status == "not_converged" else 3 if violations else 4)
    return metrics, tuple(violations), diagnostics, status
