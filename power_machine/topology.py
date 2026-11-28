"Electrical connectivity after switching and ideal-link contraction."

from __future__ import annotations

from collections import defaultdict
from .elements import series_bypassed


def ideal_line_candidate(branch: dict) -> bool:
    "Return whether an untyped ElmLne has zero R1, X1 and B1. Confirm ideal links against REF."
    p = branch.get("params", {})
    return (branch.get("class") == "ElmLne" and branch.get("outserv", 0) != 1
            and not branch.get("type_id") and not branch.get("type_class")
            and not branch.get("type_params")
            and all(p.get(key) == 0 for key in ("R1", "X1", "B1")))


def ideal_zpu_candidate(branch: dict) -> bool:
    "Identify neutral, unshunted near-zero ElmZpu links in their equipment pu base."
    if branch.get("class") != "ElmZpu" or branch.get("outserv", 0) == 1:
        return False
    p = branch.get("params", {})
    keys = ("r_pu", "x_pu", "r_pu_ji", "x_pu_ji", "uratio", "nphshift",
            "ag", "gi_pu", "bi_pu", "gj_pu", "bj_pu")
    if any(p.get(key) is None for key in keys):
        return False
    return (abs(complex(p["r_pu"], p["x_pu"])) < 1e-6
            and abs(complex(p["r_pu_ji"], p["x_pu_ji"])) < 1e-6
            and p["uratio"] == 1 and p["nphshift"] == 0 and p["ag"] == 0
            and all(p[key] == 0 for key in ("gi_pu", "bi_pu", "gj_pu", "bj_pu")))


def build_topology(model: dict, open_branches=()) -> dict:
    "Return fused nodes, islands and active elements while preserving original IDs.\n\nopen_branches excludes incomplete branches in diagnostic mode. An open\nStaSwitch disconnects only its declared terminal."
    buses = {b["id"]: b for b in model.get("buses", []) if b.get("outserv", 0) != 1}
    parent = {bus_id: bus_id for bus_id in buses}
    open_branches = set(open_branches)
    blocked = {(s.get("element_id"), s.get("port")) for s in model.get("switches", [])
               if s.get("class") == "StaSwitch" and s.get("outserv", 0) != 1
               and not s.get("closed", False) and s.get("element_id") and s.get("port")}

    def root(bus_id):
        while parent[bus_id] != bus_id:
            parent[bus_id] = parent[parent[bus_id]]
            bus_id = parent[bus_id]
        return bus_id

    def union(a, b):
        a, b = root(a), root(b)
        if a != b:
            parent[max(a, b)] = min(a, b)

    def port_ok(element, key, port):
        return element.get(key) in buses and (element["id"], port) not in blocked

    ideal_edges = []
    ideal_disconnected = []
    for switch in model.get("switches", []):
        if (switch.get("class") == "ElmCoup" and switch.get("outserv", 0) != 1
                and switch.get("closed", False)
                and port_ok(switch, "bus1", "bus1")
                and port_ok(switch, "bus2", "bus2")):
            union(switch["bus1"], switch["bus2"])
            ideal_edges.append({"id": switch["id"], "class": "ElmCoup",
                                "bus_from": switch["bus1"], "bus_to": switch["bus2"]})
    for branch in model.get("branches", []):
        if branch["id"] in open_branches or not (
                ideal_line_candidate(branch) or ideal_zpu_candidate(branch)
                or (branch.get("class") in ("ElmScap", "ElmSind")
                    and branch.get("outserv", 0) != 1
                    and series_bypassed(branch))):
            continue
        if (port_ok(branch, "bus_from", "bus1") and
                port_ok(branch, "bus_to", "bus2")):
            union(branch["bus_from"], branch["bus_to"])
            ideal_edges.append({"id": branch["id"], "class": branch["class"],
                                "bus_from": branch["bus_from"], "bus_to": branch["bus_to"]})
        else:
            ideal_disconnected.append(branch["id"])

    bus_to_node = {bus_id: root(bus_id) for bus_id in buses}
    node_graph = {node: set() for node in bus_to_node.values()}
    active = {group: [] for group in ("branches", "branches3", "shunts", "loads", "gens")}
    dangling = {}

    for group in ("branches", "branches3"):
        for item in model.get(group, []):
            if (item.get("outserv", 0) == 1 or item["id"] in open_branches
                    or item["id"] in ideal_disconnected
                    or any(edge["id"] == item["id"] for edge in ideal_edges)):
                continue
            if group == "branches3":
                ports = (("bus_hv", "bushv"), ("bus_mv", "busmv"), ("bus_lv", "buslv"))
            elif item["class"] == "ElmTr2":
                ports = (("bus_from", "bushv"), ("bus_to", "buslv"))
            else:
                ports = (("bus_from", "bus1"), ("bus_to", "bus2"))
            connected = [port_ok(item, key, port) for key, port in ports]
            if not all(connected):
                if (group == "branches" and item["class"] == "ElmLne"
                        and sum(connected) == 1):
                    dangling[item["id"]] = connected.index(True)
                continue
            nodes = [bus_to_node[item[key]] for key, _ in ports]
            for node in nodes[1:]:
                node_graph[nodes[0]].add(node)
                node_graph[node].add(nodes[0])
            active[group].append(item["id"])

    for group in ("shunts", "loads", "gens"):
        for item in model.get(group, []):
            if (item.get("outserv", 0) != 1 and item.get("bus") in buses
                    and (item["id"], "bus1") not in blocked
                    and not item.get("seq0_only", False)):
                active[group].append(item["id"])

    islands = []
    node_to_island = {}
    for start in sorted(node_graph):
        if start in node_to_island:
            continue
        island_id = len(islands)
        stack = [start]
        nodes = []
        while stack:
            node = stack.pop()
            if node in node_to_island:
                continue
            node_to_island[node] = island_id
            nodes.append(node)
            stack.extend(node_graph[node] - node_to_island.keys())
        islands.append({"id": island_id, "nodes": sorted(nodes), "buses": [],
                        "elements": {group: [] for group in active},
                        "status": "sin_referencia", "reference_gen": None})
    for bus_id, node in bus_to_node.items():
        islands[node_to_island[node]]["buses"].append(bus_id)
    lookup = {item["id"]: item for group in active for item in model.get(group, [])}
    for group, ids in active.items():
        for item_id in ids:
            item = lookup[item_id]
            bus_id = item.get("bus") or item.get("bus_from") or item.get("bus_hv")
            island = islands[node_to_island[bus_to_node[bus_id]]]
            island["elements"][group].append(item_id)
            if group == "gens" and item.get("ref") and island["reference_gen"] is None:
                island["reference_gen"] = item_id
                island["status"] = "con_referencia"
    for island in islands:
        island["buses"].sort()
    return {"bus_to_node": bus_to_node, "node_to_island": node_to_island,
            "islands": islands, "active": active, "dangling_branches": dangling,
            "blocked_ports": sorted(blocked), "ideal_edges": ideal_edges,
            "ideal_disconnected": ideal_disconnected}
