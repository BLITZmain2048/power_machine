"""Contratos del editor. Señales y parámetros conservan unidades explícitas."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROFILE = "pf_blkdef_fields_v1/compiler_existing_v1"
MAX_SOURCE_BYTES = 1024 * 1024
MAX_DRAFT_BYTES = 32 * 1024 * 1024
MAX_STATES = 256
MAX_PORTS = 256
MAX_NODES = 512


def compiler_fingerprint():
    """Huella de los archivos locales que determinan compilación e integración."""
    directory = ROOT / "power_machine/dynamics"
    files = ("dsl_ir.py", "dsl_compile.py", "dsl_expr.py", "dsl_codegen.py", "dsl_flat.py",
             "dsl_initialization.py", "dsl_switching.py", "blocks.py", "solver.py", "interfaces.py")
    hashes = {name: hashlib.sha256((directory / name).read_bytes()).hexdigest() for name in files}
    for name in ("compiler_adapter.py", "preview.py", "events.py"):
        hashes["dsl_editor/"+name] = hashlib.sha256((ROOT / "power_machine/dsl_editor" / name).read_bytes()).hexdigest()
    return fingerprint(hashes)


@dataclass(frozen=True)
class ModelDraft:
    schema: str
    draft_id: str
    object_id: str
    relative_path: str
    source_format: str
    source_text: str
    parameters: dict
    ports: tuple[dict, ...]
    original_sha256: str | None
    metadata: dict


@dataclass(frozen=True)
class CompileDiagnostic:
    level: str
    code: str
    message: str
    object_id: str | None
    relative_path: str | None
    line: int | None = None
    column: int | None = None


@dataclass(frozen=True)
class CompilePreview:
    status: str
    source_sha256: str
    compiler_profile: str
    state_names: tuple[str, ...]
    algebraic_names: tuple[str, ...]
    diagnostics: tuple[CompileDiagnostic, ...]


@dataclass(frozen=True)
class PreviewCase:
    """Calendario [s], entradas ZOH a la derecha e inicialización explícita."""
    time_s: tuple[float, ...]
    inputs: dict[str, tuple[float, ...]]
    initial: dict
    expected: dict | None = None


class EditorError(ValueError):
    def __init__(self, code, message, *, line=None, column=None, status="invalid_input"):
        super().__init__(message)
        self.code, self.line, self.column, self.status = code, line, column, status


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encode(value).encode("utf-8")).hexdigest()


def source_hash(draft):
    return hashlib.sha256(draft.source_text.encode("utf-8")).hexdigest()


def draft_from_dict(data):
    data = dict(data)
    data["ports"] = tuple(data["ports"])
    return ModelDraft(**data)


def case_from_dict(data):
    return PreviewCase(tuple(data["time_s"]), {k: tuple(v) for k, v in data["inputs"].items()},
                       data["initial"], data.get("expected"))


def finite(value, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise EditorError("nonfinite_value", f"{label}: se requiere número finito")
    return float(value)


def confined_path(path, *, root=ROOT):
    root = Path(root).resolve()
    target = Path(path)
    target = (root / target).resolve() if not target.is_absolute() else target.resolve()
    if not target.is_relative_to(root):
        raise EditorError("outside_root", "La ruta debe permanecer dentro de la raíz")
    return target


def diagnostic(error, draft=None):
    return CompileDiagnostic("error", getattr(error, "code", "compiler_error"), str(error),
        getattr(draft, "object_id", None), getattr(draft, "relative_path", None),
        getattr(error, "line", None), getattr(error, "column", None))


def validate_draft(draft):
    if draft.schema != "pm-dsl-draft-v1" or draft.source_format != "pf_blkdef_fields_v1":
        raise EditorError("unsupported_format", "Solo se admite pm-dsl-draft-v1/pf_blkdef_fields_v1", status="unsupported")
    if not all(isinstance(x, str) and x for x in (draft.draft_id, draft.object_id, draft.relative_path)):
        raise EditorError("missing_identity", "Faltan draft_id, object_id o relative_path")
    if not isinstance(draft.source_text, str) or not isinstance(draft.parameters, dict) or not isinstance(draft.metadata, dict) or not isinstance(draft.ports, (tuple, list)):
        raise EditorError("invalid_draft", "Fuente, parámetros, metadata o puertos tienen tipo incorrecto")
    if len(draft.source_text.encode("utf-8")) > MAX_SOURCE_BYTES or len(encode(asdict(draft)).encode("utf-8")) > MAX_DRAFT_BYTES:
        raise EditorError("draft_too_large", "Fuente máxima 1 MiB, draft máximo 32 MiB")
    for name, spec in draft.parameters.items():
        if not isinstance(spec, dict) or not {"value", "unit", "range", "provenance"} <= spec.keys():
            raise EditorError("invalid_parameter", f"{name}: declare valor, unidad, rango y procedencia")
        value = spec["value"]
        if spec["unit"] is not None and not isinstance(spec["unit"], str):
            raise EditorError("invalid_parameter", f"{name}: unidad debe ser texto o null explícito")
        bounds = spec["range"]
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2 or not spec["provenance"]:
            raise EditorError("invalid_parameter", f"{name}: rango o procedencia ausentes")
        low, high = (None if x is None else finite(x, name) for x in bounds)
        if low is not None and high is not None and low > high:
            raise EditorError("invalid_parameter", f"{name}: rango invertido")
        if value is None:
            raise EditorError("missing_parameter", f"{name}: valor ausente, no se reemplaza por cero")
        if isinstance(value, (list, tuple)):
            if any(len(p) != 2 for p in value):
                raise EditorError("invalid_parameter", f"{name}: curva requiere pares")
            for pair in value:
                for x in pair:
                    finite(x, name)
        else:
            value = finite(value, name)
            if low is not None and value < low or high is not None and value > high:
                raise EditorError("parameter_range", f"{name}: valor fuera del rango declarado")
