"""Contrato de entrada de indicadores RMS, con unidades explícitas."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any


@dataclass(frozen=True)
class DynMetricOptions:
    """Opciones de posproceso, tiempos en s y ROCOF en Hz/s."""

    event_time_s: float | None = None
    rocof_window_ms: int = 200
    voltage_band_min_pu: float = 0.9
    voltage_band_max_pu: float = 1.1
    voltage_recovery_pu: float = 0.95
    settling_band_pct: float = 2.0
    profile: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.event_time_s is not None and (isinstance(self.event_time_s, bool)
                                              or not _finite(self.event_time_s)):
            raise ValueError("event_time_s debe ser finito o nulo")
        if self.rocof_window_ms not in (100, 200, 500):
            raise ValueError("rocof_window_ms admite 100, 200 o 500 ms")
        values = (self.voltage_band_min_pu, self.voltage_band_max_pu,
                  self.voltage_recovery_pu, self.settling_band_pct)
        if any(not _finite(value) or value <= 0 for value in values):
            raise ValueError("Los umbrales deben ser positivos y finitos")
        if self.voltage_band_min_pu >= self.voltage_band_max_pu:
            raise ValueError("La banda mínima de tensión debe ser menor que la máxima")
        if not isinstance(self.profile, dict):
            raise ValueError("profile debe ser un objeto JSON")


def _finite(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
