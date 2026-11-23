'Switching support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from collections import defaultdict, deque

from power_machine.topology import ideal_line_candidate, ideal_zpu_candidate


def resolve_element_switches(model: dict, element_id: str) -> list[str]:
    'Return switching-device IDs at both terminals. Traverse only closed ideal connections to the first reachable devices. Preserve equal-distance ties. Missing switching data must be explicit.'
    elements = {item["id"]: item for group in ("branches", "branches3")
                for item in model.get(group, [])}
    element = elements.get(element_id)
    if element is None:
        raise _coded_error(ValueError, 'invalid_switching_data', f'Unknown switching element: {element_id}')
    if element.get("outserv", 0) == 1:
        raise _coded_error(ValueError, 'invalid_switching_data', f'Element is out of service: {element_id}')
    if element.get("class") == "ElmTr2":
        ports = (("bus_from", "bushv"), ("bus_to", "buslv"))
    elif element.get("class") == "ElmTr3":
        ports = (("bus_hv", "bushv"), ("bus_mv", "busmv"), ("bus_lv", "buslv"))
    else:
        ports = (("bus_from", "bus1"), ("bus_to", "bus2"))

    switches = [item for item in model.get("switches", [])
                if item.get("outserv", 0) != 1]
    by_port: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for item in switches:
        if item.get("class") == "StaSwitch" and item.get("element_id") and item.get("port"):
            by_port[item["element_id"], item["port"]].append(item)

    adjacency: dict[str, list[tuple[str, str, str, bool]]] = defaultdict(list)
    for branch in model.get("branches", []):
        if branch["id"] == element_id or not (
                ideal_line_candidate(branch) or ideal_zpu_candidate(branch)):
            continue
        a, b = branch.get("bus_from"), branch.get("bus_to")
        if a and b:
            adjacency[a].append((b, branch["id"], "bus1", False))
            adjacency[b].append((a, branch["id"], "bus2", False))
    for coup in switches:
        if coup.get("class") != "ElmCoup" or not coup.get("closed", False):
            continue
        a, b = coup.get("bus1"), coup.get("bus2")
        if a and b:
            adjacency[a].append((b, coup["id"], "bus1", True))
            adjacency[b].append((a, coup["id"], "bus2", True))

    selected: set[str] = set()
    for bus_key, port in ports:
        direct = by_port[element_id, port]
        if direct:
            selected.update(item["id"] for item in direct)
            continue
        start = element.get(bus_key)
        if not start:
            raise _coded_error(ValueError, 'invalid_switching_data', f'Terminal has no bus: {element_id}/{port}')
        queue = deque([(start, 0)])
        seen = {start}
        nearest = None
        found: set[str] = set()
        while queue:
            bus, distance = queue.popleft()
            if nearest is not None and distance >= nearest:
                continue
            for other, edge_id, edge_port, is_coup in adjacency[bus]:
                apparatus = ([edge_id] if is_coup else
                             [s["id"] for s in by_port[edge_id, edge_port]])
                if apparatus:
                    nearest = distance + 1
                    found.update(apparatus)
                    continue
                opposite = "bus2" if edge_port == "bus1" else "bus1"
                if by_port[edge_id, opposite]:
                    found.update(s["id"] for s in by_port[edge_id, opposite])
                    nearest = distance + 1
                elif other not in seen:
                    seen.add(other)
                    queue.append((other, distance + 1))
        if not found:
            raise _coded_error(ValueError, 'invalid_switching_data', f'No reachable switch from {element_id}/{port}. The search only traverses ideal links. Specify explicit switch IDs')
        selected.update(found)
    return sorted(selected)
