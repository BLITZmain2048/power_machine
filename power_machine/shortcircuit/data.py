"Explicit data, numerical methods and traceable results for shortcircuit studies."
from __future__ import annotations

from dataclasses import asdict
import json
import math
from pathlib import Path

from .schema import SCError, ShortCircuitData

DATA_SCHEMA = "power_machine-sc-data-v1"


def from_dict(document: dict) -> ShortCircuitData:
    if not isinstance(document, dict) or not str(document.get("schema", "")).endswith("-sc-data-v1"):
        raise SCError("invalid_sc_schema", "power_machine-sc-data-v1 is required")
    if not isinstance(document.get("elements"), dict) or not isinstance(document.get("sources"), dict):
        raise SCError("invalid_sc_schema", "elements and sources must be mappings by ID")
    # Rechaza NaN incluso en metadatos, no solo en los parámetros usados.
    json.dumps(document, allow_nan=False)
    return ShortCircuitData(DATA_SCHEMA, document["elements"], document["sources"],
                            tuple(document.get("assumptions", ())))


def read_supplemental(path) -> ShortCircuitData:
    "Read supplementary JSON. The serialized entry point also checks path containment and hashes."
    from .results import confined_path
    workspace = Path(__file__).resolve().parents[2]
    path = Path(path).resolve()
    return from_dict(json.loads(path.read_text(encoding="utf-8-sig")))


def parameter(record, key, item=None, *, unit=None):
    "Require a value, unit and provenance. Missing data never become zero."
    datum = record.get("parameters", record).get(key)
    if not isinstance(datum, dict) or datum.get("value") is None:
        raise SCError("missing_sc_data", f"Missing short-circuit parameter {key}", item, magnitude=key)
    if not datum.get("unit"):
        raise SCError("missing_sc_data", f"Missing unit for {key}", item, magnitude=key)
    if not datum.get("analytical_fixture") and not (
            datum.get("source_path") and datum.get("source_attribute")):
        raise SCError("missing_sc_data", f"Missing provenance for {key}", item, magnitude=key)
    if unit is not None and datum["unit"] != unit:
        raise SCError("invalid_sc_unit", f"{key} requires unit {unit}", item, magnitude=key)
    return datum


def scalar(record, key, item=None, *, unit=None, positive=False):
    value = float(parameter(record, key, item, unit=unit)["value"])
    if not math.isfinite(value) or (positive and value <= 0):
        raise SCError("invalid_sc_value", f"{key} must be finite" +
                      (" and positive" if positive else ""), item, magnitude=key)
    return value


def text_value(record, key, item=None):
    return str(parameter(record, key, item)["value"])


def complex_value(value):
    if isinstance(value, dict):
        return complex(float(value["real"]), float(value["imag"]))
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return complex(*value)
    return complex(value)


def impedance_ohm(record, key, bus_kv, item=None, *, allow_zero=False):
    "Convert physical ohms or equipment-based pu impedance to ohms."
    d = parameter(record, key, item)
    z = complex_value(d["value"])
    if d["unit"] in ("pu", "p.u."):
        sn, ur = d.get("base_mva"), d.get("base_kv")
        if sn is None or ur is None or not math.isfinite(float(sn)) or not math.isfinite(float(ur)) or sn <= 0 or ur <= 0:
            raise SCError("missing_sc_data", f"Missing valid base for {key}", item, magnitude=key)
        z *= float(ur)**2 / float(sn)
    elif d["unit"] not in ("ohm", "Ω"):
        raise SCError("invalid_sc_unit", f"Unknown impedance unit for {key}", item)
    if not math.isfinite(z.real) or not math.isfinite(z.imag):
        raise SCError("nonfinite_sc_data", f"Nonfinite impedance {key}", item)
    if z.real < 0:
        raise SCError("nonphysical_resistance", f"Negative resistance in {key}", item, magnitude=key)
    if z == 0 and not allow_zero:
        raise SCError("zero_sc_impedance", f"Zero impedance {key}. Nodes are not merged automatically", item)
    return z


def data_dict(data):
    return asdict(data)
