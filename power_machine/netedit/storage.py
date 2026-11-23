"""Persistencia atómica y revisión histórica, confinada a la raíz del trabajo."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tempfile
import uuid

from .schema import canonical_json, variant_from_dict, variant_sha256, variant_to_dict, value_sha256

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def safe_path(path, root=None):
    root = Path.cwd().resolve() if root is None else Path(root).resolve()
    path = Path(path)
    if root is None:
        raise ValueError("Raíz no resuelta")
    from ..resources import output_root
    if path.is_absolute() and path.resolve().is_relative_to(output_root()) and root == Path.cwd().resolve():
        root = output_root()
    if ".." in path.parts:
        raise ValueError("Ruta con ../ rechazada")
    target = (path if path.is_absolute() else root / path).resolve()
    if not target.is_relative_to(root):
        raise ValueError("Ruta o symlink fuera de raíz")
    return target


def atomic_json(value, path, *, root=None):
    """Conserva cualquier revisión previa con nombre único antes de os.replace."""
    target = safe_path(path, root)
    text = canonical_json(value) + "\n"
    if target.exists():
        try:
            previous_data = json.loads(target.read_text(encoding="utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("El destino existente no es un archivo de variante, historial o enlace compatible") from exc
        if not isinstance(previous_data, dict) or previous_data.get("schema") != value.get("schema"):
            raise ValueError("No se sobrescribe un modelo base ni un archivo de otro esquema")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Revalidar después de crear el directorio.
    target = safe_path(target, root)
    if target.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        previous = target.with_name(target.name + ".revision_" + stamp + "_" + uuid.uuid4().hex + ".json")
        with previous.open("xb") as handle:
            handle.write(target.read_bytes())
            handle.flush()
            os.fsync(handle.fileno())
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                     prefix=target.name + ".", suffix=".tmp", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)


def read_json(path, *, root=None):
    def reject_constant(value):
        raise ValueError("Constante JSON no finita: " + value)
    return json.loads(safe_path(path, root).read_text(encoding="utf-8-sig"),
                      parse_constant=reject_constant)


def save_variant(variant, path):
    data = variant_to_dict(variant)
    variant_from_dict(data)
    atomic_json(data, path)


def load_variant(path):
    return variant_from_dict(read_json(path))


def save_history(history, path):
    atomic_json(history.to_dict(), path)


def load_history(base_model, path):
    from .commands import VariantHistory
    return VariantHistory.from_dict(base_model, read_json(path))


def save_study_link(study, variant, variant_path, link_path):
    """Archivo lateral, no añade campos al StudyCase v1."""
    study = study.to_json() if hasattr(study, "to_json") else study
    if not str(study.get("schema")).endswith("-study-v1"):
        raise ValueError("Estudio v1 requerido")
    target = safe_path(variant_path)
    saved = load_variant(target)
    if variant_sha256(saved) != variant_sha256(variant):
        raise ValueError("La variante guardada no coincide")
    atomic_json({"schema": "power_machine-study-links-v1", "study_sha256": value_sha256(study),
                 "variant_path": target.relative_to(PROJECT_ROOT).as_posix(),
                 "variant_sha256": variant_sha256(variant)}, link_path)


def load_study_link(study, link_path):
    study = study.to_json() if hasattr(study, "to_json") else study
    data = read_json(link_path)
    if not str(data.get("schema")).endswith("-study-links-v1") or data.get("study_sha256") != value_sha256(study):
        raise ValueError("El hash del estudio lateral no coincide")
    if Path(data["variant_path"]).is_absolute():
        raise ValueError("La ruta lateral debe ser relativa")
    variant = load_variant(safe_path(data["variant_path"]))
    if data.get("variant_sha256") != variant_sha256(variant):
        raise ValueError("El hash de variante lateral no coincide")
    return variant
