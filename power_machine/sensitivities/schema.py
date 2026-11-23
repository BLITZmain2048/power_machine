"""Contratos para sensibilidades estáticas alrededor de un flujo de potencia."""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class SensitivityOptions:
    """Configuración con incrementos explícitos en MW y Mvar.

    Los incrementos se aplican centrados, ``+h`` y ``-h``, sobre una fuente PQ
    temporal. No cambian el modelo ni el escenario de quien llama.
    """

    injection_bus_id: str
    outage_branch_id: str | None = None
    step_p_mw: float = 1.0
    step_q_mvar: float = 1.0
    q_limits: bool = True
    max_iter: int = 30
    tolerance_pu: float = 1e-8

    def __post_init__(self):
        if not isinstance(self.injection_bus_id, str) or not self.injection_bus_id:
            raise ValueError("injection_bus_id debe ser un ID de barra no vacío")
        if self.outage_branch_id is not None and (not isinstance(self.outage_branch_id, str)
                                                  or not self.outage_branch_id):
            raise ValueError("outage_branch_id debe ser un ID de rama o null")
        for name in ("step_p_mw", "step_q_mvar", "tolerance_pu"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} debe ser positivo y finito")
        if type(self.max_iter) is not int or self.max_iter < 1:
            raise ValueError("max_iter debe ser un entero positivo")
        if type(self.q_limits) is not bool:
            raise ValueError("q_limits debe ser booleano")
