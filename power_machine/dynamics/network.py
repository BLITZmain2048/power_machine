'Network support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import copy

import numpy as np

from power_machine import elements
from power_machine.loadflow import _stamp
from power_machine.topology import build_topology

from .switching import resolve_element_switches


class DynamicNetwork:
    'System-base Ybus with load-flow node fusion. lf_voltages maps REF bus IDs to p.u. phasors. Stamp static load admittances from explicit load-flow consumption.'

    def __init__(self, model: dict, lf_voltages: dict[str, complex] | None = None):
        self.model = copy.deepcopy(model)
        self.lf_voltages = dict(lf_voltages or {})
        self.s_base_mva = float(self.model["meta"]["s_base_mva"])
        if self.s_base_mva <= 0:
            raise _coded_error(ValueError, 'invalid_network_data', 's_base_mva must be positive')
        self.open_branches: set[str] = set()
        self.load_factors: dict[str, float] = {}
        self.faults: dict[str, complex] = {}
        self.switching_active = False
        self.rebuild()

    def rebuild(self) -> None:
        'Rebuild fused nodes and Ybus after a discrete network change.'
        self.revision = getattr(self, "revision", 0) + 1
        self.topology = build_topology(self.model, self.open_branches)
        self.node_ids = sorted(set(self.topology["bus_to_node"].values()))
        self.node_index = {node: i for i, node in enumerate(self.node_ids)}
        self.bus_to_node = self.topology["bus_to_node"]
        self.deenergized_bus_ids: set[str] = set()
        self.deenergized_node_ids: set[str] = set()
        self.deenergized_device_ids: set[str] = set()
        if self.switching_active:
            generators = {item["id"]: item for item in self.model.get("gens", [])}
            for island in self.topology["islands"]:
                # Una fuente local puede sostener una isla sin ser la referencia
                # angular global. Las islas sin generadores quedan a V=0.
                if (not set(island['buses']).intersection(getattr(self, 'voltage_boundary_ids', ()))
                        and not any(generators[gid].get("bus") in island["buses"]
                                    for gid in island["elements"]["gens"])):
                    self.deenergized_bus_ids.update(island["buses"])
                    self.deenergized_node_ids.update(island["nodes"])
                    self.deenergized_device_ids.update(
                        item_id for group in ("loads", "gens")
                        for item_id in island["elements"][group])
        self.ybus = np.zeros((len(self.node_ids), len(self.node_ids)), dtype=complex)
        buses = {item["id"]: item for item in self.model.get("buses", [])}
        items = {item["id"]: item for group in ("branches", "branches3", "shunts", "loads")
                 for item in self.model.get(group, [])}
        for group in ("branches", "branches3"):
            for item_id in self.topology["active"][group]:
                ids, _, matrix = _stamp(items[item_id], buses, self.s_base_mva)
                indices = [self.index_of_bus(bus_id) for bus_id in ids]
                # Agrega antes de sumar si bornes de una estampa están fusionados.
                for i in set(indices):
                    for j in set(indices):
                        self.ybus[i, j] += sum(matrix[a, b] for a, ia in enumerate(indices)
                                               for b, jb in enumerate(indices) if ia == i and jb == j)
        for item_id, connected in self.topology["dangling_branches"].items():
            item = items[item_id]
            ids, _, matrix = _stamp(item, buses, self.s_base_mva)
            other = 1 - connected
            if matrix[other, other] == 0:
                raise _coded_error(ValueError, 'invalid_network_data', f'Singular dangling branch: {item_id}')
            admittance = (matrix[connected, connected]
                          - matrix[connected, other] * matrix[other, connected]
                          / matrix[other, other])
            self.ybus[self.index_of_bus(ids[connected]), self.index_of_bus(ids[connected])] += admittance
        for item_id in self.topology["active"]["shunts"]:
            item = items[item_id]
            i = self.index_of_bus(item["bus"])
            self.ybus[i, i] += elements.y_shunt(item, buses[item["bus"]]["un_kv"],
                                                 self.s_base_mva, f_nom_hz=self.model.get("meta", {}).get("f_nom_hz"))
        for item_id in self.topology["active"]["loads"]:
            item = items[item_id]
            bus_id = item["bus"]
            if bus_id not in self.lf_voltages:
                raise _coded_error(ValueError, 'missing_network_data', f'Missing load-flow voltage for load {item_id} at {bus_id}')
            v0 = complex(self.lf_voltages[bus_id])
            if abs(v0) == 0:
                raise _coded_error(ValueError, 'invalid_network_data', f'Zero load-flow voltage at {bus_id}')
            p_mw, q_mvar = elements.load_pq_mw(item)
            factor = self.load_factors.get(item_id, 1.0)
            y_load = np.conj(complex(p_mw, q_mvar) / self.s_base_mva) * factor / abs(v0) ** 2
            i = self.index_of_bus(bus_id)
            self.ybus[i, i] += y_load
        self.solid_fault_indexes = set()
        for bus_id, z_fault_pu in self.faults.items():
            i = self.index_of_bus(bus_id)
            if z_fault_pu == 0:
                self.solid_fault_indexes.add(i)
            else:
                self.ybus[i, i] += 1 / z_fault_pu
        # Impone V=0 en cada nodo sin fuente y elimina el bloque singular.
        # En la isla pasiva las cargas de admitancia consumen cero a V=0.
        for node in self.deenergized_node_ids:
            i = self.node_index[node]
            self.ybus[i, :] = 0
            self.ybus[:, i] = 0
            self.ybus[i, i] = 1

    def index_of_bus(self, bus_id: str) -> int:
        try:
            return self.node_index[self.bus_to_node[bus_id]]
        except KeyError as exc:
            raise _coded_error(ValueError, 'missing_network_data', f'Bus is missing or out of service: {bus_id}') from exc

    def voltage_vector(self, bus_voltages: dict[str, complex]) -> np.ndarray:
        'Map REF bus voltages to fused nodes, averaging phasors when closing a coupler.'
        groups: dict[str, list[complex]] = {node: [] for node in self.node_ids}
        for bus_id, node in self.bus_to_node.items():
            if bus_id in bus_voltages:
                groups[node].append(complex(bus_voltages[bus_id]))
        missing = [node for node, values in groups.items() if not values]
        if missing:
            raise _coded_error(ValueError, 'missing_network_data', f'Missing initial voltages at nodes: {missing}')
        return np.asarray([sum(groups[node]) / len(groups[node]) for node in self.node_ids],
                          dtype=complex)

    def bus_voltages(self, voltage: np.ndarray) -> dict[str, complex]:
        voltage = np.asarray(voltage, dtype=complex)
        if voltage.size != len(self.node_ids):
            raise _coded_error(ValueError, 'invalid_network_data', 'Voltage dimension does not match the network')
        return {bus_id: complex(voltage[self.index_of_bus(bus_id)])
                for bus_id in self.bus_to_node}

    def set_branch_open(self, element_id: str, open_: bool = True) -> None:
        branches = {item["id"] for group in ("branches", "branches3")
                    for item in self.model.get(group, [])}
        switches = {item["id"]: item for item in self.model.get("switches", [])
                    if item.get("class") == "ElmCoup"}
        if element_id in switches:
            switches[element_id]["closed"] = not open_
        elif element_id in branches:
            if open_:
                self.open_branches.add(element_id)
            else:
                self.open_branches.discard(element_id)
        else:
            raise _coded_error(ValueError, 'invalid_network_data', f'Unknown branch or coupler: {element_id}')
        self.rebuild()

    def set_switch_open(self, switch_id: str, open_: bool = True) -> None:
        'Operate a StaSwitch or ElmCoup by REF ID without changing outserv.'
        switches = {item["id"]: item for item in self.model.get("switches", [])}
        item = switches.get(switch_id)
        if item is None or item.get("class") not in ("StaSwitch", "ElmCoup"):
            raise _coded_error(ValueError, 'invalid_network_data', f'Unknown switch: {switch_id}')
        item["closed"] = not open_
        self.switching_active = True
        self.rebuild()

    def set_element_switching(self, element_id: str, open_: bool = True) -> list[str]:
        'Resolve an element EvtSwitch into its explicit REF switching devices.'
        ids = resolve_element_switches(self.model, element_id)
        switches = {item["id"]: item for item in self.model.get("switches", [])}
        for switch_id in ids:
            switches[switch_id]["closed"] = not open_
        self.switching_active = True
        self.rebuild()
        return ids

    def set_load_factor(self, load_id: str, factor: float) -> None:
        if not np.isfinite(factor) or factor < 0:
            raise _coded_error(ValueError, 'invalid_network_data', 'Invalid load factor')
        if load_id not in {item["id"] for item in self.model.get("loads", [])}:
            raise _coded_error(ValueError, 'invalid_network_data', f'Unknown load: {load_id}')
        self.load_factors[load_id] = float(factor)
        self.rebuild()

    def set_fault(self, bus_id: str, z_fault_pu: complex | None) -> None:
        self.index_of_bus(bus_id)
        if z_fault_pu is None:
            self.faults.pop(bus_id, None)
        else:
            z = complex(z_fault_pu)
            if z.real < 0 or not np.isfinite([z.real, z.imag]).all():
                raise _coded_error(ValueError, 'invalid_network_data', 'Fault impedance must be finite with nonnegative resistance [p.u.]')
            self.faults[bus_id] = z
        self.rebuild()
