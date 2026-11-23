'Loads support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import math
from dataclasses import dataclass, field
from typing import Mapping

import numpy as np


def _voltage_factor(params: Mapping[str, object], suffix: str, ratio: float) -> float:
    """Polinomio de TypLod, TechRef GeneralLoad (1)-(2), sobre V/V_LF."""
    weights = [float(params.get(k+suffix, 0.0)) for k in ("a", "b", "c")]
    if not math.isclose(sum(weights), 1.0, abs_tol=1e-7):
        raise _coded_error(ValueError, 'invalid_loads_data', f'TypLod coefficients {suffix} do not sum to 1')
    result = 0.0
    for coefficient, key in zip(weights, ("e_a", "e_b", "e_c")):
        if coefficient == 0:
            continue
        exponent = params.get(key+suffix)
        if exponent is None:
            # Alias efectivos PF 2024 comprobados en attributes.csv/description.
            alias = {"e_aP": "kpu0", "e_aQ": "kqu0",
                     "e_cP": "kpu", "e_cQ": "kqu"}.get(key+suffix)
            exponent = params.get(alias) if alias else None
        if exponent is None:
            raise _coded_error(ValueError, 'missing_loads_data', f'Missing {key + suffix} with nonzero weight')
        result += coefficient * ratio ** float(exponent)
    return result


@dataclass
class RMSLoad:
    'DynamicNetwork load represented by a corrective current. init receives total consumed load-flow MW+jMvar. Exported TypLod data determine the dynamic correction to the stamped load.'

    object_id: str
    relative_path: str
    bus_id: str
    type_params: Mapping[str, object] = field(default_factory=dict)
    s_base_mva: float = 100.0
    n_states: int = 0
    n_alg: int = 0
    discrete_state: dict[str, object] = field(default_factory=dict)
    frequency_pu: float = 1.0
    _v0: complex | None = field(default=None, init=False, repr=False)
    _s0_mva: complex | None = field(default=None, init=False, repr=False)
    _y_baseline: complex | None = field(default=None, init=False, repr=False)
    _p_factor: float = field(default=1.0, init=False, repr=False)
    _q_factor: float = field(default=1.0, init=False, repr=False)

    @classmethod
    def from_model(cls, load: Mapping[str, object], *, s_base_mva: float = 100.0) -> "RMSLoad":
        if load.get("class") != "ElmLod":
            raise _coded_error(ValueError, 'invalid_loads_data', 'ElmLod is required')
        return cls(str(load["id"]), str(load["path"]), str(load["bus"]),
                   dict(load.get("type_params") or {}), s_base_mva=s_base_mva)

    @property
    def dynamic_fraction(self) -> float:
        if not self.type_params:
            return 0.0
        share = self.type_params.get("loddy")
        if share is None:
            raise _coded_error(ValueError, 'missing_loads_data', f'Missing TypLod.loddy: {self.object_id}')
        fraction = float(share) / 100.0
        if not 0 <= fraction <= 1:
            raise _coded_error(ValueError, 'invalid_loads_data', 'TypLod.loddy is outside 0..100 %')
        return fraction

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray:
        'Initialize from V_LF in p.u. and consumed S_LF in MVA.'
        v, s = complex(v_bus), complex(i_or_s_from_lf)
        if self.s_base_mva <= 0 or abs(v) == 0 or not np.isfinite(
                [v.real, v.imag, s.real, s.imag]).all():
            raise _coded_error(ValueError, 'invalid_loads_data', 'Invalid load-flow load')
        self._v0, self._s0_mva = v, s
        self._y_baseline = (s / self.s_base_mva).conjugate() / abs(v)**2
        self.discrete_state["dynamic_fraction"] = self.dynamic_fraction
        if self.dynamic_fraction and any(float(self.type_params.get(key, 0.0)) != 0.0
                                         for key in ("tpf", "tqf")):
            raise _coded_error(NotImplementedError, 'unsupported_loads_data', 'TypLod dynamic frequency constants are not implemented')
        # Descubrir ausencias en el punto base, antes de un evento.
        if self.dynamic_fraction:
            self._dynamic_factors(1.0)
        return np.empty(0)

    def _dynamic_factors(self, voltage_ratio: float) -> tuple[float, float]:
        p_v = _voltage_factor(self.type_params, "P", voltage_ratio)
        q_v = _voltage_factor(self.type_params, "Q", voltage_ratio)
        # TechRef §5.2.1: dependencia lineal de frecuencia si kpf/kqf ≠ 0.
        p_f = 1.0 + float(self.type_params.get("kpf", 0.0)) * (self.frequency_pu-1.0)
        q_f = 1.0 + float(self.type_params.get("kqf", 0.0)) * (self.frequency_pu-1.0)
        return p_v*p_f, q_v*q_f

    def consumed_power_mva(self, v: complex) -> complex:
        if self._v0 is None or self._s0_mva is None:
            raise _coded_error(ValueError, 'invalid_loads_data', 'Load-flow initialization is required')
        ratio = abs(v) / abs(self._v0)
        static = 1.0-self.dynamic_fraction
        if self.dynamic_fraction and abs(v) == 0:
            raise _coded_error(ValueError, 'missing_loads_data', 'Dynamic load model requires a low-voltage limiter')
        dp, dq = self._dynamic_factors(ratio) if self.dynamic_fraction else (0.0, 0.0)
        return complex(self._s0_mva.real*self._p_factor*(static*ratio**2+self.dynamic_fraction*dp),
                       self._s0_mva.imag*self._q_factor*(static*ratio**2+self.dynamic_fraction*dq))

    def set_step_percent(self, *, dp_percent: float = 0.0, dq_percent: float = 0.0) -> None:
        'Apply an EvtLod percentage change relative to original load-flow power.'
        self._p_factor += float(dp_percent)/100.0
        self._q_factor += float(dq_percent)/100.0

    def set_frequency_pu(self, frequency_pu: float) -> None:
        if frequency_pu <= 0 or not math.isfinite(frequency_pu):
            raise _coded_error(ValueError, 'invalid_loads_data', 'Invalid bus frequency')
        self.frequency_pu = float(frequency_pu)

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        return np.empty(0)

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        return np.empty(0)

    def current_injection(self, x: np.ndarray, v: complex) -> complex:
        if self._y_baseline is None:
            raise _coded_error(ValueError, 'invalid_loads_data', 'Load-flow initialization is required')
        if abs(v) == 0:
            if self.dynamic_fraction:
                raise _coded_error(ValueError, 'invalid_loads_data', 'Dynamic load at zero voltage')
            return 0j
        s = self.consumed_power_mva(v) / self.s_base_mva
        full_injection = -(s / complex(v)).conjugate()
        baseline_injection = -self._y_baseline * complex(v)
        return full_injection-baseline_injection

    def events(self) -> tuple[()]:
        return ()
