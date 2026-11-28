'Converters support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import cmath
import json
import math
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np


def pv_lf_power_from_reference(pv: Mapping[str, object],
                               injection: Mapping[str, str]) -> tuple[complex, str]:
    'Read ElmPvsys P+jQ in MW+jMvar from an exact load-flow row. Require matching object_id, relative_path and units. Do not infer power from a setpoint or replace missing data.'
    if pv.get("class") != "ElmPvsys":
        raise _coded_error(ValueError, 'invalid_converters_data', 'ElmPvsys is required')
    if injection.get("object_id") != pv.get("id") or injection.get("relative_path") != pv.get("path"):
        raise _coded_error(ValueError, 'invalid_converters_data', 'Load-flow reference does not match ElmPvsys')
    if injection.get("m:P:bus1_unit") != "MW" or injection.get("m:Q:bus1_unit") != "Mvar":
        raise _coded_error(ValueError, 'invalid_converters_data', 'ElmPvsys load-flow units must be MW/Mvar')
    raw_p, raw_q = injection.get("m:P:bus1"), injection.get("m:Q:bus1")
    if raw_p in (None, "") or raw_q in (None, ""):
        raise _coded_error(ValueError, 'missing_converters_data', 'Missing ElmPvsys load-flow P/Q')
    value = complex(float(raw_p), float(raw_q))
    if not np.isfinite((value.real, value.imag)).all():
        raise _coded_error(ValueError, 'invalid_converters_data', 'ElmPvsys load-flow P/Q must be finite')
    return value, 'Supplied load-flow reference: m:P:bus1,m:Q:bus1'


def _finite_positive(value: object, name: str) -> float:
    if value is None:
        raise _coded_error(ValueError, 'missing_converters_data', f'Missing {name}')
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise _coded_error(ValueError, 'invalid_converters_data', f'{name} must be positive and finite')
    return result


@dataclass
class StaticGeneratorRMS:
    'ElmGenstat RMS adapter using the exported iSimModel. init receives total injected MVA. Mode 1 is a dq current source with the exported delay. Mode 3 is fixed admittance. Connected DSL current references must be explicit.'

    object_id: str
    relative_path: str
    bus_id: str
    mode: int
    s_rated_mva: float
    n_units: int
    s_base_mva: float = 100.0
    controlled: bool = False
    angle_delay: bool = False
    delay_s: float = 0.0
    min_voltage_pu: float = 0.0
    n_alg: int = 0
    discrete_state: dict[str, object] = field(default_factory=dict)
    _id_ref: float | None = field(default=None, init=False, repr=False)
    _iq_ref: float | None = field(default=None, init=False, repr=False)
    _reference_angle: float | None = field(default=None, init=False, repr=False)
    _admittance: complex | None = field(default=None, init=False, repr=False)
    _lf_voltage: complex | None = field(default=None, init=False, repr=False)
    reference_frequency_pu: float = field(default=1.0, init=False)

    def set_reference_frequency(self, value_pu: float) -> None:
        'Set reference speed in p.u. for the dq delay without an external PLL. Nominal-frame coordinates follow the StaticGenerator and PLL RMS reference-frame conventions.'
        self.reference_frequency_pu = float(value_pu)

    @classmethod
    def from_model(cls, gen: Mapping[str, object], *, s_base_mva: float = 100.0,
                   controlled: bool = False) -> "StaticGeneratorRMS":
        if gen.get("class") != "ElmGenstat":
            raise _coded_error(ValueError, 'invalid_converters_data', 'ElmGenstat is required')
        p = gen.get("params") or {}
        if p.get("iSimModel") is None:
            raise _coded_error(ValueError, 'missing_converters_data', 'Missing iSimModel')
        mode = int(p["iSimModel"])
        if mode not in (0, 1, 3):
            raise _coded_error(NotImplementedError, 'unsupported_converters_data', f'iSimModel={mode} is not implemented')
        return cls(str(gen["id"]), str(gen["path"]), str(gen["bus"]), mode,
                   _finite_positive(p.get("sgn"), "sgn [MVA]"),
                   int(_finite_positive(p.get("ngnum"), "ngnum")),
                   s_base_mva=s_base_mva, controlled=controlled,
                   angle_delay=bool(p.get("iDelay", 0)),
                   delay_s=float(p.get("Tdelay", 0.0)),
                   min_voltage_pu=float(p.get("umin", 0.0)))

    def __post_init__(self) -> None:
        if self.mode not in (0, 1, 3):
            raise _coded_error(ValueError, 'invalid_converters_data', 'Unknown ElmGenstat RMS mode')
        _finite_positive(self.s_rated_mva, "sgn [MVA]")
        _finite_positive(self.s_base_mva, "s_base_mva")
        if self.n_units < 1 or self.delay_s < 0 or self.min_voltage_pu < 0:
            raise _coded_error(ValueError, 'invalid_converters_data', 'Invalid count, delay or threshold')
        if self.angle_delay and self.mode != 3 and self.delay_s == 0:
            raise _coded_error(ValueError, 'missing_converters_data', 'iDelay requires positive Tdelay')
        self.discrete_state["effective_mode"] = self.mode
        self.discrete_state["control_required"] = self.controlled

    @property
    def n_states(self) -> int:
        return 2 if self.angle_delay and self.mode != 3 else 0

    @property
    def equipment_base_mva(self) -> float:
        return self.s_rated_mva * self.n_units

    def with_iec_type4a(self, **modules):
        """Compose this port with explicit IEC Type 4A module parameters.

        Returns a DynamicDevice for DAESolver. Does not inspect or replace
        an exported DLL or infer any REF frame connections.
        """
        from .iec61400 import IECType4A
        return IECType4A(self, **modules)

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray:
        'Initialize from a bus-voltage phasor and total load-flow power in MW+jMvar.'
        v = complex(v_bus)
        s_mva = complex(i_or_s_from_lf)
        if abs(v) == 0 or not np.isfinite([v.real, v.imag, s_mva.real, s_mva.imag]).all():
            raise _coded_error(ValueError, 'invalid_converters_data', 'Load flow must be finite and voltage must be nonzero')
        current = (s_mva / self.s_base_mva / v).conjugate()
        self._lf_voltage = v
        if self.mode == 3:
            self._admittance = current / v
            self.discrete_state["admittance_pu"] = self._admittance
            return np.empty(0)
        angle = cmath.phase(v)
        current_dq = current * cmath.exp(-1j * angle) * self.s_base_mva / self.equipment_base_mva
        self._id_ref, self._iq_ref = current_dq.real, current_dq.imag
        self._reference_angle = None
        if self.controlled:
            self.discrete_state["control_bound"] = False
        else:
            self.discrete_state["control_bound"] = True
        return np.array([math.cos(angle), math.sin(angle)]) if self.n_states else np.empty(0)

    def set_current_reference(self, id_ref_pu: float, iq_ref_pu: float,
                              *, angle_rad: float | None = None) -> None:
        'Set an actual DSL current command on the equipment base, with an optional PLL angle.'
        values = [id_ref_pu, iq_ref_pu]
        if angle_rad is not None:
            values.append(angle_rad)
        if not np.isfinite(values).all():
            raise _coded_error(ValueError, 'invalid_converters_data', 'Current reference must be finite')
        self._id_ref, self._iq_ref = float(id_ref_pu), float(iq_ref_pu)
        self._reference_angle = None if angle_rad is None else float(angle_rad)
        self.discrete_state["control_bound"] = True

    def _reference(self, x: np.ndarray, v: complex) -> complex:
        if self._reference_angle is not None:
            return cmath.exp(1j * self._reference_angle)
        if self.n_states:
            if len(x) != 2:
                raise _coded_error(ValueError, 'invalid_converters_data', 'Incorrect dq delay state dimension')
            return complex(float(x[0]), float(x[1]))
        if abs(v) == 0:
            raise _coded_error(ValueError, 'invalid_converters_data', 'Angular reference is undefined at zero voltage')
        return v / abs(v)

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        if not self.n_states:
            return np.empty(0)
        if abs(v) == 0:
            raise _coded_error(ValueError, 'missing_converters_data', 'dq delay requires a nonzero phasor')
        target = v / abs(v)
        result = (np.array([target.real, target.imag]) - np.asarray(x, dtype=float)) / self.delay_s
        omega_ref = 2*math.pi*50*(self.reference_frequency_pu-1.)
        return result + omega_ref*np.array([-x[1], x[0]])

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        return np.empty(0)

    def current_injection(self, x: np.ndarray, v: complex) -> complex:
        if self._lf_voltage is None:
            raise _coded_error(ValueError, 'invalid_converters_data', 'Load-flow initialization is required')
        if self.mode == 3:
            return complex(self._admittance) * complex(v)
        if self.controlled and not self.discrete_state.get("control_bound"):
            raise _coded_error(RuntimeError, 'invalid_converters_data', f'DSL control is not connected: {self.object_id}')
        if abs(v) < self.min_voltage_pu:
            return 0j
        ref = self._reference(np.asarray(x), complex(v))
        return complex(self._id_ref, self._iq_ref) * ref * self.equipment_base_mva / self.s_base_mva

    def events(self) -> tuple[()]:
        return ()


@dataclass
class WECCPVAdapter:
    'ElmPvsys port for an exact REEC_B/REGC_A pair. Validate the pair and pelm port on construction. The connected DSL controller owns the control dynamics.'

    object_id: str
    relative_path: str
    bus_id: str
    reec_object_id: str
    regc_object_id: str
    s_base_mva: float = 100.0
    controller: object | None = None
    n_states: int = 0
    n_alg: int = 0
    discrete_state: dict[str, object] = field(default_factory=dict)

    @classmethod
    def from_binding(cls, pv: Mapping[str, object], binding: Mapping[str, str],
                     *, s_base_mva: float = 100.0) -> "WECCPVAdapter":
        if pv.get("class") != "ElmPvsys":
            raise _coded_error(ValueError, 'missing_converters_data', 'The WECC pair requires an ElmPvsys instance')
        if not binding.get("reec_object_id") or not binding.get("regc_object_id"):
            raise _coded_error(ValueError, 'invalid_converters_data', 'Incomplete REEC_B/REGC_A pair')
        return cls(str(pv["id"]), str(pv["path"]), str(pv["bus"]),
                   binding["reec_object_id"], binding["regc_object_id"],
                   s_base_mva=s_base_mva)

    def bind_controller(self, controller: object) -> None:
        'Connect the supplied WECC composite DSL runtime.'
        for name in ("init", "f", "g", "current_injection", "events"):
            if not callable(getattr(controller, name, None)):
                raise _coded_error(TypeError, 'missing_converters_data', f'DSL executor is missing {name}')
        self.n_states = int(controller.n_states)
        self.n_alg = int(controller.n_alg)
        if self.n_states < 0 or self.n_alg < 0:
            raise _coded_error(ValueError, 'invalid_converters_data', 'Invalid DSL dimension')
        self.controller = controller
        self.discrete_state["dsl_bound"] = True

    def bind_exported_controller(self, pv: Mapping[str, object],
                                 binding: Mapping[str, str], catalog,
                                 injection: Mapping[str, str]) -> complex:
        'Build the case converter composite and return its traceable load-flow P+jQ.'
        if pv.get("id") != self.object_id or pv.get("path") != self.relative_path:
            raise _coded_error(ValueError, 'invalid_converters_data', 'ElmPvsys is incompatible with the WECC adapter')
        power, source = pv_lf_power_from_reference(pv, injection)
        controller = WECCDSLController(pv, binding, catalog,
                                       s_base_mva=self.s_base_mva)
        self.bind_controller(controller)
        self.discrete_state["lf_power_mva"] = power
        self.discrete_state["lf_provenance"] = source
        return power

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray:
        if self.controller is None:
            raise _coded_error(RuntimeError, 'invalid_converters_data', f'REEC_B/REGC_A has no compiled DSL: {self.object_id}')
        return self.controller.init(v_bus, i_or_s_from_lf)

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        return self.controller.f(t, x, z, v)

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        return self.controller.g(t, x, z, v)

    def current_injection(self, x: np.ndarray, v: complex) -> complex:
        return self.controller.current_injection(x, v)

    def events(self):
        return () if self.controller is None else self.controller.events()

    def accept_step(self, t: float, x: np.ndarray, v: complex) -> None:
        if self.controller is not None:
            self.controller.accept_step(t, x, v)

    def next_event_time(self, after_s: float) -> float | None:
        return None if self.controller is None else self.controller.next_event_time(after_s)


class WECCDSLController:
    'REEC_B, REGC_A and protection from the ElmPvsys composite. x concatenates their states and StaVmea states. Power inputs use the generator base.'

    n_alg = 0

    def __init__(self, pv: Mapping[str, object], binding: Mapping[str, str], catalog,
                 *, s_base_mva: float = 100.0):
        from .dsl_compile import DSLCompileError
        self.object_id = str(pv["id"])
        self.relative_path = str(pv["path"])
        self.bus_id = str(pv["bus"])
        if pv.get("class") != "ElmPvsys" or pv.get("units", {}).get("sgn") != "kVA":
            raise _coded_error(ValueError, 'missing_converters_data', 'WECC requires ElmPvsys with sgn in kVA')
        self.equipment_base_mva = _finite_positive(pv.get("params", {}).get("sgn"),
                                                   "sgn [kVA]") / 1000 * int(pv.get("params", {}).get("ngnum", 1))
        self.s_base_mva = _finite_positive(s_base_mva, "s_base_mva")
        comp = catalog.by_id[binding["composite_id"]]
        if not any(isinstance(e, dict) and e.get("relative_path") == self.relative_path
                   for e in catalog.attrs(comp).get("pelm", ())):
            raise _coded_error(DSLCompileError, 'invalid_converters_data', f'ElmPvsys is not linked by pelm: {self.relative_path}')
        self.reec = catalog.compile_dsl(catalog.by_id[binding["reec_object_id"]]["relative_path"])
        self.regc = catalog.compile_dsl(catalog.by_id[binding["regc_object_id"]]["relative_path"])
        protection = [e for e in catalog.attrs(comp).get("pelm", ()) if isinstance(e, dict)
                      and e.get("class") == "ElmDsl" and
                      e.get("relative_path", "").endswith("Protection.ElmDsl")]
        if len(protection) != 1:
            raise _coded_error(DSLCompileError, 'invalid_converters_data', f'Protección WECC ambigua: {self.relative_path}')
        self.protection = catalog.compile_dsl(protection[0]["relative_path"])
        # El marco conecta fe;Fnom de StaVmea a f;Fnom de la protección.
        # Conservar los estados del filtro del TechRef, también tras eventos;
        # una frecuencia nominal fija ocultaba toda esa entrada dinámica.
        from .measurements import VoltageMeasurement
        from .controllers import _frame_wire
        members = catalog.attrs(comp).get('pelm', ())
        measurements = [(i, e) for i, e in enumerate(members) if isinstance(e, dict)
                        and e.get('class') == 'StaVmea']
        if len(measurements) != 1:
            raise _coded_error(DSLCompileError, 'invalid_converters_data', f'Ambiguous WECC measurement: {self.relative_path}')
        mi, measurement = measurements[0]
        pi = next(i for i,e in enumerate(members) if e == protection[0])
        _frame_wire(catalog, comp, mi, pi, 'fe;Fnom', 'f;Fnom')
        mrow = catalog.object_at(measurement['relative_path'])
        ma = catalog.attrs(mrow)
        if (ma.get('pbusbar') or {}).get('relative_path') != catalog.by_id[self.bus_id]['relative_path']:
            raise _coded_error(DSLCompileError, 'missing_converters_data', f"WECC StaVmea requires a remote port: {mrow['relative_path']}")
        self.voltage_measurement = VoltageMeasurement(mrow['object_id'], mrow['relative_path'])
        self.n_states = self.reec.n_states + self.regc.n_states + self.protection.n_states + 2
        self.discrete_state: dict[str, object] = {"dsl_bound": True,
            "composite_id": binding["composite_id"], "lf_provenance": None}
        self._reec_inputs: dict[str, float] = {}
        self._regc_inputs: dict[str, float] = {}
        self._last_t = 0.0

    def _parts(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        a = np.asarray(x, dtype=float).reshape(-1)
        if a.size != self.n_states:
            raise _coded_error(ValueError, 'invalid_converters_data', 'Incorrect WECC state vector length')
        nr, ng = self.reec.n_states, self.regc.n_states
        return a[:nr], a[nr:nr+ng], a[nr+ng:-2]

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray:
        vmag = abs(v_bus)
        if vmag <= 0:
            raise _coded_error(ValueError, 'missing_converters_data', 'WECC requires nonzero load-flow voltage')
        s = complex(i_or_s_from_lf) / self.equipment_base_mva
        id0, iq0 = s.real/vmag, -s.imag/vmag
        xg, self._regc_inputs = self.regc.initialize(
            {"Vt": vmag, "idref": id0, "iqref": iq0})
        xr, self._reec_inputs = self.reec.initialize(
            {"Vt": vmag, "Pe": s.real, "Qgen": s.imag,
             "Ipcmd": self._regc_inputs["Ipcmd"],
             "Iqcmd": self._regc_inputs["Iqcmd"]})
        xp, _ = self.protection.initialize({"f": 1.0, "Fnom": 50.0, "u": vmag})
        x = np.r_[xr, xg, xp, self.voltage_measurement.init(v_bus)]
        derivative = self.f(0.0, x, np.empty(0), v_bus)
        if np.max(np.abs(derivative)) >= 1e-8:
            raise _coded_error(ValueError, 'invalid_converters_data', f'WECC is not at equilibrium: {self.relative_path}: {np.max(np.abs(derivative)):.3g}')
        return x

    def _signals(self, t: float, x: np.ndarray, v: complex):
        xr, xg, xp = self._parts(x)
        vmag = abs(v)
        if vmag == 0:
            raise _coded_error(ValueError, 'missing_converters_data', 'WECC requires nonzero voltage')
        # Estados REGC: xq e xp en base del equipo; Qgen positivo inyectado.
        reec_inputs = {**self._reec_inputs, "Vt": vmag,
                       "Pe": vmag * float(xg[self.regc.state_names.index("xp")]),
                       "Qgen": vmag * float(xg[self.regc.state_names.index("xq")])}
        reec_y, dr = self.reec.evaluate(t, xr, reec_inputs)
        regc_inputs = {**self._regc_inputs, "Vt": vmag,
                       "Ipcmd": reec_y["Ipcmd"], "Iqcmd": reec_y["Iqcmd"]}
        regc_y, dg = self.regc.evaluate(t, xg, regc_inputs)
        frequency = self.voltage_measurement.outputs(x[-2:], v)['fe_pu']
        prot_inputs = {"f": frequency, "Fnom": self.voltage_measurement.f_nom_hz, "u": vmag}
        prot_y, dp = self.protection.evaluate(t, xp, prot_inputs)
        current = (complex(regc_y["idref"], regc_y["iqref"]) * v / vmag *
                   self.equipment_base_mva / self.s_base_mva)
        if prot_y["CB_open"] >= 0.5:
            current = 0j
        return np.r_[dr, dg, dp, self.voltage_measurement.f(x[-2:], v)], current, (reec_inputs, regc_inputs, prot_inputs)

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        self._last_t = float(t)
        return self._signals(t, x, v)[0]

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        return np.empty(0)

    def current_injection(self, x: np.ndarray, v: complex) -> complex:
        return self._signals(self._last_t, x, v)[1]

    def accept_step(self, t: float, x: np.ndarray, v: complex) -> None:
        'Commit flip-flops and buffers only after accepting the DAE step.'
        xr, xg, xp = self._parts(x)
        _, _, (ri, gi, pi) = self._signals(t, x, v)
        self.reec.accept_step(t, xr, ri)
        self.regc.accept_step(t, xg, gi)
        self.protection.accept_step(t, xp, pi)

    def next_event_time(self, after_s: float) -> float | None:
        times = [dsl.next_event_time(after_s) for dsl in
                 (self.reec, self.regc, self.protection)]
        return min((t for t in times if t is not None), default=None)

    def events(self) -> tuple[()]:
        return ()


def converter_bindings(dynamic_export: Mapping[str, object]) -> dict[str, object]:
    'Read ElmComp.pelm without filling null ports or changing controller families. Return connected ElmGenstat IDs and exact WECC pairs.'
    objects = dynamic_export["objects"]
    by_path = {row["relative_path"]: row for row in objects}
    by_id = {row["object_id"]: row for row in objects}
    values = {}
    for row in dynamic_export["attributes"]:
        if row["status"] == "PRESENT" and row["attribute"] in ("pelm", "typ_id"):
            values[(row["object_id"], row["attribute"])] = json.loads(row["value_json"])
    controlled: dict[str, str] = {}
    wecc: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for parent in objects:
        if parent["class"] != "ElmComp":
            continue
        elements = values.get((parent["object_id"], "pelm")) or []
        resolved = []
        for ref in elements:
            path = ref.get("relative_path") if isinstance(ref, dict) else None
            resolved.append(by_path.get(path) if path else None)
        for item in resolved:
            if item and item["class"] == "ElmGenstat":
                controlled[item["object_id"]] = parent["object_id"]
        dsl = [item for item in resolved if item and item["class"] == "ElmDsl"]
        families: dict[str, str] = {}
        for item in dsl:
            typ = values.get((item["object_id"], "typ_id"))
            name = (typ or {}).get("relative_path", "").split("\\")[-1]
            if name in ("REEC_B.BlkDef", "REGC_A.BlkDef"):
                families[name[:-7]] = item["object_id"]
        if families:
            if set(families) != {"REEC_B", "REGC_A"}:
                raise _coded_error(ValueError, 'invalid_converters_data', f"Incomplete WECC pair: {parent['object_id']}")
            pv = [item for item in resolved if item and item["class"] == "ElmPvsys"]
            if len(pv) == 1:
                wecc[pv[0]["object_id"]] = {
                    "composite_id": parent["object_id"],
                    "reec_object_id": families["REEC_B"],
                    "regc_object_id": families["REGC_A"],
                }
            elif not pv:
                missing.append(parent["object_id"])
            else:
                raise _coded_error(ValueError, 'invalid_converters_data', f"More than one ElmPvsys in {parent['object_id']}")
    return {"controlled_genstat": controlled, "wecc_pvsys": wecc,
            "wecc_without_generator": missing}
