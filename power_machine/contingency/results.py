"Explicit data, numerical methods and traceable results for contingency studies."
from __future__ import annotations

import csv
from dataclasses import asdict, is_dataclass, fields
import hashlib
import json
import math
from pathlib import Path
import re
import uuid


def _json_data_reference(value):
    "Serialize dimensionless complex values as real/imag and nonfinite values as null."
    if value is None or type(value) in (str, int, bool):
        return value
    if type(value) is float:
        return value if math.isfinite(value) else None
    if is_dataclass(value):
        return {f.name:_json_data_reference(getattr(value,f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): _json_data_reference(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_data_reference(v) for v in value]
    if isinstance(value, complex):
        return {"real": _json_data_reference(value.real), "imag": _json_data_reference(value.imag), "unit": "1"}
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return _json_data_reference(value.item())
    return value


def _json_default(value):
    if is_dataclass(value):
        return {f.name:getattr(value,f.name) for f in fields(value)}
    if isinstance(value,complex):
        return {'real':value.real,'imag':value.imag,'unit':'1'}
    if hasattr(value,'item'):return value.item()
    raise TypeError(type(value).__name__)

def json_data(value):
    """Return independent JSON records, mapping nonfinite quantities to null."""
    try:
        return json.loads(json.dumps(value,default=_json_default,allow_nan=False,ensure_ascii=False))
    except (TypeError,ValueError,OverflowError):
        return _json_data_reference(value)

def fingerprint(value) -> str:
    # Las entradas no finitas se rechazan, nunca se hashean como un cero.
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def diagnostic(code, message, *, level="warning", object_id=None, relative_path=None, model_id=None):
    return dict(code=code, level=level, object_id=object_id,
                relative_path=relative_path, model_id=model_id, message=str(message))


def envelope(model, scenario, options, *, study=None, variant_sha256=None, ldf_options=None):
    return {"schema": "power_machine-parity-result-v1", "module": "contingency",
            "run_id": "n1_" + uuid.uuid4().hex,
            "model_sha256": fingerprint(model), "variant_sha256": variant_sha256,
            "study_sha256": fingerprint(study if study is not None else {"scenario": scenario}),
            "options": json_data(options), "status": "completed",
            "units": {"voltage": "p.u.", "angle": "deg", "active_power": "MW",
                      "reactive_power": "Mvar", "current": "kA", "loading": "%",
                      "max_mismatch_mva": "MVA", "max_mismatch_pu": "p.u."},
            "provenance": {"inputs": [], "formulas": [
                {"edition": "static_ac_screening_v1", "profile": "static_ac_screening_v1",
                 "formula": "N1=n, N2=n(n-1)/2, I/S and component severity"}],
                "analytical_checks": [], "normative_evidence": [],
                "pf_comparison": "not validated against PowerFactory",
                "ldf_options": json_data(ldf_options or {}),
                "bases": {"s_base_mva": model.get("meta", {}).get("s_base_mva"),
                          "f_nom_hz": model.get("meta", {}).get("f_nom_hz"),
                          "nominal_kv_by_bus": {b["id"]: b.get("un_kv") for b in model.get("buses", [])}},
                "signs": {"generation": "positive injection", "demand": "positive consumption",
                          "branch_power": "positive leaving the specified terminal",
                          "internal_angles": "rad", "reported_angles": "deg"},
                "threshold_basis": "Screening options, not normative limits"},
            "diagnostics": [], "payload": {}}


CSV_FIELDS = ("case_id", "status", "outage_ids", "relative_paths", "topology_sha256",
              "min_u_pu", "max_u_pu", "max_loading_pct", "max_apparent_loading_pct",
              "unsupplied_p_mw", "known_unsupplied_p_mw", "newly_unsupplied_p_mw",
              "energized_converged", "newly_deenergized_p_mw",
              "newly_deenergized_generation_p_mw", "newly_deenergized_load_ids",
              "newly_deenergized_gen_ids",
              "severity", "solved_bus_count", "unresolved_bus_count", "p_loss_mw",
              "reference_p_mw", "reference_q_mvar")


def export_run(result: dict, directory, *, root=None) -> tuple[Path, ...]:
    "Create an exclusive run directory. An explicit root confines all output paths."
    root = Path(root).resolve() if root is not None else Path(directory).resolve().parent
    target = Path(directory).resolve()
    if not target.is_relative_to(root):
        raise ValueError('[invalid_input] ' + "Output is outside the study directory")
    run_id = result["run_id"]
    if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
        raise ValueError('[invalid_input] ' + "run_id is not a safe directory name")
    clean = json_data(result)
    encoded = json.dumps(clean, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    target = target / run_id
    target.mkdir(parents=True, exist_ok=False)
    manifest = target / "manifest.json"
    manifest_tmp = target / "manifest.json.part"
    manifest_tmp.write_text(encoded, encoding="utf-8")
    table = target / "cases.csv"
    table_tmp = target / "cases.csv.part"
    with table_tmp.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for case in clean["payload"].get("cases", []):
            row = {key: case.get("metrics", {}).get(key) for key in CSV_FIELDS}
            row.update(case_id=case["case_id"], status=case["status"],
                       outage_ids=json.dumps([o["object_id"] for o in case["outages"]], ensure_ascii=False),
                       relative_paths=json.dumps([o["relative_path"] for o in case["outages"]], ensure_ascii=False))
            writer.writerow(row)
    # Publicar cada archivo mediante rename en el mismo directorio. El manifest
    # se publica al final para señalar que ambas escrituras terminaron.
    table_tmp.rename(table)
    manifest_tmp.rename(manifest)
    return manifest, table
