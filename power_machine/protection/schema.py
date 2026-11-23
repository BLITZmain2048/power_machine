"""Contratos de sobrecorriente, magnitudes primarias en A y tiempos en s."""
from __future__ import annotations

from dataclasses import dataclass, asdict
import math


@dataclass(frozen=True)
class CurveSpec:
    family: str
    name: str
    coefficients: dict
    source: dict


@dataclass(frozen=True)
class RelayStage:
    object_id: str
    relative_path: str | None
    relay_id: str
    protected_element_id: str | None
    terminal_id: str | None
    pickup_primary_a: float | None
    time_dial: float | None
    curve: CurveSpec
    enabled: bool
    measurement: str
    ct_ratio: float | None = None
    definite_delay_s: float | None = None


@dataclass(frozen=True)
class CoordinationPair:
    primary_stage_id: str
    backup_stage_id: str
    breaker_time_s: float
    required_margin_s: float


@dataclass(frozen=True)
class CoordinationResult:
    status: str
    currents_primary_a: tuple[float, ...]
    pairs: tuple[dict, ...]
    diagnostics: tuple[dict, ...]


class ProtectionError(ValueError):
    """Dato inválido o cobertura ausente, siempre con diagnóstico trazable."""

    def __init__(self, code, message, stage=None, *, status="invalid_input"):
        super().__init__(message)
        item = asdict(stage) if isinstance(stage, RelayStage) else (stage or {})
        self.status = status
        self.diagnostic = {"code": code, "level": "error",
            "object_id": item.get("object_id"), "relative_path": item.get("relative_path"),
            "message": message}


def diagnostic(code, message, stage=None, level="warning"):
    item = ProtectionError(code, message, stage).diagnostic
    item["level"] = level
    return item


def number(value, label, *, positive=False, nonnegative=False, stage=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ProtectionError("missing_or_nonfinite", f"{label} debe ser un número finito", stage)
    if (positive and value <= 0) or (nonnegative and value < 0):
        raise ProtectionError("nonphysical_setting", f"{label} fuera de su dominio", stage)
    return float(value)


def stage_from_dict(value):
    data = dict(value)
    if not isinstance(data.get("curve"), CurveSpec):
        data["curve"] = CurveSpec(**data["curve"])
    result = RelayStage(**data)
    if not result.object_id or not result.relay_id or not isinstance(result.enabled, bool):
        raise ProtectionError("invalid_identity", "ID de etapa/relé y enabled booleano obligatorios", result)
    if result.measurement not in ("phase", "residual_3i0", "neutral"):
        raise ProtectionError("unsupported_measurement", "Medida no cubierta", result)
    return result
