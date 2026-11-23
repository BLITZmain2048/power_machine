'Interfaces support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np


@runtime_checkable
class DynamicDevice(Protocol):
    'Machine, converter, dynamic-load or controller protocol. init receives load-flow bus voltage and either current or power as declared by the adapter. Current injection uses network-base p.u. Local algebraic variables are explicit.'

    object_id: str
    relative_path: str
    bus_id: str
    n_states: int
    n_alg: int
    discrete_state: Mapping[str, object]

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray: ...

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray: ...

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray: ...

    def current_injection(self, x: np.ndarray, v: complex) -> complex: ...

    def events(self) -> Sequence[object]: ...


@runtime_checkable
class LocalJacobianDevice(Protocol):
    'Optional local Jacobian protocol. Rows are [f (p.u./s), g, Re(I), Im(I)]. Columns are [x, z, Re(V), Im(V)]. It adds no mandatory DynamicDevice requirement.'

    def jacobian_local(self, t: float, x: np.ndarray, z: np.ndarray,
                       v: complex, *, rel_step: float = 1e-6) -> np.ndarray: ...


@dataclass(frozen=True)
class PortMeasurements:
    'Network-base p.u. measurements for a multiport device. Branch currents are positive out of the selected terminal. Power equals V times conjugate(I).'

    bus_voltage_pu: Mapping[str, complex]
    bus_current_pu: Mapping[str, complex]
    bus_power_pu: Mapping[str, complex]
    branch_current_pu: Mapping[tuple[str, int], complex]
    branch_power_pu: Mapping[tuple[str, int], complex]


@runtime_checkable
class MultiportDynamicDevice(Protocol):
    'Optional remote measurement and injection protocol. read_bus_ids identifies voltages, read_branch_ids identifies measured branches and injection_bus_ids identifies network injections.'

    read_bus_ids: Sequence[str]
    read_branch_ids: Sequence[str]
    injection_bus_ids: Sequence[str]

    def f_multi(self, t: float, x: np.ndarray, z: np.ndarray,
                ports: PortMeasurements) -> np.ndarray: ...

    def g_multi(self, t: float, x: np.ndarray, z: np.ndarray,
                ports: PortMeasurements) -> np.ndarray: ...

    def current_injections_multi(self, x: np.ndarray, z: np.ndarray,
                                 ports: PortMeasurements) -> Mapping[str, complex]: ...


def complex_to_rect(values: np.ndarray | complex) -> np.ndarray:
    'Stack real and imaginary components, in that order, without changing the base.'
    a = np.atleast_1d(np.asarray(values, dtype=complex))
    return np.r_[a.real, a.imag]


def rect_to_complex(values: np.ndarray) -> np.ndarray:
    'Invert complex_to_rect and require an even vector length.'
    a = np.asarray(values, dtype=float).reshape(-1)
    if a.size % 2:
        raise _coded_error(ValueError, 'invalid_interfaces_data', 'Rectangular vector length must be even')
    n = a.size // 2
    return a[:n] + 1j * a[n:]


def finite_difference_jacobian(function, point: np.ndarray, *,
                               rel_step: float = 1e-6,
                               abs_step: float = 1e-8) -> np.ndarray:
    'Central differences of a real vector function. Per-column steps are max(abs_step, rel_step*max(1, abs(point[j]))). The caller controls the operating regime.'
    p = np.asarray(point, dtype=float).reshape(-1)
    if not np.isfinite(p).all() or rel_step <= 0 or abs_step <= 0:
        raise _coded_error(ValueError, 'invalid_interfaces_data', 'Finite-difference point and steps must be valid')
    base = np.asarray(function(p), dtype=float).reshape(-1)
    jac = np.empty((base.size, p.size), dtype=float)
    for j in range(p.size):
        h = max(abs_step, rel_step * max(1.0, abs(p[j])))
        plus, minus = p.copy(), p.copy()
        plus[j] += h
        minus[j] -= h
        fp = np.asarray(function(plus), dtype=float).reshape(-1)
        fm = np.asarray(function(minus), dtype=float).reshape(-1)
        if fp.size != base.size or fm.size != base.size:
            raise _coded_error(ValueError, 'invalid_interfaces_data', 'Function dimension changed under perturbation')
        jac[:, j] = (fp - fm) / (2 * h)
    if not np.isfinite(jac).all():
        raise _coded_error(ValueError, 'invalid_interfaces_data', 'Jacobian must be finite')
    return jac
