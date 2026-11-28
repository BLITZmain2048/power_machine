"Explicit data, numerical methods and traceable results for shortcircuit studies."
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from power_machine.resources import confined_output
import uuid

import numpy as np

from .schema import SCError, diagnostic


def json_value(value, unit=""):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, (complex, np.complexfloating)):
        if not math.isfinite(value.real) or not math.isfinite(value.imag):
            raise SCError("nonfinite_result", "Nonfinite phasor", status="numerical_failure")
        return {"real": float(value.real), "imag": float(value.imag), "unit": unit}
    if isinstance(value, dict):
        return {str(k): json_value(v, "kA" if "currents" in str(k) and str(k).endswith("_ka") else "ohm" if k == "z_th_ohm" else unit)
                for k, v in value.items()}
    if isinstance(value, (tuple, list, np.ndarray)):
        return [json_value(v, unit) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        raise SCError("nonfinite_result", "Nonfinite result", status="numerical_failure")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def confined_path(root, path):
    root = Path(root).resolve()
    workspace = Path(__file__).resolve().parents[2]
    if root == Path(root.anchor):
        raise SCError("path_outside_root", "The root must be a study directory")
    candidate = Path(path)
    resolved = (candidate if candidate.is_absolute() else root/candidate).resolve()
    if not resolved.is_relative_to(root):
        raise SCError("path_outside_root", "Path is outside the study root")
    return resolved


def envelope(result, model, study, options, *, run_id=None, variant_sha256=None, inputs=(), analytical_checks=()):
    method = options.get("method", "iec60909")
    formulas = [{"source": "power_machine.shortcircuit",
                 "standard": "IEC 60909-0" if method == "iec60909" else "analytical RL circuit",
                 "edition": "2016" if method == "iec60909" else "analytical_rl_v1",
                 "profile": options.get("profile"), "normative_status": "pending"}]
    return json_value({"schema": "power_machine-parity-result-v1", "module": "shortcircuit",
        "run_id": run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid.uuid4().hex,
        "model_sha256": fingerprint(model), "study_sha256": fingerprint(study),
        "variant_sha256": variant_sha256, "options": options, "status": result.status,
        "units": {"Ik_initial_sym_ka": "kA", "ip_peak_ka": "kA", "Ib_sym_ka": "kA", "Ith_ka": "kA",
                  "phase_currents_ka": "kA", "sequence_currents_ka": "kA", "z_th_ohm": "ohm"},
        "provenance": {"inputs": list(inputs), "formulas": formulas,
                       "analytical_checks": list(analytical_checks),
                       "normative_evidence": options.get("normative_evidence", []),
                       "ref_comparison": "not validated against the external reference"},
        "diagnostics": result.diagnostics,
        "payload": {"fault": result.fault, **result.quantities, "contributions": result.contributions}})


def write_result(root, result, *, historical=False):
    "Create an exclusive run directory and write atomic JSON without replacing previous runs."
    run_id = result["run_id"]
    if not isinstance(run_id, str) or not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
        raise SCError("invalid_run_id", "run_id must be a simple identifier")
    payload = json.dumps(json_value(result), ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    folder = ("results/archive/shortcircuit" if historical else "results/parity/shortcircuit")
    directory = confined_output(Path(root).expanduser().resolve() / folder / run_id, root)
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)
    temporary = directory/"result.json.tmp"
    with temporary.open("x", encoding="utf-8") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(directory/"result.json")
    return directory/"result.json"
