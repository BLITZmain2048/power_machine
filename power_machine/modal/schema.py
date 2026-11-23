'Schema support for power-system modal analysis.'
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
import numpy as np


@dataclass(frozen=True)
class StateRef:
    index: int
    object_id: str
    relative_path: str
    variable: str
    unit: str
    scale: float


@dataclass(frozen=True)
class OperatingPoint:
    x: np.ndarray
    z: np.ndarray
    time_s: float
    states: tuple[StateRef, ...]
    algebraic_scales: np.ndarray
    metadata: dict


@dataclass(frozen=True)
class ModalOptions:
    equilibrium_tol: float = 1e-8
    condition_limit: float = 1e12
    step_factor: float = 1.0
    participation_condition_limit: float = 1e8
    eigenvalue_tol_per_s: float = 1e-8


@dataclass(frozen=True)
class Linearization:
    fx: np.ndarray
    fz: np.ndarray
    gx: np.ndarray
    gz: np.ndarray
    a: np.ndarray
    diagnostics: tuple[dict, ...]


@dataclass(frozen=True)
class ModalResult:
    status: str
    modes: tuple[dict, ...]
    participation: tuple[dict, ...] | None
    diagnostics: tuple[dict, ...]


class DAEAdapter(Protocol):
    def evaluate(self, x: np.ndarray, z: np.ndarray,
                 time_s: float) -> tuple[np.ndarray, np.ndarray]: ...

    def clone(self): ...


def diagnostic(code, message, *, level="info", state=None, **details):
    'Return a structured diagnostic with name-independent object identity.'
    return {"code": code, "level": level,
            "object_id": state.object_id if state else None,
            "relative_path": state.relative_path if state else None,
            "message": message, **details}


class ModalError(ValueError):
    'Controlled interruption retaining serializable status and diagnostics.'
    def __init__(self, status, diagnostics):
        self.status = status
        self.diagnostics = tuple(diagnostics)
        super().__init__(" | ".join(d["message"] for d in diagnostics))


def fail(status, code, message, **details):
    raise ModalError(status, (diagnostic(code, message, level="error", **details),))


def real_vector(value, name):
    'Reject complex values, matrices and missing entries before converting to float64.'
    raw = np.asarray(value)
    if np.iscomplexobj(raw) or raw.ndim != 1 or raw.dtype.kind not in "fiu":
        fail('invalid_input', 'real_vector_required', f'{name} must be a real vector')
    result = raw.astype(np.float64, copy=True)
    if not np.isfinite(result).all():
        fail('invalid_input', 'nonfinite_input', ' contains nonfinite values')
    return result


def validate_options(options):
    for key, value in vars(options).items():
        if isinstance(value, bool) or not np.isfinite(value) or value <= 0:
            fail('invalid_input', 'invalid_options', f'{key} must be positive and finite')


def is_cancelled(cancel):
    return cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel())


def check_cancel(cancel):
    if is_cancelled(cancel):
        fail('cancelled', 'cancelled', 'Modal calculation cancelled')
