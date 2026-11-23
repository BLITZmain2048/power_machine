'Load general support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import math
from collections.abc import Mapping
from types import MappingProxyType

import numpy as np

from .loads import RMSLoad


def _number(params: Mapping[str, object], key: str) -> float:
    """Lee un parámetro obligatorio finito, sin convertir ausencias en cero."""
    value = params.get(key)
    if value is None or isinstance(value, (str, bool)):
        raise _coded_error(ValueError, 'missing_load_general_data', f'Missing or nonnumeric General Load parameter: {key}')
    result = float(value)
    if not math.isfinite(result):
        raise _coded_error(ValueError, 'invalid_load_general_data', f'General Load parameter must be finite: {key}')
    return result


class GeneralLoad(RMSLoad):
    'One-state RMSLoad-compatible model with an analytical local Jacobian and no local algebraic variables. This bounded profile excludes frequency response and nonzero lead times. Unsupported data remain explicit.'

    state_names = ("xu_pu",)

    def __init__(self, object_id: str, relative_path: str, bus_id: str,
                 type_params: Mapping[str, object], s_base_mva: float = 100.0):
        params = dict(type_params)
        super().__init__(object_id, relative_path, bus_id,
                         MappingProxyType(params), s_base_mva=s_base_mva,
                         n_states=1, n_alg=0)
        if not math.isfinite(s_base_mva) or s_base_mva <= 0:
            raise _coded_error(ValueError, 'invalid_load_general_data', 's_base_mva must be positive and finite')
        if _number(params, "i_nln") != 1:
            raise _coded_error(NotImplementedError, 'unsupported_load_general_data', 'GeneralLoad requires TypLod.i_nln=1')
        share_percent = _number(params, "loddy")
        if not 0 < share_percent <= 100:
            raise _coded_error(ValueError, 'missing_load_general_data', 'GeneralLoad requires 0<loddy<=100 %')
        self.t1_s = _number(params, "t1")
        if self.t1_s <= 0:
            raise _coded_error(NotImplementedError, 'unsupported_load_general_data', 'Single-state GeneralLoad requires t1>0 s')
        for key in ("tpu", "tqu", "tpf", "tqf", "kpf", "kqf"):
            if _number(params, key) != 0:
                raise _coded_error(NotImplementedError, 'unsupported_load_general_data', f'Single-state GeneralLoad requires {key}=0')
        self.udmin_pu = _number(params, "udmin")
        self.udmax_pu = _number(params, "udmax")
        if not 0 < self.udmin_pu < self.udmax_pu:
            raise _coded_error(ValueError, 'invalid_load_general_data', 'Require 0<udmin<udmax in bus p.u.')
        self._polynomials = tuple(self._polynomial(params, suffix) for suffix in ("P", "Q"))

    @staticmethod
    def _polynomial(params: Mapping[str, object], suffix: str) -> tuple:
        a, b = (_number(params, key+suffix) for key in ("a", "b"))
        c = 1.0-a-b
        if min(a, b, c) < 0:
            raise _coded_error(ValueError, 'invalid_load_general_data', f'Weights {suffix} must be nonnegative and sum to one')
        if "c"+suffix in params and not math.isclose(
                _number(params, "c"+suffix), c, rel_tol=0, abs_tol=1e-7):
            raise _coded_error(ValueError, 'invalid_load_general_data', f'c{suffix} does not match 1-a{suffix}-b{suffix}')
        aliases = ("kpu0", "kpu1", "kpu") if suffix == "P" else ("kqu0", "kqu1", "kqu")
        terms = []
        for weight, canonical, alias in zip((a, b, c),
                                            ("e_a"+suffix, "e_b"+suffix, "e_c"+suffix), aliases):
            if weight == 0:
                continue
            exponent = _number(params, canonical if canonical in params else alias)
            if canonical in params and alias in params and _number(params, alias) != exponent:
                raise _coded_error(ValueError, 'invalid_load_general_data', f'Incompatible exponents: {canonical}, {alias}')
            if exponent < 0:
                raise _coded_error(NotImplementedError, 'unsupported_load_general_data', 'Negative exponents are not supported at zero voltage')
            terms.append((weight, exponent))
        return tuple(terms)

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray:
        'Accept V_LF in p.u. and consumed MW+jMvar. Return xu=U0.'
        v, s = complex(v_bus), complex(i_or_s_from_lf)
        if not np.isfinite([v.real, v.imag, s.real, s.imag]).all():
            raise _coded_error(ValueError, 'invalid_load_general_data', 'Load-flow load must be finite')
        u0_pu = abs(v)
        if not self.udmin_pu <= u0_pu <= self.udmax_pu:
            raise _coded_error(ValueError, 'invalid_load_general_data', 'Load-flow U0 must lie within udmin..udmax')
        self._v0, self._s0_mva = v, s
        self._y_baseline = (s/self.s_base_mva).conjugate()/u0_pu**2
        self._p_factor = self._q_factor = 1.0
        self.frequency_pu = 1.0
        self.discrete_state["dynamic_fraction"] = self.dynamic_fraction
        return np.array([u0_pu])

    def _point(self, x: np.ndarray, v: complex) -> tuple[float, float, complex]:
        if self._v0 is None:
            raise _coded_error(ValueError, 'invalid_load_general_data', 'Load-flow initialization is required')
        state = np.asarray(x, dtype=float).reshape(-1)
        if state.size != 1 or not math.isfinite(float(state[0])) or state[0] < 0:
            raise _coded_error(ValueError, 'missing_load_general_data', 'GeneralLoad requires finite nonnegative xu')
        voltage = complex(v)
        if not (math.isfinite(voltage.real) and math.isfinite(voltage.imag)):
            raise _coded_error(ValueError, 'invalid_load_general_data', 'Bus voltage must be finite')
        return float(state[0]), abs(voltage), voltage

    def voltage_multiplier(self, u_pu: float) -> tuple[float, float]:
        'Return (k, dk/dU) for the continuous General Load polynomial, with U in p.u.'
        if not math.isfinite(u_pu) or u_pu < 0:
            raise _coded_error(ValueError, 'invalid_load_general_data', 'U must be finite and nonnegative')
        lower, upper = self.udmin_pu, self.udmax_pu
        if u_pu < lower/2:
            return 2*(u_pu/lower)**2, 4*u_pu/lower**2
        if u_pu <= lower:
            return 1-2*((u_pu-lower)/lower)**2, -4*(u_pu-lower)/lower**2
        if u_pu < upper:
            return 1.0, 0.0
        return 1+(u_pu-upper)**2, 2*(u_pu-upper)

    def _factors(self, xu_pu: float, *, derivative: bool = False) -> np.ndarray:
        ratio = xu_pu/abs(self._v0)
        values = []
        for terms in self._polynomials:
            if derivative:
                if ratio == 0 and any(0 < exponent < 1 for _, exponent in terms):
                    raise _coded_error(ValueError, 'invalid_load_general_data', 'Jacobian is singular at xu=0 for an exponent between 0 and 1')
                value = sum(weight*exponent*ratio**(exponent-1)/abs(self._v0)
                            for weight, exponent in terms if exponent != 0)
            else:
                value = sum(weight*ratio**exponent for weight, exponent in terms)
            values.append(value)
        result = np.array(values)
        if not np.isfinite(result).all():
            raise _coded_error(ValueError, 'invalid_load_general_data', 'General Load polynomial must be finite')
        return result

    def _power_parts(self, xu_pu: float, u_pu: float,
                     *, derivative: bool = False) -> tuple:
        """Potencias MW,Mvar y, si se pide, derivadas por xu y por U p.u."""
        d = self.dynamic_fraction
        u0_pu = abs(self._v0)
        k, dk = self.voltage_multiplier(u_pu)
        factors = self._factors(xu_pu)
        base = np.array([self._s0_mva.real*self._p_factor,
                         self._s0_mva.imag*self._q_factor])
        powers = base*((1-d)*(u_pu/u0_pu)**2+d*k*factors)
        if not derivative:
            return powers, None, None
        by_xu = base*d*k*self._factors(xu_pu, derivative=True)
        by_u = base*((1-d)*2*u_pu/u0_pu**2+d*dk*factors)
        return powers, by_xu, by_u

    def consumed_power_mva(self, v: complex, x: np.ndarray | None = None) -> complex:
        'Return consumed MW+jMvar. Without x, evaluate only steady state. The solver supplies x directly without storing Newton iterates.'
        xu_pu, u_pu, _ = self._point(np.array([abs(v)]) if x is None else x, v)
        powers, _, _ = self._power_parts(xu_pu, u_pu)
        return complex(*powers)

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        'Return dxu/dt in p.u./s without clipping measured voltage to a band.'
        xu_pu, u_pu, _ = self._point(x, v)
        return np.array([(u_pu-xu_pu)/self.t1_s])

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        'Return no local algebraic residual, consistently with RMSLoad.'
        self._point(x, v)
        return np.empty(0)

    def _low_voltage_coefficient(self, xu_pu: float) -> complex:
        """Icorr/V [p.u.] en la rama U<udmin/2, sin dividir por U."""
        base = complex(self._s0_mva.real*self._p_factor,
                       self._s0_mva.imag*self._q_factor)
        fp, fq = self._factors(xu_pu)
        d = self.dynamic_fraction
        admittance = complex(base.real*((1-d)/abs(self._v0)**2+d*2*fp/self.udmin_pu**2),
                             base.imag*((1-d)/abs(self._v0)**2+d*2*fq/self.udmin_pu**2))
        return self._y_baseline-(admittance/self.s_base_mva).conjugate()

    def current_injection(self, x: np.ndarray, v: complex) -> complex:
        'Return corrective current in p.u.: -conj(S/Sbase/V)+Y_LF*V. At low voltage evaluate the equivalent-admittance form continuously, including V=0.'
        xu_pu, u_pu, voltage = self._point(x, v)
        if u_pu < self.udmin_pu/2:
            return self._low_voltage_coefficient(xu_pu)*voltage
        powers, _, _ = self._power_parts(xu_pu, u_pu)
        power = complex(*powers)/self.s_base_mva
        return -(power/voltage).conjugate()+self._y_baseline*voltage

    def jacobian_local(self, t: float, x: np.ndarray, z: np.ndarray,
                       v: complex, *, rel_step: float = 1e-6) -> np.ndarray:
        'Analytical rows [f, Re(Icorr), Im(Icorr)], columns [xu, Re(V), Im(V)]. rel_step is accepted for protocol compatibility. Handle the low-voltage branch explicitly at V=0.'
        xu_pu, u_pu, voltage = self._point(x, v)
        if np.asarray(z).size:
            raise _coded_error(ValueError, 'invalid_load_general_data', 'GeneralLoad has no local algebraic variables')
        jac = np.zeros((3, 3))
        jac[0, 0] = -1/self.t1_s
        if u_pu > 0:
            radial = np.array([voltage.real, voltage.imag])/u_pu
            jac[0, 1:] = radial/self.t1_s
        if u_pu < self.udmin_pu/2:
            by_real = self._low_voltage_coefficient(xu_pu)
            by_imag = 1j*by_real
            if u_pu > 0:
                slopes = self._factors(xu_pu, derivative=True)
                by_xu = np.array([self._s0_mva.real*self._p_factor,
                                  self._s0_mva.imag*self._q_factor])
                by_xu *= self.dynamic_fraction*2/self.udmin_pu**2*slopes
                by_state = -(complex(*by_xu)/self.s_base_mva).conjugate()*voltage
                jac[1:, 0] = [by_state.real, by_state.imag]
        else:
            powers, by_xu, by_u = self._power_parts(xu_pu, u_pu, derivative=True)
            conjugate_s = complex(*powers).conjugate()/self.s_base_mva
            conjugate_x = complex(*by_xu).conjugate()/self.s_base_mva
            conjugate_u = complex(*by_u).conjugate()/self.s_base_mva
            vc = voltage.conjugate()
            by_state = -conjugate_x/vc
            jac[1:, 0] = [by_state.real, by_state.imag]
            by_real = -conjugate_u*radial[0]/vc+conjugate_s/vc**2+self._y_baseline
            by_imag = -conjugate_u*radial[1]/vc-1j*conjugate_s/vc**2+1j*self._y_baseline
        jac[1:, 1] = [by_real.real, by_real.imag]
        jac[1:, 2] = [by_imag.real, by_imag.imag]
        return jac
