"""Sobre serializable y CSV de sensibilidades, sin dependencias de interfaz."""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
import re
import uuid


def json_value(value):
    """Convierte valores numpy y no finitos a una forma JSON honesta."""
    if isinstance(value, dict):
        return {str(key): json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_value(value.item())
    return value


def fingerprint(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def diagnostic(code, message, *, level="warning", object_id=None, relative_path=None):
    return {"code": code, "level": level, "message": str(message),
            "object_id": object_id, "relative_path": relative_path}


def envelope(model, scenario, options):
    return {
        "schema": "power_machine-parity-result-v1",
        "module": "sensitivities",
        "run_id": "sens_" + uuid.uuid4().hex,
        "model_sha256": fingerprint(model),
        "study_sha256": fingerprint({"scenario": scenario}),
        "options": json_value(options),
        "status": "completed",
        "units": {"voltage": "p.u.", "active_power": "MW", "reactive_power": "Mvar",
                  "voltage_sensitivity_p": "p.u./MW", "voltage_sensitivity_q": "p.u./Mvar",
                  "ptdf": "MW/MW", "lodf": "MW/MW", "loss_sensitivity": "MW/MW"},
        "provenance": {
            "pf_comparison": "sin validar contra PF",
            "formulas": [{"profile": "ac_finite_difference_v1",
                          "formula": "dX/dz = (X(z+h)-X(z-h))/(2h)"},
                         {"profile": "lodf_linear_v1",
                          "formula": "LODF_mk = PTDF_mk/(1-PTDF_kk)"}],
            "signs": {"injection": "positiva inyectada", "branch_power": "positiva saliendo del extremo from"},
        },
        "diagnostics": [],
        "payload": {},
    }


CSV_FIELDS = ("kind", "object_id", "relative_path", "name", "from_bus_id", "to_bus_id",
              "u_pu", "du_dp_pu_per_mw", "du_dq_pu_per_mvar", "p_from_mw",
              "ptdf_mw_per_mw", "lodf_mw_per_mw", "predicted_post_outage_p_mw",
              "actual_post_outage_p_mw", "lodf_error_mw")


def export_csv(result: dict, directory, *, root) -> Path:
    """Exporta una tabla nueva bajo la raíz indicada, sin sobrescribir archivos."""
    root_path = Path(root).resolve()
    directory_path = Path(directory).resolve()
    if not directory_path.is_relative_to(root_path):
        raise ValueError("La exportación debe permanecer dentro de la carpeta de trabajo")
    run_id = result.get("run_id", "")
    if not re.fullmatch(r"sens_[A-Za-z0-9]+", run_id):
        raise ValueError("run_id no es seguro para exportación")
    directory_path.mkdir(parents=True, exist_ok=True)
    target = directory_path / f"{run_id}.csv"
    with target.open("x", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        payload = result.get("payload", {})
        for row in payload.get("buses", []):
            writer.writerow({"kind": "bus", "object_id": row.get("object_id"),
                             "relative_path": row.get("relative_path"), "name": row.get("name"),
                             "u_pu": row.get("u_pu"), "du_dp_pu_per_mw": row.get("du_dp_pu_per_mw"),
                             "du_dq_pu_per_mvar": row.get("du_dq_pu_per_mvar")})
        for row in payload.get("branches", []):
            writer.writerow({"kind": "branch", "object_id": row.get("object_id"),
                             "relative_path": row.get("relative_path"), "name": row.get("name"),
                             "from_bus_id": row.get("bus_from"), "to_bus_id": row.get("bus_to"),
                             "p_from_mw": row.get("p_from_mw"), "ptdf_mw_per_mw": row.get("ptdf_mw_per_mw"),
                             "lodf_mw_per_mw": row.get("lodf_mw_per_mw"),
                             "predicted_post_outage_p_mw": row.get("predicted_post_outage_p_mw"),
                             "actual_post_outage_p_mw": row.get("actual_post_outage_p_mw"),
                             "lodf_error_mw": row.get("lodf_error_mw")})
    return target
