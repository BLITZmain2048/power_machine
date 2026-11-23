"""Adaptador CC a PRO, sin sustituir corrientes de terminal por Ik de barra."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import uuid

from power_machine.protection.coordination import pair_point
from power_machine.protection.results import currents_from_shortcircuit, fingerprint
from power_machine.protection.schema import CoordinationPair, ProtectionError, stage_from_dict


def _json_value(value):
    """Rechaza NaN e Infinity antes de publicar un resultado serializable."""
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON no admite NaN o Infinity")
    return value


def _sha256(value):
    return hashlib.sha256(json.dumps(_json_value(value), ensure_ascii=False,
        sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _diagnostic(code, message, *, stage=None, level="warning"):
    return {"code": code, "message": message, "level": level,
            "object_id": getattr(stage, "object_id", None),
            "relative_path": getattr(stage, "relative_path", None)}


def _phase_for(stage, mapping):
    if stage.measurement == "phase":
        return mapping.get("phase")
    if stage.measurement == "residual_3i0":
        return "3I0"
    if stage.measurement == "neutral":
        return "neutral"
    return None


def _coverage(stage, mapping, current, diagnostics):
    missing = []
    if not stage.protected_element_id:
        missing.append("protected_element_id")
    if not stage.terminal_id:
        missing.append("terminal")
    if not mapping or not mapping.get("provenance"):
        missing.append("terminal_mapping")
    if stage.measurement == "phase" and mapping and mapping.get("phase") not in ("a", "b", "c"):
        missing.append("phase")
    if stage.ct_ratio is None or not isinstance(stage.ct_ratio, (int, float)) or isinstance(stage.ct_ratio, bool) or stage.ct_ratio <= 0:
        missing.append("ct_ratio")
    if current is None:
        missing.append("terminal_contribution")
    if missing:
        return "partial", missing
    if any(row.get("object_id") == stage.object_id for row in diagnostics):
        return "partial", ["terminal_contribution"]
    return "covered", []


def adapt_fault(stages, shortcircuit_result, *, fault_id, terminal_mapping, fault_run_id=None, stage_id=None):
    """Adapta un resultado CC inmutable a filas de corriente por etapa de relé.

    La salida conserva tanto la fase o medida como el extremo. ``current_primary_a``
    se obtiene exclusivamente de la contribución de rama publicada por shortcircuit.
    """
    if not isinstance(fault_id, str) or not fault_id:
        raise ValueError("fault_id es obligatorio")
    if not isinstance(shortcircuit_result, dict):
        raise ValueError("shortcircuit_result debe ser un objeto")
    run_id = fault_run_id or shortcircuit_result.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("fault_run_id es obligatorio")
    if fault_run_id is not None and fault_run_id != shortcircuit_result.get("run_id"):
        raise ValueError("fault_run_id no coincide con la corrida CC")
    stage_list = tuple(stage_from_dict(asdict(item) if hasattr(item, "object_id") else item) for item in stages)
    mapping = deepcopy(terminal_mapping or {})
    diagnostics = []
    try:
        currents, current_diagnostics = currents_from_shortcircuit(
            stage_list, shortcircuit_result,
            model_sha256=shortcircuit_result.get("model_sha256"),
            study_sha256=shortcircuit_result.get("study_sha256"),
            variant_sha256=shortcircuit_result.get("variant_sha256"),
            terminal_mapping=mapping,
        )
        diagnostics.extend(current_diagnostics)
    except ProtectionError as exc:
        currents = {}
        diagnostics.append(exc.diagnostic)
    rows = []
    for stage in stage_list:
        current = currents.get(stage.object_id)
        row_mapping = mapping.get(stage.object_id, {})
        coverage, missing = _coverage(stage, row_mapping, current, diagnostics)
        source_quantity = {"phase": "phase_currents_ka", "residual_3i0": "sequence_currents_ka.i0",
                           "neutral": "neutral_current_ka"}.get(stage.measurement)
        conversion = (current or {}).get("conversion")
        rows.append({
            "fault_run_id": run_id,
            "fault_id": fault_id,
            "element_id": stage.protected_element_id,
            "terminal": stage.terminal_id,
            "measurement": stage.measurement,
            "phase": _phase_for(stage, row_mapping),
            "current_primary_a": (current or {}).get("current_primary_a"),
            "ct_ratio": stage.ct_ratio,
            "stage_id": stage.object_id,
            "source_quantity": source_quantity,
            "coverage": coverage,
            "missing": missing,
            "provenance": {
                "shortcircuit_sha256": _sha256(shortcircuit_result),
                "model_sha256": shortcircuit_result.get("model_sha256"),
                "study_sha256": shortcircuit_result.get("study_sha256"),
                "variant_sha256": shortcircuit_result.get("variant_sha256"),
                "stage_id": stage_id,
                "terminal_mapping": row_mapping,
                "conversion": conversion,
                "conversion_units": "kA_to_A" if conversion else None,
            },
        })
    return {"fault_run_id": run_id, "fault_id": fault_id, "stage_id": stage_id,
            "rows": rows, "diagnostics": diagnostics}


def _pair_rows(stages, pairs, fault_rows):
    index = {stage.object_id: stage for stage in stages}
    output, diagnostics = [], []
    for pair_input in pairs:
        primary_id = pair_input.get("primary_stage_id")
        backup_id = pair_input.get("backup_stage_id")
        primary, backup = index.get(primary_id), index.get(backup_id)
        for fault in fault_rows:
            rows = {row["stage_id"]: row for row in fault["rows"]}
            p_row, b_row = rows.get(primary_id), rows.get(backup_id)
            missing = []
            if primary is None or backup is None:
                missing.append("stage")
            if p_row is None or b_row is None:
                missing.append("terminal_contribution")
            else:
                missing.extend(p_row["missing"])
                missing.extend(b_row["missing"])
            if primary is not None and backup is not None and primary.measurement != backup.measurement:
                missing.append("measurement")
            breaker = pair_input.get("breaker_time_s")
            required = pair_input.get("required_margin_s")
            if isinstance(breaker, bool) or not isinstance(breaker, (int, float)) or not math.isfinite(breaker) or breaker < 0:
                missing.append("breaker_time_s")
            if isinstance(required, bool) or not isinstance(required, (int, float)) or not math.isfinite(required) or required < 0:
                missing.append("required_margin_s")
            common = {"fault_run_id": fault["fault_run_id"], "fault_id": fault["fault_id"],
                      "stage_id": fault.get("stage_id"), "primary_stage_id": primary_id,
                      "backup_stage_id": backup_id, "breaker_time_s": breaker,
                      "required_margin_s": required}
            if missing:
                output.append({**common, "coverage": "not_evaluable", "covered": False,
                               "margin_s": None, "pass": None, "missing": sorted(set(missing)),
                               "reason": "Faltan datos de terminal, medida, TC, interruptor o margen requerido"})
                continue
            relation = CoordinationPair(primary_id, backup_id, float(breaker), float(required))
            point, point_diagnostics = pair_point(primary, backup, relation,
                p_row["current_primary_a"], b_row["current_primary_a"])
            diagnostics.extend(point_diagnostics)
            output.append({**common, **point, "coverage": "covered" if point["covered"] else "partial",
                           "missing": [] if point["covered"] else ["curve_operation"]})
    return output, diagnostics


def tcc_fault_markers(rows):
    """Filtra marcadores TCC sin inventar una posición para corrientes ausentes."""
    return [{"current_primary_a": row["current_primary_a"], "label":
             f"{row['fault_id']} · {row['stage_id']} · {row['measurement']}"}
            for row in rows if row.get("coverage") == "covered" and row.get("current_primary_a", 0) > 0]


def execute_request(request):
    """Ejecuta el enlace de filas CC-PRO con estados explícitos y trazabilidad."""
    try:
        request = deepcopy(request)
        if not str(request.get("schema")).endswith("-protection-bridge-request-v1"):
            raise ValueError("Se requiere schema power_machine-protection-bridge-request-v1")
        stages = tuple(stage_from_dict(item) for item in request.get("stages", ()))
        if not stages:
            raise ValueError("Se requiere al menos una etapa de relé")
        fault_rows = []
        for fault in request.get("faults", ()):
            fault_rows.append(adapt_fault(stages, fault.get("shortcircuit_result"),
                fault_id=fault.get("fault_id"), fault_run_id=fault.get("fault_run_id"),
                stage_id=fault.get("stage_id"), terminal_mapping=fault.get("terminal_mapping", {})))
        if not fault_rows:
            raise ValueError("Se requiere al menos una falla")
        pairs, pair_diagnostics = _pair_rows(stages, request.get("pairs", ()), fault_rows)
        currents = [row for fault in fault_rows for row in fault["rows"]]
        diagnostics = [item for fault in fault_rows for item in fault["diagnostics"]] + pair_diagnostics
        status = "completed" if all(row["coverage"] == "covered" for row in currents) and all(
            row["coverage"] == "covered" for row in pairs) and not diagnostics else "partial"
        return _json_value({"schema": "power_machine-protection-bridge-result-v1", "module": "protection_bridge",
            "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid.uuid4().hex,
            "status": status, "units": {"current_primary_a": "A", "ct_ratio": "1", "margin_s": "s", "time_s": "s"},
            "provenance": {"pf_comparison": "sin validar contra PF", "adapter": "terminal_measurement_v1"},
            "payload": {"fault_currents": currents, "pair_results": pairs,
                        "fault_markers": tcc_fault_markers(currents)}, "diagnostics": diagnostics})
    except (ProtectionError, TypeError, ValueError, KeyError) as exc:
        message = str(exc)
        return {"schema": "power_machine-protection-bridge-result-v1", "module": "protection_bridge",
                "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid.uuid4().hex,
                "status": "invalid_input", "units": {"current_primary_a": "A", "margin_s": "s"},
                "provenance": {"pf_comparison": "sin validar contra PF"}, "payload": {},
                "diagnostics": [_diagnostic("invalid_request", message, level="error")]}
