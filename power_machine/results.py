"""Resultados de ramas y exportación portátil del flujo de potencia."""

from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np


def branch_result(item: dict, voltages: list[complex], admittance: np.ndarray,
                  nominal_kv: list[float], s_base_mva: float, island_id: int) -> dict:
    """Potencias salientes [MW/Mvar] y corrientes [kA] por cada extremo."""
    currents = admittance @ np.asarray(voltages)
    powers = np.asarray(voltages) * np.conj(currents) * s_base_mva
    result = {"id": item["id"], "name": item["name"], "class": item["class"],
              "path": item.get("path"), "island": island_id, "status": "resuelto"}
    labels = ("from", "to") if len(voltages) == 2 else ("hv", "mv", "lv")
    bus_keys = ("bus_from", "bus_to") if len(voltages) == 2 else ("bus_hv", "bus_mv", "bus_lv")
    for k, label in enumerate(labels):
        result[f"bus_{label}"] = item[bus_keys[k]]
        result[f"p_{label}_mw"] = float(powers[k].real)
        result[f"q_{label}_mvar"] = float(powers[k].imag)
        result[f"i_{label}_ka"] = float(abs(currents[k]) * s_base_mva /
                                        (math.sqrt(3) * nominal_kv[k]))
    result["p_loss_mw"] = float(sum(powers).real)
    result["q_loss_mvar"] = float(sum(powers).imag)
    return result


def reconstruct_ideal_flows(ideal_edges: list[dict], bus_to_node: dict,
                            node_to_island: dict, voltages: dict,
                            buses_by_id: dict, branches: list[dict],
                            gens: list[dict], loads: list[dict],
                            shunt_power: dict, passive_nodes=()) -> tuple[list[dict], list[str]]:
    """Recupera S saliente [MW+j Mvar] por KCL; en mallas usa mínima norma.

    Las potencias de extremos ideales tienen pérdidas nulas. La solución en
    una malla no es única: la convención de mínima norma queda explícita.
    """
    residual = {bus_id: 0j for bus_id in bus_to_node}
    for row in gens:
        if row.get("p_mw") is not None:
            residual[row["bus"]] += complex(row["p_mw"], row["q_mvar"])
    for row in loads:
        if row.get("p_mw") is not None:
            residual[row["bus"]] -= complex(row["p_mw"], row["q_mvar"])
    for bus_id, power in shunt_power.items():
        residual[bus_id] -= power
    for row in branches:
        for label in ("from", "to", "hv", "mv", "lv"):
            bus_id = row.get("bus_" + label)
            p, q = row.get("p_" + label + "_mw"), row.get("q_" + label + "_mvar")
            if bus_id in residual and p is not None and q is not None:
                residual[bus_id] -= complex(p, q)

    by_node = {}
    for edge in ideal_edges:
        node = bus_to_node[edge["bus_from"]]
        by_node.setdefault(node, []).append(edge)
    rows, warnings = [], []
    passive_nodes = set(passive_nodes)
    for node, edges in by_node.items():
        passive = node not in voltages and node in passive_nodes
        if node not in voltages and not passive:
            continue
        vertices = sorted({bus for edge in edges for bus in
                           (edge["bus_from"], edge["bus_to"])})
        index = {bus: i for i, bus in enumerate(vertices)}
        incidence = np.zeros((len(vertices), len(edges)))
        for j, edge in enumerate(edges):
            incidence[index[edge["bus_from"]], j] = 1.0
            incidence[index[edge["bus_to"]], j] = -1.0
        demand = np.array([residual[bus] for bus in vertices])
        flows = (np.zeros(len(edges), complex) if passive else
                 np.linalg.lstsq(incidence, demand.real, rcond=None)[0]
                 + 1j * np.linalg.lstsq(incidence, demand.imag, rcond=None)[0])
        mismatch = float(np.max(np.abs(incidence @ flows - demand)))
        mesh = len(edges) > len(vertices) - 1
        if mesh and not passive:
            warnings.append(f"Node {node}: {len(edges)} ideal connections in a mesh; minimum-norm flows are not unique")
        if mismatch > 1e-4:
            warnings.append(f"Node {node}: ideal-connection KCL residual {mismatch:.6g} MVA")
        for edge, flow in zip(edges, flows):
            v = voltages.get(node, 0j)
            row = {"id": edge["id"], "name": edge.get("name", edge["id"]),
                   "class": edge["class"], "path": edge.get("path"),
                   "island": node_to_island[node],
                   "status": "sin_energia_pasiva" if passive else
                             ("malla_no_unica" if mesh else "resuelto"),
                   "bus_from": edge["bus_from"], "bus_to": edge["bus_to"],
                   "p_from_mw": float(flow.real), "q_from_mvar": float(flow.imag),
                   "p_to_mw": float(-flow.real), "q_to_mvar": float(-flow.imag),
                   "p_loss_mw": 0.0, "q_loss_mvar": 0.0}
            for label, bus_id in (("from", edge["bus_from"]), ("to", edge["bus_to"])):
                un_kv = float(buses_by_id[bus_id]["un_kv"])
                row["i_" + label + "_ka"] = (float(abs(flow) /
                    (math.sqrt(3) * un_kv * abs(v))) if abs(v) else
                    (0.0 if passive else None))
            rows.append(row)
    return rows, warnings


def write_results(result: dict, prefix: str | Path) -> list[Path]:
    """Escribe tablas CSV y resumen JSON bajo el prefijo dado."""
    prefix = Path(prefix)
    prefix.mkdir(parents=True, exist_ok=True)
    paths = []
    for group in ("buses", "branches", "gens"):
        path = prefix / f"{group}.csv"
        rows = result[group]
        fields = list(dict.fromkeys(key for row in rows for key in row))
        with path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)
        paths.append(path)
    path = prefix / "summary.json"
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    paths.append(path)
    return paths
