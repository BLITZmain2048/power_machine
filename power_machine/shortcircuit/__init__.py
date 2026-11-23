"Explicit data, numerical methods and traceable results for shortcircuit studies."
from __future__ import annotations

import copy
import json
from dataclasses import asdict
from pathlib import Path

from .data import from_dict, read_supplemental
from .results import confined_path, envelope, file_sha256, fingerprint, write_result
from .schema import FaultSpec, SCError, ShortCircuitData, ShortCircuitOptions, ShortCircuitResult, diagnostic
from .solver import QUANTITY_KEYS, _cancelled, _run_shortcircuit, run_shortcircuit

__all__ = ["FaultSpec", "ShortCircuitOptions", "ShortCircuitData", "ShortCircuitResult",
           "read_supplemental", "run_shortcircuit", "execute_request"]


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    "Execute a serialized study request after validating snapshot schemas and hashes.\n\nNo GUI is imported and results are persisted only when explicitly requested."
    model, study, options, inputs = {}, {}, {}, []
    variant_sha256, root = None, None
    try:
        request = copy.deepcopy(request)
        json.dumps(request, allow_nan=False)
        if not str(request.get("schema")).endswith("-parity-request-v1") or request.get("module") != "shortcircuit":
            raise SCError("invalid_request_schema", "Short-circuit requests require power_machine-parity-request-v1")
        root = Path(request["root"]).resolve()
        if root == Path(root.anchor):
            raise SCError("path_outside_root", "The root must be a study directory")
        workspace = Path(__file__).resolve().parents[2]
        snapshot = request["snapshot"]
        model, study = snapshot["model"], snapshot["study"]
        hashes = snapshot["fingerprints"]
        for key, value in (("model_sha256", model), ("study_sha256", study)):
            if hashes.get(key) != fingerprint(value):
                raise SCError("snapshot_hash_mismatch", f"Hash mismatch {key}")
        variant_sha256 = hashes.get("variant_sha256")
        options = request["options"]
        sc_options = ShortCircuitOptions(**options)
        options = asdict(sc_options)
        for evidence in sc_options.normative_evidence:
            if not isinstance(evidence, dict) or not all(evidence.get(k) for k in ("path", "sha256", "edition", "section")):
                raise SCError("invalid_normative_evidence", "Evidence requires a path, hash, edition and section")
            evidence_path = confined_path(root, evidence["path"])
            if file_sha256(evidence_path) != evidence["sha256"]:
                raise SCError("normative_evidence_hash_mismatch", "Normative evidence hash mismatch")
            inputs.append({"path": evidence["path"], "sha256": evidence["sha256"], "role": "normative_evidence"})
        input_data = request["input"]
        if input_data.get("supplemental_path") is not None:
            path = confined_path(root, input_data["supplemental_path"])
            sha = file_sha256(path)
            if input_data.get("supplemental_sha256") != sha:
                raise SCError("supplemental_hash_mismatch", "Short-circuit supplement hash mismatch")
            data = read_supplemental(path)
            inputs.append({"path": str(path.relative_to(root)), "sha256": sha})
        else:
            document = input_data["supplemental"]
            sha = fingerprint(document)
            if input_data.get("supplemental_sha256") != sha:
                raise SCError("supplemental_hash_mismatch", "Inline short-circuit supplement hash mismatch")
            data = from_dict(document)
            inputs.append({"path": "snapshot/input/supplemental", "sha256": sha})
        fault = FaultSpec(**input_data["fault"])
        # Los cambios de operación del estudio se aplican después de la variante.
        electrical_model = _apply_scenario(model, study.get("scenario", {}))
        if _cancelled(cancel):
            result = ShortCircuitResult("cancelled", asdict(fault), dict.fromkeys(QUANTITY_KEYS), (), ())
        else:
            result = _run_shortcircuit(electrical_model, fault, sc_options, data, cancel=cancel, progress=progress)
        output = envelope(result, model, study, options, variant_sha256=variant_sha256, inputs=inputs)
        output["provenance"]["assumptions"] = list(data.assumptions)
        if electrical_model != model:
            output["provenance"]["materialized_scenario_sha256"] = fingerprint(electrical_model)
        if input_data.get("output") and result.status != "cancelled":
            write_result(root, output, historical=bool(input_data.get("historical")))
        return output
    except (SCError, ValueError, TypeError, KeyError, OSError) as exc:
        d = exc.diagnostic if isinstance(exc, SCError) else diagnostic("invalid_request", str(exc), level="error")
        result = ShortCircuitResult(exc.status if isinstance(exc, SCError) else "invalid_input", {}, dict.fromkeys(QUANTITY_KEYS), (), (d,))
        # Las entradas inválidas pueden incluir NaN, se neutraliza su metadata.
        try:
            return envelope(result, model, study, options, variant_sha256=variant_sha256, inputs=inputs)
        except (SCError, ValueError, TypeError):
            return envelope(result, {}, {}, {}, inputs=inputs)


def _apply_scenario(model, scenario):
    """Apply a bounded SC scenario without deriving SC data from LF P/Q setpoints."""
    result = copy.deepcopy(model)
    index = {item["id"]: item for group in ("buses", "switches", "branches", "branches3", "gens", "loads", "shunts")
             for item in result.get(group, ())}
    for field, changes in scenario.items():
        if not changes:
            continue
        if field not in ("outserv", "closed", "load_scale", "gen_p_mw", "v_set_pu"):
            raise SCError("unsupported_sc_scenario", f"Unsupported scenario change {field}", status="unsupported")
        for item_id, value in changes.items():
            if item_id not in index:
                raise SCError("invalid_scenario_id", "Scenario element does not exist", {"id": item_id})
            item = index[item_id]
            if field == "outserv":
                if value not in (0, 1, False, True):
                    raise SCError("invalid_scenario_state", "outserv must be 0 or 1", item)
                item["outserv"] = int(value)
            elif field == "closed":
                if item.get("class") not in ("ElmCoup", "StaSwitch") or not isinstance(value, bool):
                    raise SCError("invalid_scenario_state", "closed requires a switch and a boolean", item)
                item["closed"] = value
            elif field in ("gen_p_mw", "v_set_pu"):
                raise SCError("unsupported_sc_scenario", "A load-flow setpoint does not define short-circuit prefault data. Supply explicit values", item, status="unsupported")
            # load_scale no afecta cargas ordinarias omitidas, se conserva el estudio.
    return result
