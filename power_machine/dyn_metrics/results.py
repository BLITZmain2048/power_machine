"""Sobre y exportación CSV para indicadores de respuesta dinámica."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path
import uuid


def diagnostic(code, message, *, level="warning", signal_id=None):
    return {"code": code, "level": level, "signal_id": signal_id, "message": str(message)}


def json_data(value):
    if isinstance(value, dict):
        return {str(key): json_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_data(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_data(value.item())
    return value


def envelope(options):
    return {
        "schema": "pm-parity-result-v1",
        "module": "dyn_metrics",
        "run_id": "dyn_metrics_" + uuid.uuid4().hex,
        "status": "completed",
        "options": json_data(options),
        "units": {
            "time": "s", "frequency": "Hz", "rocof": "Hz/s",
            "voltage": "p.u.", "power": "MW", "damping_ratio": "1",
            "percentage": "%",
        },
        "provenance": {
            "inputs": [],
            "formulas": [
                "ROCOF por regresión lineal móvil, excluye los primeros 10 ms tras el evento",
                "Oscilación por ajuste logarítmico de picos de una señal seleccionada",
            ],
            "analytical_checks": [],
            "normative_evidence": [],
            "pf_comparison": "sin validar contra PF",
            "threshold_basis": "Perfil de referencia configurable, no es verificación normativa",
            "profile_label": "referencial, verificar contra la criterios aplicables al estudio",
        },
        "diagnostics": [],
        "payload": {"signals": [], "indicators": []},
    }


CSV_FIELDS = (
    "indicator_id", "signal_id", "kind", "label", "state", "value", "unit",
    "threshold", "threshold_unit", "profile", "note",
)


def export_indicators_csv(result: dict, path) -> Path:
    """Exporta la tabla sin transformar ausentes en ceros."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in result.get("payload", {}).get("indicators", []):
            writer.writerow({key: row.get(key) for key in CSV_FIELDS})
    return target


def result_json(result: dict) -> str:
    return json.dumps(json_data(result), ensure_ascii=False, indent=2, allow_nan=False)
