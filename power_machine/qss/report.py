"""CSV nativos y resumen. Unidades en columnas y signos en el contrato JSON."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from ..lf_status import energized_status
from .dispatch import apparent_limit


TIME = ["step_index", "timestamp_iso", "t_s"]
IDENTITY = ["object_id", "relative_path", "name"]
SCHEMAS = {
    "bus_voltage": TIME + IDENTITY + ["status", "u_pu", "u_kv", "angle_deg"],
    "branch_flows": TIME + IDENTITY + ["status", "end", "bus_object_id", "bus_relative_path",
                                       "p_out_mw", "q_out_mvar", "i_ka"],
    "generation": TIME + IDENTITY + ["status", "reference", "p_injected_mw", "q_injected_mvar",
                                     "s_mva", "s_rated_mva", "limit_flag"],
    "dispatch_p_mw": TIME + IDENTITY + ["reference", "lambda_pu", "p_scheduled_injected_mw",
                                       "p_scheduled_per_unit_injected_mw", "n_units",
                                       "p_solved_injected_mw", "previous_loss_p_mw", "loss_source_index"],
    "load_factors": TIME + IDENTITY + ["factor_pu", "scale0_pu", "p_consumed_mw", "q_consumed_mvar"],
    "totals": TIME + ["converged", "global_converged", "load_consumed_p_mw", "load_consumed_q_mvar",
                      "generation_injected_p_mw", "generation_injected_q_mvar", "loss_p_mw",
                      "loss_q_mvar", "balance_p_mw", "lambda_pu", "previous_loss_p_mw", "loss_source_index"],
    "convergence": TIME + ["converged", "global_converged", "accepted", "collapse", "iterations", "max_mismatch_mva",
                           "jacobian_condition_pu", "elapsed_s", "reason"],
    "violations": TIME + IDENTITY + ["u_pu", "lower_u_pu", "upper_u_pu", "kind"],
}


def _energy(rows, field):
    """Trapecio [MW*s/3600 = MWh], sin integrar a través de ausencias."""
    energy_mwh = covered_s = 0.0
    missing_segments = 0
    for a, b in zip(rows, rows[1:]):
        if a[field] is None or b[field] is None:
            missing_segments += 1
            continue
        dt_s = b["t_s"] - a["t_s"]
        energy_mwh += (a[field] + b[field]) * 0.5 * dt_s / 3600
        covered_s += dt_s
    return {"mwh": energy_mwh if missing_segments == 0 else None,
            "covered_mwh": energy_mwh, "covered_s": covered_s,
            "missing_segments": missing_segments}


def write_report(output, model, records, metadata, *, elapsed_s):
    """Exporta ausencias como celdas vacías, nunca suma ceros de islas fallidas.

    Ángulos [grados] respecto de la referencia de la LF, corriente [kA]
    como magnitud. P/Q por extremo positivos saliendo de su barra.
    """
    from ..elements import load_pq_mw
    output = Path(output)
    index = {x["id"]: x for group in ("buses", "branches", "branches3", "switches", "gens", "loads")
             for x in model.get(group, [])}

    def identity(item_id):
        x = index[item_id]
        return {"object_id": x["object_id"], "relative_path": x["relative_path"], "name": x["name"]}

    tables = {name: [] for name in SCHEMAS}
    reference_exceedances = []
    for record in records:
        time_row = {"step_index": record["index"], "timestamp_iso": record["timestamp_iso"], "t_s": record["t_s"]}
        result, plan = record["result"], record["plan"]

        def add(table, fields, item_id=None):
            row = {**time_row, **(identity(item_id) if item_id is not None else {}), **fields}
            tables[table].append(row)
            return row

        for bus in result["buses"]:
            add("bus_voltage", {k: bus[k] for k in ("status", "u_pu", "u_kv", "angle_deg")}, bus["id"])
            if bus["u_pu"] is not None and not 0.95 <= bus["u_pu"] <= 1.05:
                add("violations", {"u_pu": bus["u_pu"], "lower_u_pu": 0.95, "upper_u_pu": 1.05,
                                   "kind": "undervoltage" if bus["u_pu"] < 0.95 else "overvoltage"}, bus["id"])
        for branch in result["branches"]:
            for end in ("from", "to", "hv", "mv", "lv"):
                bus_id = branch.get("bus_" + end)
                if bus_id is not None:
                    add("branch_flows", {"status": branch["status"], "end": end, "bus_object_id": bus_id,
                                         "bus_relative_path": index[bus_id]["relative_path"],
                                         "p_out_mw": branch["p_" + end + "_mw"],
                                         "q_out_mvar": branch["q_" + end + "_mvar"],
                                         "i_ka": branch["i_" + end + "_ka"]}, branch["id"])
        solved = {g["id"]: g for g in result["gens"]}
        for gen_id, gen in solved.items():
            item = index[gen_id]
            s_mva, limit = apparent_limit(gen["p_mw"], gen["q_mvar"], item.get("s_rated_mva"))
            row = add("generation", {"status": gen["status"], "reference": bool(item.get("ref")),
                                     "p_injected_mw": gen["p_mw"], "q_injected_mvar": gen["q_mvar"],
                                     "s_mva": s_mva, "s_rated_mva": item.get("s_rated_mva"), "limit_flag": limit}, gen_id)
            if limit and item.get("ref"):
                reference_exceedances.append(row)
            p_scheduled_mw = (plan["reference_expected_p_mw"] if item.get("ref") else
                              plan["scenario"]["gen_p_mw"][gen_id])
            n_units = item.get("params", {}).get("ngnum", item.get("n_units", 1))
            add("dispatch_p_mw", {"reference": bool(item.get("ref")), "lambda_pu": plan["lambda_pu"],
                                  "p_scheduled_injected_mw": p_scheduled_mw,
                                  "p_scheduled_per_unit_injected_mw": p_scheduled_mw / n_units,
                                  "n_units": n_units, "p_solved_injected_mw": gen["p_mw"],
                                  "previous_loss_p_mw": plan["previous_loss_p_mw"],
                                  "loss_source_index": record["loss_source_index"]}, gen_id)
        for load in model["loads"]:
            factor = record["factors"][load["id"]]
            p_mw, q_mvar = load_pq_mw(load)
            active = not load.get("outserv", 0)
            add("load_factors", {"factor_pu": factor, "scale0_pu": plan["scenario"]["load_scale"][load["id"]],
                                 "p_consumed_mw": p_mw * factor if active else 0.0,
                                 "q_consumed_mvar": q_mvar * factor if active else 0.0}, load["id"])
        totals = result["totals"]
        status = record.get("energized_status") or energized_status(result)
        success = status["ok"]
        p_gen_mw = totals["generation_p_mw"] if success else None
        p_loss_mw = totals["branch_loss_p_mw"] if success else None
        add("totals", {"converged": success, "global_converged": result["converged"], "load_consumed_p_mw": plan["load_p_mw"],
                       "load_consumed_q_mvar": plan["load_q_mvar"], "generation_injected_p_mw": p_gen_mw,
                       "generation_injected_q_mvar": totals["generation_q_mvar"] if success else None,
                       "loss_p_mw": p_loss_mw, "loss_q_mvar": totals["branch_loss_q_mvar"] if success else None,
                       "balance_p_mw": p_gen_mw - plan["load_p_mw"] - p_loss_mw if success else None,
                       "lambda_pu": plan["lambda_pu"], "previous_loss_p_mw": plan["previous_loss_p_mw"],
                       "loss_source_index": record["loss_source_index"]})
        add("convergence", {"converged": success, "global_converged": result["converged"], "accepted": record["accepted"], "collapse": record["collapse"],
                            "iterations": result["iterations"], "max_mismatch_mva": result["max_mismatch_mva"],
                            "jacobian_condition_pu": result["jacobian_condition"], "elapsed_s": record["elapsed_s"],
                            "reason": " | ".join(i.get("reason", i["status"]) for i in result["islands"])})
    for name, rows in tables.items():
        with (output / (name + ".csv")).open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, SCHEMAS[name])
            writer.writeheader()
            writer.writerows(rows)
    available = [row for row in tables["bus_voltage"] if row["u_pu"] is not None]
    failures = [r for r in records if r["failure"]]
    totals = tables["totals"]
    peak = max(totals, key=lambda row: row["load_consumed_p_mw"])
    summary = {**metadata, "units_and_signs": {
        "time": "t_s y elapsed_s en segundos, timestamp_iso calendario ISO 8601",
        "bus": "u_pu adimensional, u_kv magnitud kV, angle_deg grados respecto de la referencia LF",
        "load": "P [MW] y Q [Mvar] positivos consumidos, factores adimensionales",
        "generation": "P [MW] y Q [Mvar] positivos inyectados, S y nominal en MVA",
        "branch": "P [MW] y Q [Mvar] positivos saliendo de la barra del extremo, i_ka magnitud kA",
        "loss": "P [MW] y Q [Mvar] suma de extremos, Q puede ser negativa por capacitancia",
        "balance": "balance_p_mw = generación inyectada - carga consumida - pérdidas",
        "energy": "MWh por trapecio con dt_s/3600, pérdidas ausentes no se interpolan",
        "dispatch_reference": "P programada de referencia es expectativa con pérdidas previas, P resuelta absorbe residuo",
        "dispatch_per_unit": "P programada total dividida por ngnum, no es potencia p.u.",
        "missing": "CSV vacío y JSON null son ausencias, no cero",
    }, "rows_per_csv": {name: len(rows) for name, rows in tables.items()},
        "elapsed_s": elapsed_s, "steps_executed": len(records),
        "nonconverged_steps": [r["index"] for r in records
                                if not (r.get("energized_status") or energized_status(r["result"]))["ok"]],
        "unaccepted_steps": [r["index"] for r in records if not r["accepted"]],
        "first_failure": ({k: failures[0][k] for k in ("index", "timestamp_iso", "t_s", "factors", "collapse")}
                          if failures else None),
        "stress_search": {"last_index_examined": records[-1]["index"],
                          "no_failure_in_examined_interval": not failures},
        "load_energy": _energy(totals, "load_consumed_p_mw"),
        "loss_energy": _energy(totals, "loss_p_mw"), "peak_load": peak,
        "umin": min(available, key=lambda row: row["u_pu"]) if available else None,
        "umax": max(available, key=lambda row: row["u_pu"]) if available else None,
        "voltage_violation_count": len(tables["violations"]),
        "voltage_violation_definition": "una violación por barra e instante fuera de [0.95, 1.05] p.u.",
        "voltage_missing_count": sum(row["u_pu"] is None for row in tables["bus_voltage"]),
        "reference_apparent_exceedance_count": len(reference_exceedances),
        "reference_apparent_exceedances": reference_exceedances,
        "max_mismatch_mva_accepted": max((r["result"]["max_mismatch_mva"] for r in records if r["accepted"]), default=None),
        "max_abs_balance_p_mw": max((abs(row["balance_p_mw"]) for row in totals if row["balance_p_mw"] is not None), default=None),
        "thermal_loading": "no calculada, sin ratings térmicos validados"}
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return summary
