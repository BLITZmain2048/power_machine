"""Contrato de factores adimensionales de carga y procedencia SHA256."""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def model_digest(model: dict) -> str:
    """Huella semántica del modelo, independiente del formato JSON."""
    return digest(json.dumps(model, sort_keys=True, ensure_ascii=False,
                             allow_nan=False).encode("utf-8"))


@dataclass(frozen=True)
class SeriesStep:
    """Instante [s] y factores adimensionales por object_id de carga."""
    timestamp_iso: str
    t_s: float
    factors: dict[str, float]


@dataclass(frozen=True)
class Series:
    name: str
    steps: tuple[SeriesStep, ...]
    provenance: dict


def _number(value, context):
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Ausencia o número inválido en {context}: {value!r}") from None
    if not math.isfinite(result):
        raise ValueError(f"Ausencia no finita en {context}: {value!r}")
    return result


def read_series(model_path, manifest_path, name, *, default_factor=None) -> tuple[dict, Series]:
    """Lee CSV UTF-8 con separador punto y coma y verifica ambas huellas.

    Cada factor multiplica scale0, P y Q conservan su signo y coseno phi.
    El factor por defecto solo cubre columnas ausentes, nunca celdas vacías.
    Devuelve modelo intacto y serie mapeada a IDs, sin escribir archivos.
    """
    model_path, manifest_path = Path(model_path), Path(manifest_path)
    manifest_bytes = manifest_path.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8-sig"))
    entry = manifest.get("series", {}).get(name)
    if not entry:
        raise ValueError(f"Serie desconocida en manifiesto: {name}")
    model_bytes = model_path.read_bytes()
    if digest(model_bytes) != manifest.get("sha256_modelo"):
        raise ValueError("SHA256 del modelo no coincide con el manifiesto")
    series_path = (manifest_path.parent / entry["archivo"]).resolve()
    if not series_path.is_relative_to(manifest_path.parent.resolve()):
        raise ValueError("La serie debe permanecer dentro de la carpeta del manifiesto")
    series_bytes = series_path.read_bytes()
    if digest(series_bytes) != entry.get("sha256"):
        raise ValueError(f"SHA256 de la serie {name} no coincide con el manifiesto")
    model = json.loads(model_bytes.decode("utf-8-sig"))
    by_name = {}
    for load in model.get("loads", []):
        by_name.setdefault(load["name"], []).append(load["id"])
    if default_factor is not None:
        default_factor = _number(default_factor, "default_factor")
    reader = csv.reader(io.StringIO(series_bytes.decode("utf-8-sig")), delimiter=";")
    header = next(reader, [])
    if header[:2] != ["timestamp_iso", "t_s"]:
        raise ValueError("Cabecera requerida: timestamp_iso y t_s, separados por punto y coma")
    if len(header) != len(set(header)):
        raise ValueError("Columnas duplicadas en cabecera")
    columns = {}
    for column in header[2:]:
        ids = by_name.get(column, [])
        if not ids:
            raise ValueError(f"Columna de carga desconocida: {column}")
        if len(ids) != 1:
            raise ValueError(f"Nombre de carga ambiguo: {column}, IDs: {ids}")
        columns[column] = ids[0]
    missing = [load for load in model.get("loads", []) if load["id"] not in columns.values()]
    if missing and default_factor is None:
        raise ValueError("Carga sin columna, declare default_factor: " + ", ".join(x["name"] for x in missing))
    steps = []
    for row_number, row in enumerate(reader, 2):
        if len(row) != len(header):
            raise ValueError(f"Fila {row_number}: se esperaban {len(header)} celdas, recibidas {len(row)}")
        try:
            datetime.fromisoformat(row[0])
        except ValueError:
            raise ValueError(f"Fila {row_number}: timestamp_iso inválido: {row[0]!r}") from None
        t_s = _number(row[1], f"fila {row_number}, t_s [s]")
        if steps and t_s <= steps[-1].t_s:
            raise ValueError(f"Fila {row_number}: t_s debe ser estrictamente creciente")
        factors = {columns[column]: _number(value, f"fila {row_number}, {column}")
                   for column, value in zip(header[2:], row[2:])}
        factors.update({load["id"]: default_factor for load in missing})
        steps.append(SeriesStep(row[0], t_s, factors))
    if not steps:
        raise ValueError("Serie sin instantes")
    if entry.get("muestras") != len(steps):
        raise ValueError("Cantidad de muestras incompatible con el manifiesto")
    return model, Series(name, tuple(steps), {
        "model_sha256": digest(model_bytes), "model_semantic_sha256": model_digest(model),
        "series_sha256": digest(series_bytes), "manifest_sha256": digest(manifest_bytes),
        "manifest": manifest, "default_factor": default_factor,
        "missing_columns_defaulted": [load["id"] for load in missing],
        "model_path": str(model_path), "series_path": str(series_path),
    })
