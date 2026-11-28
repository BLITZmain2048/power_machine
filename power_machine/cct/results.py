'Results support for power-system critical clearing time.'
from __future__ import annotations

import hashlib
import json
import uuid


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def diagnostic(code, message, *, level="warning"):
    return {"code": code, "level": level, "message": str(message)}


def envelope(model, study, options):
    return {
        "schema": "pm-parity-result-v1",
        "module": "cct",
        "run_id": "cct_" + uuid.uuid4().hex,
        "model_sha256": fingerprint(model),
        "study_sha256": fingerprint(study),
        "variant_sha256": None,
        "status": "completed",
        "options": options,
        "units": {"clearing_time": "s", "resolution": "ms", "angle": "deg",
                  "voltage": "p.u.", "time": "s"},
        "provenance": {"ref_comparison": 'not validated against the external reference',
                       "formulas": [{"profile": "cct_rms_bisection_v1",
                                     "formula": 'Bisection with stable/unstable endpoints, COI angular criterion and voltage recovery'}],
                       "validity_messages": [], "assumptions": []},
        "diagnostics": [], "payload": {},
    }
