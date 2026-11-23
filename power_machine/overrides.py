"""Parámetros complementarios con procedencia explícita."""

from __future__ import annotations

import copy
import json
from pathlib import Path


def read_overrides(path: str | Path) -> dict:
    """Lee un mapa de ID de elemento a parámetros, fuente y fecha."""
    return json.loads(Path(path).read_text(encoding="utf-8"))


def apply_overrides(model: dict, overrides: dict) -> tuple[dict, list[str]]:
    """Aplica valores no nulos sin modificar el modelo de entrada."""
    result = copy.deepcopy(model)
    by_id = {item["id"]: item for group in ("branches", "branches3", "shunts", "loads", "gens")
             for item in result.get(group, [])}
    warnings = []
    for item_id, record in overrides.items():
        if item_id not in by_id:
            raise ValueError(f"Override para elemento desconocido: {item_id}")
        values = {key: value for key, value in record.get("params", {}).items() if value is not None}
        if not values:
            continue
        if not record.get("source") or not record.get("date"):
            raise ValueError(f"Override {item_id} exige source y date")
        by_id[item_id].setdefault("params", {}).update(values)
        warnings.append(f"Override {item_id}: {', '.join(sorted(values))}; fuente={record['source']}; fecha={record['date']}")
    return result, warnings
