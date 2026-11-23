'Avr ieeet1 support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import math
from types import MappingProxyType

import numpy as np


PARAMETER_NAMES = (
    "Tr", "Ka", "Ta", "Ke", "Te", "Kf", "Tf", "E1", "Se1", "E2", "Se2",
    "Vrmin", "Vrmax",
)

AVR_PROFILES = ("dyn_a", "pf_identificado")
PF_IDENTIFIED_STATUS = ('Structure identified from PF linearization. BlkDef was not exported or validated')


@dataclass(frozen=True)
class _Ports:
    """Nombres de puertos para el contrato de control, sin fingir un grafo DSL."""

    inputs: tuple[str, ...] = ("dVref", "Vref", "Vt", "Vs")
    outputs: tuple[str, ...] = ("Efd",)


class IEEET1AVR:
    'Local IEEE Type 1 exciter. Inputs use machine nominal-voltage p.u. Efd, Vr and Xf use the exported excitation base. Parameters and saturation curves are explicit.'

    n_states = 4
    state_names = ("Vmeas_pu", "Vr_pu", "Efd_pu", "Efd_lag_pu")
    ir = _Ports()

    def __init__(self, object_id: str, relative_path: str,
                 parameters: Mapping[str, float] | Sequence[float], *,
                 tr_min_s: float = 1e-4, profile: str = "dyn_a"):
        if profile not in AVR_PROFILES:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Perfil IEEET1 desconocido: ' + str(profile))
        self.profile = profile
        self.saturation_curve = "exponential" if profile == "pf_identificado" else "quadratic"
        self.freeze_vm_when_tr_zero = profile == "pf_identificado"
        self.object_id = object_id
        self.relative_path = relative_path
        if isinstance(parameters, Mapping):
            missing = set(PARAMETER_NAMES) - parameters.keys()
            if missing:
                raise _coded_error(ValueError, 'missing_avr_ieeet1_data', f'Missing IEEET1 parameters: {sorted(missing)}')
            values = [parameters[name] for name in PARAMETER_NAMES]
        else:
            values = list(parameters)
            if len(values) != len(PARAMETER_NAMES):
                raise _coded_error(ValueError, 'missing_avr_ieeet1_data', 'IEEET1 requires 13 values, including Vrmin and Vrmax')
        if any(value is None or isinstance(value, (str, bool)) for value in values):
            raise _coded_error(ValueError, 'missing_avr_ieeet1_data', 'Missing or nonnumeric IEEET1 parameter')
        p = dict(zip(PARAMETER_NAMES, map(float, values)))
        if not all(math.isfinite(value) for value in p.values()):
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'IEEET1 parameters must be finite')
        if p["Tr"] < 0 or any(p[key] <= 0 for key in ("Ka", "Ta", "Te", "Tf")):
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Tr must be >= 0. Ka, Ta, Te and Tf must be positive')
        if p["Kf"] < 0 or p["Vrmin"] >= p["Vrmax"]:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Kf must be nonnegative and Vr limits must be ordered')
        if not math.isfinite(tr_min_s) or tr_min_s <= 0:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'tr_min_s must be positive and finite')
        self.parameters = MappingProxyType(p)
        self.tr_min_s = float(tr_min_s)
        self.tr_effective_s = p["Tr"] if p["Tr"] > 0 else self.tr_min_s
        self.saturation_a_pu, self.saturation_b, self.saturation_roundoff = self._fit_saturation()

    def _fit_saturation(self) -> tuple[float, float, bool]:
        p = self.parameters
        e1, e2, s1, s2 = (p[key] for key in ("E1", "E2", "Se1", "Se2"))
        if self.saturation_curve == "exponential":
            self.exp_enabled = not (s1 == s2 == 0)
            self.exp_b_per_pu = 0.0
            if self.exp_enabled:
                if not (0 < e1 < e2 and 0 < s1 < s2):
                    raise _coded_error(ValueError, 'missing_avr_ieeet1_data', 'Exponential saturation requires 0<E1<E2 and 0<Se1<Se2')
                self.exp_b_per_pu = math.log(s2/s1)/(e2-e1)
            return 0.0, 0.0, False
        if s1 < 0 or s2 < 0:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Se1 and Se2 must be nonnegative')
        if s1 == s2 == 0:
            return 0.0, 0.0, False
        if not 0 < e1 < e2 or not s1 < s2:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Invalid saturation points. Require 0<E1<E2 and Se1<Se2')
        q1, q2 = math.sqrt(e1*s1), math.sqrt(e2*s2)
        slope = (q2-q1)/(e2-e1)
        a_pu = e1-q1/slope
        if abs(a_pu) <= 1e-6*e2:
            return 0.0, (s1/e1+s2/e2)/2, True
        if a_pu < 0:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Points are incompatible with monotone quadratic saturation, A<0')
        return a_pu, slope*slope, False

    def saturation(self, efd_pu: float) -> float:
        'Return dimensionless SE. Return zero below the curve threshold or for nonpositive Efd.'
        if not math.isfinite(efd_pu):
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Efd must be finite')
        if self.saturation_curve == "exponential":
            if not self.exp_enabled or efd_pu <= 0:
                return 0.0
            return self.parameters["Se1"]*math.exp(
                self.exp_b_per_pu*(efd_pu-self.parameters["E1"]))
        if efd_pu <= self.saturation_a_pu or self.saturation_b == 0:
            return 0.0
        return self.saturation_b*(efd_pu-self.saturation_a_pu)**2/efd_pu

    def _saturation_drop(self, efd_pu: float) -> tuple[float, float]:
        """Caída Efd SE en p.u. y su derivada respecto de Efd en p.u."""
        if self.saturation_curve == "exponential":
            se = self.saturation(efd_pu)
            return efd_pu*se, se*(1+self.exp_b_per_pu*efd_pu)
        over_pu = max(0.0, efd_pu-self.saturation_a_pu)
        return self.saturation_b*over_pu**2, 2*self.saturation_b*over_pu

    def _state(self, x: np.ndarray) -> np.ndarray:
        state = np.asarray(x, dtype=float).reshape(-1)
        if state.size != self.n_states or not np.isfinite(state).all():
            raise _coded_error(ValueError, 'missing_avr_ieeet1_data', 'IEEET1 requires four finite states')
        return state

    def _inputs(self, inputs: Mapping[str, float]) -> dict[str, float]:
        missing = set(self.ir.inputs) - inputs.keys()
        if missing:
            raise _coded_error(ValueError, 'missing_avr_ieeet1_data', f'Missing IEEET1 inputs: {sorted(missing)}')
        values = {name: float(inputs[name]) for name in self.ir.inputs}
        if not all(math.isfinite(value) for value in values.values()) or values["Vt"] < 0:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Invalid IEEET1 inputs. Vt must be a nonnegative magnitude')
        return values

    def _amplifier(self, state: np.ndarray, inputs: Mapping[str, float]) -> tuple[float, bool]:
        p = self.parameters
        vm_pu, vr_pu, efd_pu, xf_pu = state
        vf_pu = p["Kf"]/p["Tf"]*(efd_pu-xf_pu)
        measured_pu = vm_pu if p["Tr"] > 0 else inputs["Vt"]
        error_pu = inputs["Vref"]+inputs["dVref"]+inputs["Vs"]-measured_pu-vf_pu
        raw_per_s = (p["Ka"]*error_pu-vr_pu)/p["Ta"]
        blocked = ((vr_pu >= p["Vrmax"] and raw_per_s > 0)
                   or (vr_pu <= p["Vrmin"] and raw_per_s < 0))
        return (0.0 if blocked else raw_per_s), blocked

    def evaluate(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[dict[str, float], np.ndarray]:
        'Return Efd in p.u. and state derivatives in p.u./s without modifying x.'
        state = self._state(x)
        u = self._inputs(inputs)
        p = self.parameters
        vm_pu, vr_pu, efd_pu, xf_pu = state
        dvr_per_s, _ = self._amplifier(state, u)
        vr_limited_pu = min(max(vr_pu, p["Vrmin"]), p["Vrmax"])
        drop_pu, _ = self._saturation_drop(efd_pu)
        derivative = np.array([
            (u["Vt"]-vm_pu)/self.tr_effective_s,
            dvr_per_s,
            (vr_limited_pu-p["Ke"]*efd_pu-drop_pu)/p["Te"],
            (efd_pu-xf_pu)/p["Tf"],
        ])
        if self.freeze_vm_when_tr_zero and p["Tr"] == 0:
            derivative[0] = 0.0
        return {"Efd": float(efd_pu)}, derivative

    def initialize(self, known: Mapping[str, float]) -> tuple[np.ndarray, dict[str, float]]:
        'Compute Vref from explicit load-flow Efd and Vt. Reject an operating point outside limits. Optional dVref and Vs default to zero.'
        if "Efd" not in known or "Vt" not in known:
            raise _coded_error(ValueError, 'missing_avr_ieeet1_data', 'IEEET1 initialization requires load-flow Efd and Vt')
        efd_pu, vt_pu = float(known["Efd"]), float(known["Vt"])
        if not math.isfinite(efd_pu) or not math.isfinite(vt_pu) or vt_pu < 0:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'Invalid initial Efd or Vt')
        p = self.parameters
        vr_pu = p["Ke"]*efd_pu+self._saturation_drop(efd_pu)[0]
        if not p["Vrmin"] <= vr_pu <= p["Vrmax"]:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', f'IEEET1 equilibrium is infeasible, Vr={vr_pu:.12g} is outside limits')
        u = {"Vt": vt_pu, "dVref": float(known.get("dVref", 0.0)),
             "Vs": float(known.get("Vs", 0.0))}
        u["Vref"] = float(known.get("Vref", vt_pu+vr_pu/p["Ka"]-u["dVref"]-u["Vs"]))
        state = np.array([vt_pu, vr_pu, efd_pu, efd_pu])
        _, derivative = self.evaluate(0.0, state, u)
        if np.max(np.abs(derivative)) >= 1e-8:
            raise _coded_error(ValueError, 'invalid_avr_ieeet1_data', 'IEEET1 initialization has no numerical equilibrium. Check Vref')
        return state, u

    def jacobian(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
        'Return analytical (df, dy), with columns [Vm, Vr, Efd, Xf, dVref, Vref, Vt, Vs]. At limits use the active non-windup branch, not an average across branches.'
        state = self._state(x)
        u = self._inputs(inputs)
        p = self.parameters
        _, blocked = self._amplifier(state, u)
        df = np.zeros((4, 8))
        df[0, 0] = -1/self.tr_effective_s
        df[0, 6] = 1/self.tr_effective_s
        if self.freeze_vm_when_tr_zero and p["Tr"] == 0:
            df[0] = 0.0
        if not blocked:
            df[1, :4] = [(-p["Ka"]/p["Ta"] if p["Tr"] > 0 else 0.0), -1/p["Ta"],
                          -p["Ka"]*p["Kf"]/(p["Ta"]*p["Tf"]),
                          p["Ka"]*p["Kf"]/(p["Ta"]*p["Tf"])]
            df[1, [4, 5, 7]] = p["Ka"]/p["Ta"]
            if p["Tr"] == 0:
                df[1, 6] = -p["Ka"]/p["Ta"]
        df[2, 1] = (1/p["Te"] if p["Vrmin"] < state[1] < p["Vrmax"] else 0.0)
        df[2, 2] = -(p["Ke"]+self._saturation_drop(state[2])[1])/p["Te"]
        df[3, 2:4] = [1/p["Tf"], -1/p["Tf"]]
        dy = np.zeros((1, 8))
        dy[0, 2] = 1.0
        return df, dy

    def project_state(self, x: np.ndarray) -> np.ndarray:
        'Copy x and project Vr onto its limits after an accepted integration step.'
        state = self._state(x).copy()
        state[1] = min(max(state[1], self.parameters["Vrmin"]), self.parameters["Vrmax"])
        return state

    def accept_step(self, t_s: float, x: np.ndarray,
                    inputs: Mapping[str, float]) -> tuple[dict[str, float], np.ndarray]:
        'Accept an already bounded state. This controller has no discrete memory.'
        return self.evaluate(t_s, x, inputs)

    def next_event_time(self, after_s: float) -> None:
        'Return no internal timed event.'
        return None
