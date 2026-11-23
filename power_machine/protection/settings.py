"""Workspace privado, revisiones inmutables y overrides con unidad y procedencia."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
import uuid

from .schema import ProtectionError, stage_from_dict

SETTINGS_SCHEMA = "power_machine-protection-settings-v1"
WORKSPACE_ROOT = Path.cwd().resolve()
SETTING_UNITS = {"pickup_primary_a": "A", "time_dial": "1", "ct_ratio": "1",
    "definite_delay_s": "s", "enabled": "bool", "protected_element_id": "object_id",
    "terminal_id": "object_id", "measurement": "1", "curve": "CurveSpec"}


def confined_path(root, path):
    root = Path(root).resolve()
    target = Path(path)
    from ..resources import output_root
    if root == WORKSPACE_ROOT and target.is_absolute() and target.resolve().is_relative_to(output_root()):
        root = output_root()
    target = (target if target.is_absolute() else root/target).resolve()
    if root == Path(root.anchor) or not target.is_relative_to(root):
        raise ProtectionError("path_outside_root", "Ruta fuera de la carpeta de trabajo actual")
    return target


def new_workspace(stages=(), *, source_manifest_sha256=None, model_sha256=None, pairs=()):
    stages = tuple(stages)
    if len({s.object_id for s in stages}) != len(stages):
        raise ProtectionError("duplicate_stage_id", "IDs de etapas duplicados")
    return {"schema": SETTINGS_SCHEMA, "source_manifest_sha256": source_manifest_sha256,
        "revision": 0, "stages": {s.object_id: asdict(s) for s in stages},
        "overrides": {}, "pairs": [asdict(p) for p in pairs], "model_sha256": model_sha256}


def validate_workspace(workspace):
    if workspace.get("schema") != SETTINGS_SCHEMA:
        raise ProtectionError("workspace_schema", "Esquema de workspace desconocido")
    revision = workspace.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        raise ProtectionError("invalid_revision", "revision debe ser entero no negativo")
    for key, data in workspace.get("stages", {}).items():
        if stage_from_dict(data).object_id != key:
            raise ProtectionError("stage_key_mismatch", "Clave distinta del ID de etapa")
    for stage_id, overrides in workspace.get("overrides", {}).items():
        if stage_id not in workspace["stages"]:
            raise ProtectionError("unknown_stage", "Override de una etapa ausente")
        for key, record in overrides.items():
            if key not in SETTING_UNITS or record.get("unit") != SETTING_UNITS[key] or not record.get("provenance"):
                raise ProtectionError("override_provenance", "Override exige atributo, unidad y procedencia")
    json.dumps(workspace, allow_nan=False)


def effective_stages(workspace):
    validate_workspace(workspace)
    output = []
    for key, data in workspace["stages"].items():
        data = deepcopy(data)
        data.update({name: deepcopy(record["value"]) for name, record in workspace["overrides"].get(key, {}).items()})
        output.append(stage_from_dict(data))
    return tuple(output)


def revise_settings(workspace, stage_id, changes, *, provenance, units):
    """Devuelve nueva revisión, conserva el stage importado y no toca exports."""
    validate_workspace(workspace)
    if stage_id not in workspace["stages"]:
        raise ProtectionError("unknown_stage", "Etapa desconocida")
    effective = next(s for s in effective_stages(workspace) if s.object_id == stage_id)
    if effective.curve.family == "sampled" and "curve" not in changes:
        if any(key in changes and changes[key] != getattr(effective, key) for key in ("pickup_primary_a", "time_dial")):
            raise ProtectionError("sampled_setting_requires_points", "Cambiar arranque/dial muestreado exige nuevos puntos y procedencia", effective)
    result = deepcopy(workspace)
    for key, value in changes.items():
        if key not in SETTING_UNITS or units.get(key) != SETTING_UNITS[key] or not provenance:
            raise ProtectionError("override_provenance", "Cambio exige unidad y procedencia explícitas")
        result["overrides"].setdefault(stage_id, {})[key] = {"value": value,
            "unit": units[key], "provenance": deepcopy(provenance)}
    result["revision"] += 1
    effective_stages(result)
    return result


def save_settings(workspace: dict, path) -> None:
    """Escribe atómicamente sin reemplazar revisiones existentes, solo dentro de raíz."""
    validate_workspace(workspace)
    effective_stages(workspace)
    path = confined_path(WORKSPACE_ROOT, path)
    payload = json.dumps(workspace, ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        # En Windows rename falla si el destino existe. El hard link también es exclusivo en POSIX.
        os.link(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def load_settings(path):
    value = json.loads(confined_path(WORKSPACE_ROOT, path).read_text(encoding="utf-8-sig"))
    validate_workspace(value)
    effective_stages(value)
    return value
