'Controllers support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass
from typing import Mapping

import numpy as np

from .dsl_compile import CompiledDSL, DSLCompileError, DynamicCatalog, _path
from .dsl_expr import linear_lookup
from .synchronous import SynchronousMachine
from .measurements import VoltageMeasurement


@dataclass(frozen=True)
class ControllerCoverage:
    object_id: str
    relative_path: str
    status: str
    blocks: tuple[tuple[str, str, str], ...]


def _machine_comp(catalog: DynamicCatalog, machine: SynchronousMachine) -> dict[str, str]:
    matches = []
    for row in catalog.objects.values():
        if row["class"] != "ElmComp" or row["relative_path"].startswith("Library."):
            continue
        if any(_path(element) == machine.relative_path for element in
               catalog.attrs(row).get("pelm", ()) if element is not None):
            matches.append(row)
    if len(matches) != 1:
        raise _coded_error(DSLCompileError, 'invalid_controllers_data', f'Expected one ElmComp for {machine.relative_path}, found {len(matches)} matches')
    return matches[0]


def _frame_wire(catalog: DynamicCatalog, comp: Mapping[str, str],
                source_index: int, target_index: int,
                source_signal: str, target_signal: str) -> None:
    a = catalog.attrs(comp)
    slots = a.get("pblk", ())
    if max(source_index, target_index) >= len(slots):
        raise _coded_error(DSLCompileError, 'invalid_controllers_data', f"Incomplete pblk: {comp['relative_path']}")
    source_path, target_path = _path(slots[source_index]), _path(slots[target_index])
    frame_path = _path(a.get("typ_id"))
    if not frame_path:
        raise _coded_error(DSLCompileError, 'invalid_controllers_data', f"ElmComp has no frame: {comp['relative_path']}")
    frame = catalog.object_at(frame_path)
    for row in catalog.children.get(frame["object_id"], ()):
        if row["class"] != "BlkSig":
            continue
        wire = catalog.attrs(row)
        if (_path(wire.get("pnodfrom")) == source_path and
            _path(wire.get("pnodto")) == target_path and
            wire.get("GetFromSigName()") == source_signal and
            wire.get("GetToSigName()") == target_signal):
            return
    raise _coded_error(DSLCompileError, 'invalid_controllers_data', f'Frame has no wire {source_signal}->{target_signal}: {frame_path}')


class ControlledSynchronousMachine:
    'A DynamicDevice combining a machine and its connected controls in one state vector. init receives total load-flow S=P+jQ in MVA. Bus voltage uses bus-base p.u., while ve and pt retain the machine conventions.'

    n_alg = 0

    def __init__(self, machine: SynchronousMachine, catalog: DynamicCatalog, *, configuration=None):
        self.configuration = configuration or {}
        self.machine = machine
        self.object_id = machine.object_id
        self.relative_path = machine.relative_path
        self.bus_id = machine.bus_id
        self.comp = _machine_comp(catalog, machine)
        self.comp_id = self.comp["object_id"]
        self.comp_path = self.comp["relative_path"]
        self.avr: CompiledDSL | None = None
        self.governor: CompiledDSL | None = None
        self.local_blocks: dict[str, CompiledDSL] = {}
        self.frame_blocks: tuple[CompiledDSL, ...] = ()
        self.frame_inputs: dict[str, dict[str, float]] = {}
        self.voltage_measurement = None
        descriptions: list[tuple[str, str, str]] = []
        pelm = catalog.attrs(self.comp).get("pelm", ())
        for index, element in enumerate(pelm):
            dsl_path = _path(element)
            if not dsl_path or element.get("class") != "ElmDsl":
                continue
            dsl = catalog.object_at(dsl_path)
            typ_path = _path(catalog.attrs(dsl).get("typ_id")) or ""
            typ_name = typ_path.rsplit("\\", 1)[-1]
            try:
                compiled = catalog.compile_dsl(dsl_path)
                if (dsl_path in self.configuration.get("avr_paths", ()) if self.configuration else "Efd" in compiled.ir.outputs):
                    _frame_wire(catalog, self.comp, index, 0, "Efd", "ve")
                    self.avr = compiled
                    descriptions.append((dsl_path, "replicado", 'AVR graph, Efd to ve'))
                elif (dsl_path in self.configuration.get("governor_paths", ()) if self.configuration else "pm" in compiled.ir.outputs):
                    _frame_wire(catalog, self.comp, index, 0, "pm", "pt")
                    self.governor = compiled
                    descriptions.append((dsl_path, "replicado", 'Governor graph, pm to pt'))
                else:
                    self.local_blocks[dsl_path] = compiled
                    descriptions.append((dsl_path, 'not_reproducible',
                                         'Compiled graph. Frame coupling is pending'))
            except DSLCompileError as exc:
                descriptions.append((dsl_path, 'not_reproducible', str(exc)))
        frame_paths = self.configuration.get("frame_paths", ())
        if frame_paths:
            active = {path: block for path, block in self.local_blocks.items()
                      if int(catalog.attrs(catalog.object_at(path)).get("outserv", 0)) == 0}
            if set(active) != set(frame_paths):
                raise _coded_error(DSLCompileError, 'invalid_controllers_data', 'Active blocks do not match the case frame_paths')
            self.frame_blocks = tuple(active[path] for path in frame_paths)
            if self.configuration.get("voltage_measurement_path"):
                row = catalog.object_at(self.configuration["voltage_measurement_path"])
                self.voltage_measurement = VoltageMeasurement(row['object_id'], row['relative_path'])
            descriptions = [(path, "replicado" if path in active else 'out_of_service',
                             'Connected PF frame' if path in active else "outserv=1")
                            for path, _, _ in descriptions]
        self.discrete_state: dict[str, object] = {
            "coverage": ControllerCoverage(self.comp_id, self.comp_path,
                "replicado" if ((self.avr is not None and self.governor is not None)
                or self.frame_blocks) and all(status in ("replicado", 'out_of_service')
                                              for _, status, _ in descriptions)
                else 'not_reproducible', tuple(descriptions)),
            "held_ve_pt": not self.frame_blocks and (self.avr is None or self.governor is None),
            "pt_outserv": bool(self.configuration.get("pt_outserv", False)),
        }
        self.n_states = (machine.n_states +
                         (self.avr.n_states if self.avr else 0) +
                         (self.governor.n_states if self.governor else 0) +
                         sum(block.n_states for block in self.frame_blocks) +
                         (2 if self.voltage_measurement else 0))
        self.vref_pu = 1.0
        self.dvref_pu = 0.0
        self.vs_pu = 0.0
        self.pref_pu = 0.0
        self.wref_pu = 1.0

    def _parts(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        values = np.asarray(x, dtype=float).reshape(-1)
        if values.size != self.n_states:
            raise _coded_error(ValueError, 'invalid_controllers_data', 'Incorrect ElmComp state vector length')
        m = self.machine.n_states
        a = self.avr.n_states if self.avr else 0
        g = self.governor.n_states if self.governor else 0
        return values[:m], values[m:m+a], values[m+a:m+a+g]

    def _frame_parts(self, x: np.ndarray) -> dict[str, np.ndarray]:
        cursor = (self.machine.n_states +
                  (self.avr.n_states if self.avr else 0) +
                  (self.governor.n_states if self.governor else 0))
        parts = {}
        for block in self.frame_blocks:
            parts[block.object_id] = np.asarray(x[cursor:cursor+block.n_states])
            cursor += block.n_states
        return parts

    def _frame_measurements(self, xm: np.ndarray, v_bus: complex) -> dict[str, float]:
        outputs = self.machine.outputs(xm, v_bus)
        ut_pu = abs(v_bus) * self.machine.bus_un_kv / self.machine.ugn_kv
        return {"ut": ut_pu, "u": ut_pu, "pg": float(outputs["P1"]),
                "Pg": float(outputs["P1"]), "qg": float(outputs["Q1"]),
                "sgnn": self.machine.sgn_mva, "cosn": float(self.machine.typ["cosn"]),
                "ie": float(outputs["ie"]), "curex": float(outputs["ie"]),
                "xspeed": float(outputs["xspeed"]), "fe": float(outputs["xspeed"])}

    def _frame_controls(self, t_s: float, x: np.ndarray, v_bus: complex,
                        *, accept: bool = False) -> np.ndarray:
        if not self.frame_blocks:
            return np.empty(0)
        xm, _, _ = self._parts(x)
        signals = self._frame_measurements(xm, v_bus)
        parts = self._frame_parts(x)
        derivatives = []
        for block in self.frame_blocks:
            inputs = {**self.frame_inputs[block.object_id], **{
                key: value for key, value in signals.items() if key in block.ir.inputs}}
            inputs = {key: inputs[key] for key in block.ir.inputs}
            if self.voltage_measurement and block.relative_path.endswith('IEEE PSS2B.ElmDsl'):
                frequency = self.voltage_measurement.outputs(x[-2:], v_bus)['fe_pu']
                inputs.update(W=frequency, fe=frequency)
            if accept:
                result, derivative = block.accept_step(t_s, parts[block.object_id], inputs)
            else:
                result, derivative = block.evaluate(t_s, parts[block.object_id], inputs)
            signals.update(result)
            derivatives.append(derivative)
            if "uerrs" in result:
                self.machine.ve = result["uerrs"]
            if "pt" in result:
                self.machine.pt = result["pt"]
        if self.voltage_measurement:
            derivatives.append(self.voltage_measurement.f(x[-2:], v_bus))
        return np.concatenate(derivatives) if derivatives else np.empty(0)

    def _init_frame(self, xm: np.ndarray, v_bus: complex) -> np.ndarray:
        """Resuelve `inc` del marco desde LF y los parámetros posteriores a ComInc."""
        measured = self._frame_measurements(xm, v_bus)
        measured.update({"pt": self.machine.pt, "uerrs": self.machine.ve,
                         "upss": 0.0, "voel": 0.0, "vuel": 0.0,
                         "vscl": 0.0, "vvhz": 0.0, "usetp": 0.0,
                         "Q1": measured["qg"],
                         "qelec": measured["qg"]/measured["sgnn"],
                         "fref": 1.0, "W": 1.0})
        states = []
        for block in self.frame_blocks:
            if block.relative_path in self.configuration.get("turbine_initialization_paths", ()):
                p = block.parameters
                pturb = (self.machine.pt*measured["sgnn"]*measured["cosn"]
                         /p["Trate"])
                area = pturb/(p["Fhp"]+p["Fip"]+p["Flp"])
                measured.update({"Pm": pturb, "PHP": 1.0, "CV": linear_lookup(
                    area, p["array_CV"], inverse=True), "Tlr": pturb})
            known = {key: measured[key] for key in measured if
                     key in (*block.ir.inputs, *block.ir.outputs, "Q1", "qelec", "Pm", "CV")}
            bx, initial_inputs = block.initialize(known)
            self.frame_inputs[block.object_id] = initial_inputs
            states.append(bx)
            outputs, derivative = block.evaluate(0.0, bx, initial_inputs)
            if max(abs(derivative), default=0.0) >= 1e-8:
                raise _coded_error(DSLCompileError, 'invalid_controllers_data', f'inc is not at equilibrium: {block.relative_path}')
            measured.update(outputs)
        if self.voltage_measurement:
            states.append(self.voltage_measurement.init(v_bus))
        return np.concatenate(states) if states else np.empty(0)

    def _controls(self, t_s: float, x: np.ndarray,
                  v_bus: complex) -> tuple[np.ndarray, np.ndarray]:
        xm, xa, xg = self._parts(x)
        machine_outputs = self.machine.outputs(xm, v_bus)
        avr_derivative = np.empty(0)
        gov_derivative = np.empty(0)
        if self.avr is not None:
            # ut de ElmSym: tensión terminal sobre TypSym.ugn, no sobre uknom.
            vt_pu = abs(v_bus) * self.machine.bus_un_kv / self.machine.ugn_kv
            result, avr_derivative = self.avr.evaluate(t_s, xa, {
                "dVref": self.dvref_pu, "Vref": self.vref_pu,
                "Vt": vt_pu, "Vs": self.vs_pu})
            self.machine.ve = result["Efd"]
        if self.governor is not None:
            result, gov_derivative = self.governor.evaluate(t_s, xg, {
                "Pref": self.pref_pu, "w": float(machine_outputs["xspeed"]),
                "wref": self.wref_pu})
            self.machine.pt = result["pm"]
        return avr_derivative, gov_derivative

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray:
        'Initialize the machine and propagate inc equations into the control references.'
        xm = self.machine.init(v_bus, i_or_s_from_lf)
        xa = np.empty(0)
        xg = np.empty(0)
        if self.avr is not None:
            vt_pu = abs(v_bus) * self.machine.bus_un_kv / self.machine.ugn_kv
            xa, known = self.avr.initialize({"Efd": self.machine.ve, "Vt": vt_pu})
            self.vref_pu = known["Vref"]
            self.dvref_pu = known["dVref"]
            self.vs_pu = known["Vs"]
        if self.governor is not None:
            xg, known = self.governor.initialize({"pm": self.machine.pt, "dw": 0.0})
            self.pref_pu = known["Pref"]
            self.wref_pu = known["wref"]
        xf = self._init_frame(xm, v_bus) if self.frame_blocks else np.empty(0)
        state = np.r_[xm, xa, xg, xf]
        residual = self.f(0.0, state, np.empty(0), v_bus)
        if np.max(np.abs(residual)) >= 1e-8:
            raise _coded_error(DSLCompileError, 'invalid_controllers_data', f'ElmComp is not at equilibrium: {self.comp_path}: {np.max(np.abs(residual)):.3g}')
        return state

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        avr_derivative, gov_derivative = self._controls(t, x, v)
        frame_derivative = self._frame_controls(t, x, v)
        xm, _, _ = self._parts(x)
        return np.r_[self.machine.f(t, xm, np.empty(0), v), avr_derivative,
                     gov_derivative, frame_derivative]

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        return np.empty(0)

    def current_injection(self, x: np.ndarray, v: complex) -> complex:
        xm, _, _ = self._parts(x)
        return self.machine.current_injection(xm, v)

    def outputs(self, x: np.ndarray, v: complex) -> dict[str, float | complex]:
        self._controls(0.0, x, v)
        self._frame_controls(0.0, x, v)
        xm, _, _ = self._parts(x)
        return {**self.machine.outputs(xm, v), "ve": self.machine.ve,
                "pt": self.machine.pt, "Vref": self.vref_pu, "Pref": self.pref_pu}

    def events(self) -> tuple[()]:
        return ()

    def accept_step(self, t_s: float, x: np.ndarray, v_bus: complex) -> None:
        'Commit connected DSL memories only after accepting an RMS step.'
        xm, xa, xg = self._parts(x)
        machine_outputs = self.machine.outputs(xm, v_bus)
        if self.avr is not None:
            vt_pu = abs(v_bus) * self.machine.bus_un_kv / self.machine.ugn_kv
            self.avr.accept_step(t_s, xa, {"dVref": self.dvref_pu,
                "Vref": self.vref_pu, "Vt": vt_pu, "Vs": self.vs_pu})
        if self.governor is not None:
            self.governor.accept_step(t_s, xg, {"Pref": self.pref_pu,
                "w": float(machine_outputs["xspeed"]), "wref": self.wref_pu})
        self._frame_controls(t_s, x, v_bus, accept=True)

    def next_event_time(self, after_s: float) -> float | None:
        times = [dsl.next_event_time(after_s) for dsl in
                 (self.avr, self.governor, *self.frame_blocks) if dsl is not None]
        return min((t for t in times if t is not None), default=None)
