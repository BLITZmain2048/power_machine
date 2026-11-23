"""Contratos de entrada para curvas de estabilidad de tensión."""
from __future__ import annotations

from dataclasses import dataclass
import math


def _finite_positive(value, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} debe ser numérico")
    value = float(value)
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        raise ValueError(f"{name} debe ser finito y positivo")
    return value


@dataclass(frozen=True)
class PVOptions:
    """Opciones PV, potencias en MW y factor de potencia adimensional."""
    initial_step_mw: float = 10.0
    minimum_step_mw: float = 0.25
    maximum_increase_mw: float = 500.0
    power_factor: float | None = None
    generator_dispatch: str = "reference"
    initialization_mode: str = "flat"
    allow_nonphysical_loads: bool = False
    q_limits: bool = True
    max_points: int = 100

    def __post_init__(self):
        initial = _finite_positive(self.initial_step_mw, "initial_step_mw")
        minimum = _finite_positive(self.minimum_step_mw, "minimum_step_mw")
        maximum = _finite_positive(self.maximum_increase_mw, "maximum_increase_mw", allow_zero=True)
        if minimum > initial:
            raise ValueError("minimum_step_mw no puede superar initial_step_mw")
        if maximum < minimum:
            raise ValueError("maximum_increase_mw debe permitir al menos un paso mínimo")
        if self.power_factor is not None:
            pf = float(self.power_factor)
            if not math.isfinite(pf) or not 0 < abs(pf) <= 1:
                raise ValueError("power_factor debe estar en (-1, 0) o (0, 1]")
        if self.generator_dispatch not in ("reference", "proportional"):
            raise ValueError("generator_dispatch debe ser reference o proportional")
        if self.initialization_mode not in ("flat", "warm_from_last"):
            raise ValueError("initialization_mode debe ser flat o warm_from_last")
        if type(self.allow_nonphysical_loads) is not bool:
            raise ValueError("allow_nonphysical_loads debe ser booleano")
        if type(self.q_limits) is not bool:
            raise ValueError("q_limits debe ser booleano")
        if type(self.max_points) is not int or not 2 <= self.max_points <= 10000:
            raise ValueError("max_points debe ser un entero entre 2 y 10000")


@dataclass(frozen=True)
class QVOptions:
    """Opciones QV, consigna de tensión en p.u. y Q en Mvar."""
    voltage_start_pu: float = 1.10
    voltage_end_pu: float = 0.80
    voltage_step_pu: float = 0.01
    source_q_min_mvar: float | None = None
    source_q_max_mvar: float | None = None
    reactive_limit_mode: str = "idealized"
    max_points: int = 100

    def __post_init__(self):
        start = _finite_positive(self.voltage_start_pu, "voltage_start_pu")
        end = _finite_positive(self.voltage_end_pu, "voltage_end_pu")
        step = _finite_positive(self.voltage_step_pu, "voltage_step_pu")
        if start == end:
            raise ValueError("El barrido QV requiere consignas de tensión distintas")
        count = int(math.ceil(abs(start - end) / step)) + 1
        if type(self.max_points) is not int or not 2 <= self.max_points <= 10000:
            raise ValueError("max_points debe ser un entero entre 2 y 10000")
        if count > self.max_points:
            raise ValueError("El barrido QV supera max_points")
        limits = (self.source_q_min_mvar, self.source_q_max_mvar)
        if (limits[0] is None) != (limits[1] is None):
            raise ValueError("Los límites de la fuente QV requieren mínimo y máximo")
        if limits[0] is not None:
            low = float(limits[0])
            high = float(limits[1])
            if not math.isfinite(low) or not math.isfinite(high) or low >= high:
                raise ValueError("Los límites QV deben ser finitos y mínimo menor que máximo")
        if self.reactive_limit_mode not in ("idealized", "active_generator_limits"):
            raise ValueError("reactive_limit_mode debe ser idealized o active_generator_limits")
