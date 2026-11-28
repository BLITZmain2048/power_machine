"""Cotejo reproducible de flujo de carga contra CSV de tablas de elementos."""

from __future__ import annotations

import csv
import json
import math
from collections import defaultdict
from pathlib import Path


def _rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _number(value):
    return None if value is None or value == "" else float(value)


def _delta(a, b):
    return None if a is None or b is None else a - b


def _angle_delta(a, b):
    return None if a is None or b is None else (a - b + 180) % 360 - 180


def _stats(rows, fields):
    result = {}
    for field in fields:
        values = sorted(abs(row[field]) for row in rows if row.get(field) is not None)
        result[field] = {"count": len(values), "max": values[-1] if values else None,
                         "p95": values[math.ceil(.95 * len(values)) - 1] if values else None}
    return result


def compare(result_dir: str | Path, reference_dir: str | Path, *, reference_bus_ids=None) -> dict:
    """Compara U [p.u.], ángulo [°], P [MW], Q [Mvar] por ID y extremo REF.

    Alinea cada isla con una barra de referencia explícita o marcada en REF.
    para islas sin ip_ctrl usa la primera barra común y lo indica.
    Celdas ausentes permanecen nulas y quedan fuera de las estadísticas.
    """
    result_dir, reference_dir = Path(result_dir), Path(reference_dir)
    model = json.loads((result_dir / "summary.json").read_text(encoding="utf-8"))
    ref_buses = {r["object_id"]: r for r in _rows(reference_dir / "ldf_buses.csv")}
    model_buses = {r["id"]: r for r in model["buses"]}
    ref_inj = _rows(reference_dir / "ldf_injections.csv")
    ref_buses = set(reference_bus_ids or ())
    if not ref_buses:
        ref_buses.update(r["terminal_id"] for r in ref_inj
                         if str(r.get("ip_ctrl", r.get("reference", ""))).lower() in ("1", "true")
                         and r.get("terminal_id"))
    offsets = {}
    alignment = {}
    for island in model["islands"]:
        common = sorted(b for b in island["buses"] if b in ref_buses
                        and b in model_buses and model_buses[b]["angle_deg"] is not None
                        and _number(ref_buses[b]["m:phiu"]) is not None)
        if not common:
            continue
        anchor = next((b for b in common if b in ref_buses), common[0])
        offsets[island["id"]] = _angle_delta(model_buses[anchor]["angle_deg"],
                                               _number(ref_buses[anchor]["m:phiu"]))
        alignment[island["id"]] = {"anchor_bus": anchor,
                                    "method": "ip_ctrl" if anchor in ref_buses else "primera_barra_comun",
                                    "offset_deg": offsets[island["id"]]}
    buses = []
    for bus_id, ref_entry in ref_buses.items():
        tw = model_buses.get(bus_id)
        ref_on = ref_entry["energizado"] == "1"
        tw_on = bool(tw and tw["status"] == "resuelto")
        du = _delta(tw["u_pu"] if tw_on else None, _number(ref_entry["m:u"]) if ref_on else None)
        da = (_angle_delta(tw["angle_deg"] - offsets[tw["island"]], _number(ref_entry["m:phiu"]))
              if ref_on and tw_on and tw["island"] in offsets else None)
        buses.append({"id": bus_id, "name": ref_entry["name"], "path": ref_entry["relative_path"],
                      "island": tw["island"] if tw else None, "ref_energized": ref_on,
                      "model_energized": tw_on, "model_status": tw["status"] if tw else "ausente",
                      "ref_u_pu": _number(ref_entry["m:u"]), "model_u_pu": tw["u_pu"] if tw else None,
                      "delta_u_pu": du, "ref_angle_deg": _number(ref_entry["m:phiu"]),
                      "model_angle_deg": tw["angle_deg"] if tw else None,
                      "delta_angle_deg": da,
                      "passes": du is not None and da is not None and abs(du) <= 1e-4 and abs(da) <= .01})
    model_branches = {r["id"]: r for r in model["branches"]}
    branches = []
    ref_branch_rows = _rows(reference_dir / "ldf_branches.csv")
    for ref_entry in ref_branch_rows:
        tw = model_branches.get(ref_entry["object_id"])
        port = ref_entry["port"]
        label = ({"bushv": "from", "buslv": "to"}.get(port)
                 if ref_entry["class"] == "ElmTr2" else
                 {"bus1": "from", "bus2": "to", "bushv": "hv",
                  "busmv": "mv", "buslv": "lv"}.get(port))
        pp, qq = _number(ref_entry["m:P:busX"]), _number(ref_entry["m:Q:busX"])
        tp = tw.get(f"p_{label}_mw") if tw and label else None
        tq = tw.get(f"q_{label}_mvar") if tw and label else None
        dp, dq = _delta(tp, pp), _delta(tq, qq)
        passes = (dp is not None and dq is not None and
                  abs(dp) <= max(.1, .001 * abs(pp)) and
                  abs(dq) <= max(.1, .001 * abs(qq)))
        branches.append({"id": ref_entry["object_id"], "name": ref_entry["name"], "path": ref_entry["relative_path"],
                         "port": port, "terminal_id": ref_entry["terminal_id"],
                         "ref_p_mw": pp, "model_p_mw": tp, "delta_p_mw": dp,
                         "ref_q_mvar": qq, "model_q_mvar": tq, "delta_q_mvar": dq,
                         "model_status": tw["status"] if tw else "ausente", "passes": passes})
    ideal_checks = []
    for item_id in model.get("ideal_lines", []):
        ref_ports = [r for r in ref_branch_rows if r["object_id"] == item_id]
        ref_ports.sort(key=lambda r: r["port"])
        if len(ref_ports) == 2:
            first, second = ref_ports
            b1, b2 = ref_buses.get(first["terminal_id"]), ref_buses.get(second["terminal_id"])
            u1, u2 = (_number(b1["m:u"]) if b1 else None,
                      _number(b2["m:u"]) if b2 else None)
            a1, a2 = (_number(b1["m:phiu"]) if b1 else None,
                      _number(b2["m:phiu"]) if b2 else None)
            p1, p2 = _number(first["m:P:busX"]), _number(second["m:P:busX"])
            q1, q2 = _number(first["m:Q:busX"]), _number(second["m:Q:busX"])
            du = abs(u1-u2) if u1 is not None and u2 is not None else None
            da = abs(_angle_delta(a1, a2)) if a1 is not None and a2 is not None else None
            ploss = abs(p1+p2) if p1 is not None and p2 is not None else None
            qloss = abs(q1+q2) if q1 is not None and q2 is not None else None
        else:
            du = da = ploss = qloss = None
        ideal_checks.append({"id": item_id, "delta_u_pu": du, "delta_angle_deg": da,
                             "p_loss_mw": ploss, "q_loss_mvar": qloss,
                             "confirmed": (du is not None and da is not None and
                                           ploss is not None and qloss is not None and
                                           du <= 1e-6 and da <= 1e-5 and
                                           ploss <= 1e-6 and qloss <= 1e-6)})
    model_inj = {r["id"]: r for group in ("gens", "loads") for r in model[group]}
    injections = []
    for ref_entry in ref_inj:
        tw = model_inj.get(ref_entry["object_id"])
        pp, qq = _number(ref_entry["m:P:bus1"]), _number(ref_entry["m:Q:bus1"])
        tp, tq = (tw.get("p_mw"), tw.get("q_mvar")) if tw else (None, None)
        # La potencia REF de carga es positiva consumida, igual que el resultado de loads.
        dp, dq = _delta(tp, pp), _delta(tq, qq)
        injections.append({"id": ref_entry["object_id"], "name": ref_entry["name"], "path": ref_entry["relative_path"],
                           "class": ref_entry["class"], "terminal_id": ref_entry["terminal_id"],
                           "ref_p_mw": pp, "model_p_mw": tp, "delta_p_mw": dp,
                           "ref_q_mvar": qq, "model_q_mvar": tq, "delta_q_mvar": dq,
                           "model_status": tw["status"] if tw else "ausente",
                           "passes": dp is not None and dq is not None and abs(dp) <= .1 and abs(dq) <= .1})
    energized = [r for r in buses if r["ref_energized"]]
    summary = {"criteria": {"bus_u_pu": 1e-4, "bus_angle_deg": .01,
                            "branch_p_mw": "max(0.1,0.001*abs(P_REF))",
                            "branch_q_mvar": "max(0.1,0.001*abs(Q_REF))",
                            "generator_pq_mw_mvar": .1},
               "buses": {"ref_energized": len(energized),
                         "model_energized": sum(r["model_energized"] for r in buses),
                         "both_energized": sum(r["ref_energized"] and r["model_energized"] for r in buses),
                         "passes": sum(r["passes"] for r in energized),
                         "ref_only": [r["id"] for r in buses if r["ref_energized"] and not r["model_energized"]],
                         "model_only": [r["id"] for r in buses if r["model_energized"] and not r["ref_energized"]],
                         "errors": _stats(buses, ("delta_u_pu", "delta_angle_deg"))},
               "branches": {"rows": len(branches),
                            "ref_measured": sum(r["ref_p_mw"] is not None and r["ref_q_mvar"] is not None for r in branches),
                            "comparable": sum(r["delta_p_mw"] is not None and r["delta_q_mvar"] is not None for r in branches),
                            "passes": sum(r["passes"] for r in branches),
                            "errors": _stats(branches, ("delta_p_mw", "delta_q_mvar"))},
               "injections": {"rows": len(injections),
                              "ref_measured": sum(r["ref_p_mw"] is not None and r["ref_q_mvar"] is not None for r in injections),
                              "comparable": sum(r["delta_p_mw"] is not None and r["delta_q_mvar"] is not None for r in injections),
                              "passes": sum(r["passes"] for r in injections),
                              "errors": _stats(injections, ("delta_p_mw", "delta_q_mvar"))},
               "islands": {"model": len(model["islands"]), "ref": None,
                           "model_unsupplied": sum(r["status"] == "sin_referencia" for r in model["islands"]),
                           "ref_unsupplied": None},
               "alignment": alignment, "omitted_branches": model["omitted_branches"],
               "ideal_lines": ideal_checks}
    return {"buses": buses, "branches": branches, "injections": injections, "summary": summary}


def write_comparison(comparison: dict, out_dir: str | Path) -> None:
    """Escribe CSV de diferencias y resumen JSON, conservando los nulos."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for group in ("buses", "branches", "injections"):
        rows = comparison[group]
        with (out_dir / f"{group}.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
            writer.writeheader()
            writer.writerows(rows)
    (out_dir / "summary.json").write_text(
        json.dumps(comparison["summary"], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
