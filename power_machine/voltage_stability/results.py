"""Sobre serializable y utilidades de trazabilidad para PV/QV."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
import hashlib
import json
import math
import uuid


def json_data(value):
    if is_dataclass(value):
        return json_data(asdict(value))
    if isinstance(value, dict):
        return {str(key): json_data(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_data(item) for item in value]
    if isinstance(value, complex):
        return {"real": json_data(value.real), "imag": json_data(value.imag), "unit": "1"}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_data(value.item())
    return value


def fingerprint(value) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def diagnostic(code, message, *, level="warning", object_id=None, relative_path=None):
    return {"code": code, "level": level, "object_id": object_id,
            "relative_path": relative_path, "message": str(message)}


def envelope(model: dict, study: dict, options: dict) -> dict:
    return {
        "schema": "power_machine-voltage-stability-result-v1",
        "module": "voltage_stability",
        "run_id": "pvqv_" + uuid.uuid4().hex,
        "model_sha256": fingerprint(model),
        "study_sha256": fingerprint(study),
        "options": json_data(options),
        "status": "completed",
        "units": {"voltage": "p.u.", "active_power": "MW", "reactive_power": "Mvar"},
        "provenance": {
            "inputs": [],
            "formulas": [{"edition": "voltage_stability_v1",
                          "profile": "static_pv_qv_newton_v1",
                          "formula": "Puntos por Newton-Raphson independiente, sin continuación predictor-corrector"}],
            "analytical_checks": [],
            "pf_comparison": "sin validar contra PF",
            "collapse_method": "La no convergencia acota una estimación, no demuestra el punto de silla-nodo",
            "bases": {"s_base_mva": model.get("meta", {}).get("s_base_mva"),
                      "f_nom_hz": model.get("meta", {}).get("f_nom_hz")},
            "signs": {"generation": "positiva inyectada", "demand": "positiva consumida",
                      "reactive_source": "positiva inyectada por la fuente ficticia"},
        },
        "diagnostics": [],
        "payload": {},
    }
