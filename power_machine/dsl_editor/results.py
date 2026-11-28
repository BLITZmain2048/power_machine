"""Frontera JSON pm-parity-result-v1 y trabajos sin dependencia de tkinter."""
from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import re
import uuid

from .schema import (ROOT, EditorError, confined_path, diagnostic, draft_from_dict,
                     case_from_dict, encode, fingerprint, compiler_fingerprint)


def envelope(status, payload, *, model=None, study=None, options=None, diagnostics=(), variant_sha256=None, inputs=()):
    runtime_hash = compiler_fingerprint()
    result = {"schema": "pm-parity-result-v1", "module": "dsl_editor", "run_id": uuid.uuid4().hex,
        "model_sha256": fingerprint(model or {}), "variant_sha256": variant_sha256,
        "study_sha256": fingerprint(study) if study is not None else None,
        "options": options or {}, "status": status,
        "units": {"time_s": "s", "inputs": payload.get("input_units", {}),
                  "outputs": payload.get("output_units", {}), "states": payload.get("state_units", {})},
        "provenance": {"inputs": [*inputs, {"path": "runtime://dsl_compiler_solver", "sha256": runtime_hash}],
            "formulas": [{"edition": runtime_hash,
            "profile": "ref_blkdef_fields_v1/compiler_existing_v1", "method": "DAESolver trapezoidal y conmutación existente"}],
            "analytical_checks": payload.get("analytical_checks", []), "normative_evidence": [],
            "ref_comparison": "sin validar contra la referencia externa"},
        "diagnostics": [asdict(d) if hasattr(d, "__dataclass_fields__") else d for d in diagnostics], "payload": payload}
    # Copia serializable, no ejecutables ni arrays numpy en frontera.
    return json.loads(encode(result))


def save_result(result, directory):
    path = Path(directory).resolve()
    if result.get("schema") != "pm-parity-result-v1" or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", result.get("run_id", "")):
        raise EditorError("invalid_result", "Esquema o run_id inválido")
    text = encode(result)
    target = path / result["run_id"] / "result.json"
    target.parent.mkdir(parents=True, exist_ok=False)
    from .workspace import _write_unique
    _write_unique(target, result)
    return target


def _verify_request(request):
    if request.get("schema") != "pm-parity-request-v1" or request.get("module") != "dsl_editor":
        raise EditorError("invalid_request", "Esquema o module incorrecto")
    confined_path(request["root"])
    if any(not isinstance(request.get(key), dict) for key in ("snapshot", "options", "input")):
        raise EditorError("invalid_request", "Snapshot, opciones e input deben ser objetos JSON")
    snapshot = request["snapshot"]
    if snapshot["model"].get("schema") != "power_machine-model-v1":
        raise EditorError("invalid_model", "Snapshot requiere power_machine-model-v1")
    hashes = snapshot["fingerprints"]
    for key, value in (("model_sha256", snapshot["model"]), ("study_sha256", snapshot["study"])):
        if hashes.get(key) != fingerprint(value):
            raise EditorError("snapshot_hash_mismatch", f"Huella inconsistente: {key}")
    variant = hashes.get("variant_sha256")
    if variant is not None and (not isinstance(variant, str) or len(variant) != 64 or any(c not in "0123456789abcdef" for c in variant)):
        raise EditorError("variant_hash_invalid", "Huella de variante inválida")
    encode(request)


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    """Entry point spawn. Nunca recibe referencias vivas de widgets."""
    draft = None
    snapshot = request.get("snapshot", {}) if isinstance(request, dict) and isinstance(request.get("snapshot", {}), dict) else {}
    options = request.get("options", {}) if isinstance(request, dict) and isinstance(request.get("options", {}), dict) else {}
    try:
        _verify_request(request)
        if cancel is not None and cancel.is_set():
            return envelope("cancelled", {}, model=snapshot["model"], study=snapshot["study"], options=options)
        input_data = request["input"]
        draft = draft_from_dict(input_data["draft"])
        actual = fingerprint(asdict(draft))
        if input_data.get("draft_sha256") != actual:
            raise EditorError("draft_hash_mismatch", "Huella del draft ausente o inconsistente")
        if "case" in input_data and input_data.get("case_sha256") != fingerprint(input_data["case"]):
            raise EditorError("case_hash_mismatch", "Huella del calendario ausente o inconsistente")
        operation = input_data.get("operation", "compile")
        from .compiler_adapter import compile_draft, block_view
        if operation == "compile":
            preview = compile_draft(draft)
            result = envelope(preview.status, {"compile": asdict(preview), "artifact_sha256": actual,
                "object_id": draft.object_id, "relative_path": draft.relative_path,
                "input_units": {p["name"]: p["unit"] for p in draft.ports if p["direction"] == "input"},
                "output_units": {p["name"]: p["unit"] for p in draft.ports if p["direction"] == "output"},
                "state_units": {n: draft.metadata.get("state_units", {}).get(n) for n in preview.state_names}}, diagnostics=preview.diagnostics)
        elif operation == "blocks":
            result = envelope("completed", block_view(draft))
        elif operation in ("preview", "compare"):
            from .preview import simulate_preview, compare_interpreter
            case = case_from_dict(input_data["case"])
            method = simulate_preview if operation == "preview" else compare_interpreter
            result = method(draft, case, options=options, cancel=cancel, progress=progress)
        else:
            raise EditorError("unsupported_operation", "Operación fuera del catálogo", status="unsupported")
        result["model_sha256"] = fingerprint(snapshot["model"])
        result["study_sha256"] = fingerprint(snapshot["study"])
        result["variant_sha256"] = snapshot["fingerprints"].get("variant_sha256")
        result["options"] = json.loads(encode(options))
        result["provenance"]["inputs"] += [{"path": "request://snapshot/model", "sha256": fingerprint(snapshot["model"])},
            {"path": "request://snapshot/study", "sha256": fingerprint(snapshot["study"])},
            {"path": "workspace://draft/" + draft.draft_id, "sha256": actual}]
        if "case" in input_data:
            result["provenance"]["inputs"].append({"path": "request://preview_case", "sha256": input_data["case_sha256"]})
        encode(result)
        return result
    except (EditorError, ValueError, KeyError, TypeError, OSError, RecursionError, AttributeError) as exc:
        # Incluso un request mal formado devuelve una envoltura JSON finita.
        try:
            encode(snapshot)
            encode(options)
        except (ValueError, TypeError, RecursionError):
            snapshot, options = {}, {}
        return envelope(getattr(exc, "status", "invalid_input"),
            {"object_id": getattr(draft, "object_id", None), "relative_path": getattr(draft, "relative_path", None)},
            model=snapshot.get("model", {}), study=snapshot.get("study"), options=options,
            diagnostics=[diagnostic(exc, draft)])
