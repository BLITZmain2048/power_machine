"Explicit data, numerical methods and traceable results for shortcircuit studies."
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FaultSpec:
    bus_id: str
    kind: str
    r_fault_ohm: float = 0.0
    x_fault_ohm: float = 0.0


@dataclass(frozen=True)
class ShortCircuitOptions:
    method: str
    case: str
    frequency_hz: float = 50.0
    min_break_time_s: float | None = None
    fault_duration_s: float | None = None
    profile: str = "iec_radial_v1"
    lv_tolerance_pct: float | None = None
    prefault_voltage_pu: float | None = None
    inception_angle_rad: float = 0.0
    rl_window_start_s: float = 0.0
    rl_window_end_s: float = 0.02
    rl_steps: int = 4096
    normative_evidence: tuple[dict, ...] = ()
    backend: str = "auto"
    diagnostic_condition: bool = False


@dataclass(frozen=True)
class ShortCircuitData:
    schema: str
    elements: dict[str, dict]
    sources: dict[str, dict]
    assumptions: tuple[dict, ...] = ()


@dataclass(frozen=True)
class ShortCircuitResult:
    status: str
    fault: dict
    quantities: dict
    contributions: tuple[dict, ...]
    diagnostics: tuple[dict, ...]


class SCError(ValueError):
    "Electrical-data or coverage error with an explicit code, identity and study status."

    def __init__(self, code, message, item=None, *, status="invalid_input", magnitude=None):
        super().__init__(message)
        item = item or {}
        self.status = status
        self.code = code
        self.diagnostic = {
            "code": code, "level": "error", "object_id": item.get("object_id", item.get("id")),
            "relative_path": item.get("relative_path", item.get("path")), "message": message,
        }
        if magnitude is not None:
            self.diagnostic["magnitude"] = magnitude


def diagnostic(code, message, item=None, level="warning", **extra):
    "Build a traceable diagnostic using original element IDs rather than display names."
    d = SCError(code, message, item).diagnostic
    d.update(level=level, **extra)
    return d
