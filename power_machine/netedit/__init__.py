"""Editor de variantes independiente de Tk y punto de trabajo para spawn."""

from copy import deepcopy
from dataclasses import asdict
import uuid

from .schema import (NetworkVariant, VariantCommand, VariantCheck, VariantError,
                     model_sha256, new_variant, object_sha256, variant_from_dict,
                     variant_to_dict, variant_sha256, value_sha256, canonical_json,
                     element_ref)
from .commands import VariantHistory, create_command, edit_command, index_model
from .validate import check_variant
from .materialize import materialize_variant, materialized_layout
from .storage import save_variant, load_variant, safe_path
from ..topology import build_topology


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    """Materializa variante antes del escenario. No escribe archivos ni accede a Tk."""
    result = {"schema": "power_machine-parity-result-v1", "module": "netedit",
              "run_id": uuid.uuid4().hex, "model_sha256": None,
              "variant_sha256": None, "study_sha256": None,
              "options": {}, "status": "invalid_input",
              "units": {"s_base_mva": "MVA", "f_nom_hz": "Hz", "un_kv": "kV",
                        "p_mw": "MW", "q_mvar": "Mvar", "angle_deg": "deg"},
              "provenance": {"inputs": [], "formulas": [{"edition": "power_machine existente",
                             "profile": "variantes LF de secuencia positiva"}],
                             "analytical_checks": [], "normative_evidence": [],
                             "ref_comparison": "sin validar contra la referencia externa"},
              "diagnostics": [], "payload": {}}
    def cancelled():
        return cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel())
    try:
        canonical_json(request)
        if not str(request.get("schema")).endswith("-parity-request-v1") or request.get("module") != "netedit":
            raise ValueError("Esquema o módulo del request incompatible")
        safe_path(request["root"], root=request["root"])
        model = deepcopy(request["snapshot"]["model"])
        study = deepcopy(request["snapshot"].get("study", {}))
        fingerprints = request["snapshot"].get("fingerprints", {})
        result["options"] = deepcopy(request.get("options", {}))
        result["model_sha256"] = model_sha256(model)
        result["study_sha256"] = value_sha256(study)
        for key in ("model_sha256", "study_sha256"):
            if fingerprints.get(key) != result[key]:
                raise ValueError("Fingerprint ausente o inconsistente: " + key)
        variant = variant_from_dict(request["input"]["variant"])
        result["variant_sha256"] = variant_sha256(variant)
        if fingerprints.get("variant_sha256") is not None and fingerprints["variant_sha256"] != result["variant_sha256"]:
            raise ValueError("Fingerprint de variante inconsistente")
        result["provenance"]["inputs"] = [
            {"path": "snapshot/model", "sha256": result["model_sha256"]},
            {"path": "snapshot/study", "sha256": result["study_sha256"]},
            {"path": "input/variant", "sha256": result["variant_sha256"]}]
        if cancelled():
            result["status"] = "cancelled"
            return result
        if progress:
            progress({"phase": "checking", "revision": variant.revision})
        checks = check_variant(model, variant)
        entries = index_model(model)
        created_paths = {c.object_id: c.data.get("path") for c in variant.commands if c.op == "create"}
        result["diagnostics"] = [{"code": c.code, "level": c.severity,
                                  "object_id": c.object_id,
                                  "relative_path": entries.get(c.object_id, (None, {}))[1].get("path", created_paths.get(c.object_id)),
                                  "message": c.message} for c in checks]
        if any(c.severity == "error" for c in checks):
            return result
        materialized = materialize_variant(model, variant)
        topology = build_topology(materialized)
        result["payload"] = {"model": materialized, "model_sha256": model_sha256(materialized),
                             "layout": materialized_layout(model, variant), "islands": topology["islands"],
                             "elements": [element_ref(item) for _, item in index_model(materialized).values()],
                             "coverage": [{**element_ref(item), "lf": True, "rms": False, "cc": False}
                                          for _, item in index_model(materialized).values()
                                          if item.get("provenance", {}).get("origin") == "power_machine_created"]}
        if cancelled():
            result["status"], result["payload"] = "cancelled", {}
            return result
        result["status"] = "partial" if any(c.code == "unknown_switch_target" for c in checks) else "completed"
        if request.get("options", {}).get("run_loadflow"):
            from ..loadflow import run_loadflow
            if progress:
                progress({"phase": "loadflow"})
            lf = run_loadflow(materialized, study.get("scenario", {}), study.get("ldf_options", {}))
            result["payload"]["loadflow"] = lf
            if not lf["converged"]:
                result["status"] = "partial" if any(i["status"] == "resuelto" for i in lf["islands"]) else "not_converged"
        if cancelled():
            result["status"], result["payload"] = "cancelled", {}
        canonical_json(result)
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        result["status"], result["payload"] = "invalid_input", {}
        result["diagnostics"].append({"code": "invalid_input", "level": "error", "object_id": None,
                                      "relative_path": None, "message": str(exc)})
        # Los datos inválidos no se devuelven como opciones no serializables.
        try:
            canonical_json(result["options"])
        except (ValueError, TypeError):
            result["options"] = {}
    return result
