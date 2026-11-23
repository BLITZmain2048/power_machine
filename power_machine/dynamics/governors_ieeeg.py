'Governors ieeeg support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from types import MappingProxyType

import numpy as np


IEEEG1_NAMES = (
    "K", "T1", "T2", "T3", "K1", "K2", "T5", "K3", "K4", "T6",
    "K5", "K6", "T4", "T7", "K7", "K8", "PNhp", "PNlp",
    "Uc", "Pmin", "Uo", "Pmax",
)
IEEEG3_NAMES = (
    "Tg", "Tp", "Sigma", "Delta", "Tr", "a11", "a13", "a21", "a23",
    "Tw", "PN", "Uc", "Pmin", "Uo", "Pmax",
)


def _number(value: float, label: str) -> float:
    """Rechaza ausencias y texto, no convierte celdas vacías en cero."""
    if value is None or isinstance(value, (str, bool, np.bool_)):
        raise _coded_error(ValueError, 'missing_governors_ieeeg_data', f'{label} is missing or nonnumeric')
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', f'{label} must be numeric') from exc
    if not math.isfinite(number):
        raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', f'{label} must be finite')
    return number


@dataclass(frozen=True)
class _Ports:
    inputs: tuple[str, ...] = ("Pref", "w", "wref")
    outputs: tuple[str, ...] = ("pm",)


class _Governor:
    """Contrato local compatible con CompiledDSL y GENERAL_P10."""

    ir = _Ports()
    parameter_names: tuple[str, ...]
    state_names: tuple[str, ...]
    gate_index: int
    n_states: int

    def __init__(self, object_id: str, relative_path: str,
                 parameters: Mapping[str, float] | Sequence[float], *,
                 pgn_mw: float):
        self.object_id, self.relative_path = object_id, relative_path
        self.pgn_mw = _number(pgn_mw, 'Pgn [MW per unit]')
        if self.pgn_mw <= 0:
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'Pgn must be positive')
        names = self.parameter_names
        if isinstance(parameters, Mapping):
            missing = set(names) - parameters.keys()
            if missing:
                raise _coded_error(ValueError, 'missing_governors_ieeeg_data', f'Missing parameters: {sorted(missing)}')
            unknown = parameters.keys() - set(names)
            if unknown:
                raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', f'Unknown parameters: {sorted(unknown)}')
            values = [parameters[name] for name in names]
        else:
            values = list(parameters)
            if len(values) != len(names):
                raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', f'Require {len(names)} values in PF order, including limits')
        p = {name: _number(value, name) for name, value in zip(names, values)}
        if not p["Uc"] < 0 < p["Uo"] or p["Pmin"] >= p["Pmax"]:
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'Invalid limits. Require Uc<0<Uo and Pmin<Pmax')
        self.parameters = MappingProxyType(p)

    def _state(self, x: np.ndarray) -> np.ndarray:
        state = np.asarray(x, dtype=float).reshape(-1)
        if state.size != self.n_states or not np.isfinite(state).all():
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', f'Require {self.n_states} finite states')
        return state

    def _inputs(self, inputs: Mapping[str, float]) -> dict[str, float]:
        missing = set(self.ir.inputs) - inputs.keys()
        if missing:
            raise _coded_error(ValueError, 'missing_governors_ieeeg_data', f'Missing inputs: {sorted(missing)}')
        return {name: _number(inputs[name], name) for name in self.ir.inputs}

    def _initial_inputs(self, known: Mapping[str, float]) -> dict[str, float]:
        if "pm" not in known:
            raise _coded_error(ValueError, 'missing_governors_ieeeg_data', 'Initialization requires load-flow pm on the Pgn base')
        wref_pu = _number(known.get("wref", 1.0), "wref")
        dw_pu = _number(known.get("dw", 0.0), "dw")
        w_pu = _number(known.get("w", wref_pu+dw_pu), "w")
        if "dw" in known and not math.isclose(w_pu-wref_pu, dw_pu, abs_tol=1e-12):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'w, wref and dw are inconsistent')
        return {"w": w_pu, "wref": wref_pu}

    def _finish_initialization(self, state: np.ndarray, inputs: dict[str, float],
                               known: Mapping[str, float]):
        p = self.parameters
        gate_pu = state[self.gate_index]
        if not p["Pmin"] <= gate_pu <= p["Pmax"]:
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'Equilibrium is infeasible. Valve is outside limits')
        reference = _number(known.get("Pref", inputs["Pref"]), "Pref")
        if not math.isclose(reference, inputs["Pref"], abs_tol=1e-12, rel_tol=1e-12):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'Initialization has no numerical equilibrium. Check Pref')
        inputs["Pref"] = reference
        output, derivative = self.evaluate(0.0, state, inputs)
        if np.max(np.abs(derivative)) >= 1e-8 or not math.isclose(
                output["pm"], _number(known["pm"], "pm"), abs_tol=1e-12, rel_tol=1e-12):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'Initialization has no numerical equilibrium. Check Pref')
        return state, {name: inputs[name] for name in self.ir.inputs}

    def _gate_rate(self, gate_pu: float, raw_per_s: float) -> tuple[float, float]:
        """Derivada limitada y pendiente respecto de la rampa sin limitar."""
        p = self.parameters
        rate = min(max(raw_per_s, p["Uc"]), p["Uo"])
        if ((gate_pu >= p["Pmax"] and rate > 0)
                or (gate_pu <= p["Pmin"] and rate < 0)):
            return 0.0, 0.0
        slope = float(p["Uc"] < raw_per_s < p["Uo"])
        return rate, slope

    def _gate(self, value_pu: float) -> tuple[float, float]:
        p = self.parameters
        return (min(max(value_pu, p["Pmin"]), p["Pmax"]),
                float(p["Pmin"] < value_pu < p["Pmax"]))

    def project_state(self, x: np.ndarray) -> np.ndarray:
        'Copy the accepted state and project the gate or valve and its rate onto the exported limits. Preserve water-memory states.'
        state = self._state(x).copy()
        state[self.gate_index] = self._gate(state[self.gate_index])[0]
        return state

    def accept_step(self, t_s: float, x: np.ndarray, inputs: Mapping[str, float]):
        'Return outputs and derivatives without modifying discrete memory.'
        return self.evaluate(t_s, x, inputs)

    def next_event_time(self, after_s: float) -> None:
        'Return no internal timed event.'
        return None


class IEEEG1Governor(_Governor):
    'Six-state multistage steam governor with lead-lag and a limited servomotor. Speed uses p.u. Valve and turbine stages use the declared rated-power base. Parameters must be supplied explicitly.'

    parameter_names = IEEEG1_NAMES
    n_states = 6
    gate_index = 1
    state_names = ("speed_lag_pu", "valve_pu", "steam_T4_pu", "steam_T5_pu",
                   "steam_T6_pu", "steam_T7_pu")

    def __init__(self, object_id: str, relative_path: str,
                 parameters: Mapping[str, float] | Sequence[float], *,
                 pgn_mw: float, turbine: str = "HP"):
        super().__init__(object_id, relative_path, parameters, pgn_mw=pgn_mw)
        p = self.parameters
        if any(p[name] <= 0 for name in ("T1", "T3", "T4", "T5", "T6", "T7")):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'G1 dynamic time constants must be positive. Artificial bypass is not supported')
        if any(p[name] < 0 for name in ("K", "T2", "PNhp", "PNlp", *[f"K{i}" for i in range(1, 9)])):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'Gains, T2 and turbine bases must be nonnegative')
        if turbine not in ("HP", "LP", "COMBINED"):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'turbine must be HP, LP or COMBINED')
        self.turbine = turbine
        self.effective_bases_mw = MappingProxyType({
            name: p[name] if p[name] > 0 else self.pgn_mw for name in ("PNhp", "PNlp")})
        self.base_assumptions = tuple('=0 interpreted as Pgn' for name in
                                      ("PNhp", "PNlp") if p[name] == 0)
        hp = [p[f"K{i}"]*self.effective_bases_mw["PNhp"]/self.pgn_mw for i in (1, 3, 5, 7)]
        lp = [p[f"K{i}"]*self.effective_bases_mw["PNlp"]/self.pgn_mw for i in (2, 4, 6, 8)]
        self.weights = tuple(hp if turbine == "HP" else lp if turbine == "LP"
                             else [a+b for a, b in zip(hp, lp)])
        if sum(self.weights) <= 0:
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'The selected shaft has no positive mechanical gain')

    def initialize(self, known: Mapping[str, float]) -> tuple[np.ndarray, dict[str, float]]:
        'Back-propagate load-flow pm to equilibrium states on Pgn. Do not clip an infeasible operating point.'
        u = self._initial_inputs(known)
        dw_pu = u["w"]-u["wref"]
        valve_pu = _number(known["pm"], "pm")/sum(self.weights)
        state = np.array([dw_pu, valve_pu, valve_pu, valve_pu, valve_pu, valve_pu])
        u["Pref"] = valve_pu+self.parameters["K"]*dw_pu
        return self._finish_initialization(state, u, known)

    def evaluate(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[dict[str, float], np.ndarray]:
        'Return pm on Pgn and state derivatives in p.u./s. Hydro gate acceleration uses p.u./s squared. Do not modify the caller state.'
        state, u, p = self._state(x), self._inputs(inputs), self.parameters
        dw_pu = u["w"]-u["wref"]
        ratio = p["T2"]/p["T1"]
        lead_pu = p["K"]*(ratio*dw_pu+(1-ratio)*state[0])
        valve_pu = self._gate(state[1])[0]
        derivative = np.empty(6)
        derivative[0] = (dw_pu-state[0])/p["T1"]
        derivative[1] = self._gate_rate(state[1], (u["Pref"]-lead_pu-state[1])/p["T3"])[0]
        previous_pu = valve_pu
        for i, name in enumerate(("T4", "T5", "T6", "T7"), 2):
            derivative[i] = (previous_pu-state[i])/p[name]
            previous_pu = state[i]
        return {"pm": float(np.dot(self.weights, state[2:]))}, derivative

    def jacobian(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
        'Return the analytical Jacobian of the selected active branch, with state columns followed by Pref, w and wref. A switching boundary has no unique classical derivative.'
        state, u, p = self._state(x), self._inputs(inputs), self.parameters
        ratio = p["T2"]/p["T1"]
        lead_pu = p["K"]*(ratio*(u["w"]-u["wref"])+(1-ratio)*state[0])
        slope = self._gate_rate(state[1], (u["Pref"]-lead_pu-state[1])/p["T3"])[1]
        df = np.zeros((6, 9))
        df[0, [0, 7, 8]] = [-1/p["T1"], 1/p["T1"], -1/p["T1"]]
        df[1, [0, 1, 6, 7, 8]] = slope/p["T3"]*np.array([
            -p["K"]*(1-ratio), -1, 1, -p["K"]*ratio, p["K"]*ratio])
        for i, name in enumerate(("T4", "T5", "T6", "T7"), 2):
            df[i, i] = -1/p[name]
            df[i, i-1] = (self._gate(state[1])[1] if i == 2 else 1.0)/p[name]
        dy = np.zeros((1, 9))
        dy[0, 2:6] = self.weights
        return df, dy


class IEEEG3Governor(_Governor):
    'Four-state linear hydro governor with permanent and transient droop. States are gate g [p.u.], gate lag z [p.u.], gate rate v [p.u./s] and reset memory r [p.u.].'

    parameter_names = IEEEG3_NAMES
    n_states = 4
    gate_index = 0
    state_names = ("gate_pu", "water_lag_pu", "gate_speed_pu_per_s", "reset_lag_pu")

    def __init__(self, object_id: str, relative_path: str,
                 parameters: Mapping[str, float] | Sequence[float], *,
                 pgn_mw: float):
        super().__init__(object_id, relative_path, parameters, pgn_mw=pgn_mw)
        p = self.parameters
        if any(p[name] <= 0 for name in ("Tg", "Tp", "Tr", "Tw", "a11", "a23")):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'G3 time constants, a11 and a23 must be positive')
        if any(p[name] < 0 for name in ("Sigma", "Delta", "PN")):
            raise _coded_error(ValueError, 'invalid_governors_ieeeg_data', 'Sigma, Delta and PN must be nonnegative')
        self.effective_bases_mw = MappingProxyType({"PN": p["PN"] if p["PN"] > 0 else self.pgn_mw})
        self.base_assumptions = (('PN=0 interpreted as Pgn',) if p["PN"] == 0 else ())
        self.power_base_ratio = self.effective_bases_mw["PN"]/self.pgn_mw
        self.water_gain = p["a21"]*p["a13"]/p["a11"]
        self.direct_gain = p["a23"]-self.water_gain

    def _pilot(self, state: np.ndarray, inputs: Mapping[str, float]) -> tuple[float, float]:
        """Aceleración pu/s² y pendiente no-windup, sobre estado de velocidad."""
        p = self.parameters
        gate_pu = self._gate(state[0])[0]
        error_pu = (inputs["Pref"]-(inputs["w"]-inputs["wref"])
                    -p["Sigma"]*gate_pu-p["Delta"]*(gate_pu-state[3]))
        acceleration = (error_pu/p["Tg"]-state[2])/p["Tp"]
        blocked = ((state[2] >= p["Uo"] and acceleration > 0)
                   or (state[2] <= p["Uc"] and acceleration < 0))
        return (0.0, 0.0) if blocked else (acceleration, 1.0)

    def initialize(self, known: Mapping[str, float]) -> tuple[np.ndarray, dict[str, float]]:
        'Back-propagate load-flow pm to equilibrium states on Pgn. Do not clip an infeasible operating point.'
        u = self._initial_inputs(known)
        gate_pu = _number(known["pm"], "pm")/(self.power_base_ratio*self.parameters["a23"])
        state = np.array([gate_pu, gate_pu, 0.0, gate_pu])
        u["Pref"] = u["w"]-u["wref"]+self.parameters["Sigma"]*gate_pu
        return self._finish_initialization(state, u, known)

    def evaluate(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[dict[str, float], np.ndarray]:
        'Return pm on Pgn and state derivatives in p.u./s. Hydro gate acceleration uses p.u./s squared. Do not modify the caller state.'
        state, u, p = self._state(x), self._inputs(inputs), self.parameters
        gate_pu = self._gate(state[0])[0]
        derivative = np.array([
            self._gate_rate(state[0], state[2])[0],
            (gate_pu-state[1])/(p["a11"]*p["Tw"]),
            self._pilot(state, u)[0],
            (gate_pu-state[3])/p["Tr"],
        ])
        pm_pu = self.power_base_ratio*(self.direct_gain*gate_pu+self.water_gain*state[1])
        return {"pm": float(pm_pu)}, derivative

    def jacobian(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
        'Return the analytical Jacobian of the selected active branch, with state columns followed by Pref, w and wref. A switching boundary has no unique classical derivative.'
        state, u, p = self._state(x), self._inputs(inputs), self.parameters
        gate_slope = self._gate(state[0])[1]
        pilot_slope = self._pilot(state, u)[1]
        df = np.zeros((4, 7))
        df[0, 2] = self._gate_rate(state[0], state[2])[1]
        water_time_s = p["a11"]*p["Tw"]
        df[1, [0, 1]] = [gate_slope/water_time_s, -1/water_time_s]
        df[2, [0, 2, 3, 4, 5, 6]] = pilot_slope*np.array([
            -(p["Sigma"]+p["Delta"])*gate_slope/(p["Tg"]*p["Tp"]),
            -1/p["Tp"], p["Delta"]/(p["Tg"]*p["Tp"]),
            1/(p["Tg"]*p["Tp"]), -1/(p["Tg"]*p["Tp"]), 1/(p["Tg"]*p["Tp"])])
        df[3, [0, 3]] = [gate_slope/p["Tr"], -1/p["Tr"]]
        dy = np.zeros((1, 7))
        dy[0, [0, 1]] = self.power_base_ratio*np.array([self.direct_gain*gate_slope, self.water_gain])
        return df, dy

    def project_state(self, x: np.ndarray) -> np.ndarray:
        'Copy the accepted state and project the gate or valve and its rate onto the exported limits. Preserve water-memory states.'
        state = super().project_state(x)
        state[2] = min(max(state[2], self.parameters["Uc"]), self.parameters["Uo"])
        return state
