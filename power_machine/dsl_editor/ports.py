"""Puertos por ID y propuestas privadas, sin conexión implícita por nombre."""
from copy import deepcopy
from dataclasses import asdict

from .schema import EditorError, finite, fingerprint, compiler_fingerprint


def validate_ports(draft, ir):
    ids = set()
    declared = {"input": set(ir.inputs), "output": set(ir.outputs)}
    actual = {"input": set(), "output": set()}
    for port in draft.ports:
        if not {"id", "name", "direction", "signal_kind", "unit", "target_id", "cardinality"} <= port.keys():
            raise EditorError("invalid_port", "Puerto requiere ID, dirección, señal, unidad, destino y cardinalidad")
        if not port["id"] or port["id"] in ids or port["direction"] not in declared or isinstance(port["cardinality"], bool) or port["cardinality"] != 1:
            raise EditorError("invalid_port", "ID duplicado, dirección inválida o cardinalidad distinta de uno")
        if port["name"] not in declared[port["direction"]] or port["name"] in actual[port["direction"]]:
            raise EditorError("unknown_port", f"Puerto inexistente o repetido: {port['id']}")
        if not port["signal_kind"]:
            raise EditorError("invalid_port", "signal_kind debe ser explícito")
        actual[port["direction"]].add(port["name"])
        ids.add(port["id"])
    if actual != declared:
        raise EditorError("missing_port", "Declare exactamente los puertos de entrada y salida del IR")


def check_connection(source, target, converter=None):
    if source["direction"] != "output" or target["direction"] != "input":
        raise EditorError("port_direction", "El cable requiere salida hacia entrada")
    if source["signal_kind"] != target["signal_kind"]:
        raise EditorError("signal_kind_mismatch", "Tipos de señal incompatibles")
    if source["unit"] is None or target["unit"] is None:
        raise EditorError("missing_unit", "Binding requiere unidades conocidas")
    if source["unit"] != target["unit"]:
        if not converter or converter.get("from_unit") != source["unit"] or converter.get("to_unit") != target["unit"] or not converter.get("provenance"):
            raise EditorError("unit_mismatch", "Unidad incompatible sin convertidor explícito con procedencia")
        finite(converter.get("factor"), "factor")
        finite(converter.get("offset"), "offset")
    return {"source_port_id": source["id"], "target_port_id": target["id"], "converter": deepcopy(converter)}


def propose_binding(draft, element_id, slot_id):
    if not element_id or not slot_id:
        raise EditorError("missing_binding_target", "Declare element_id y slot_id")
    return {"status": "proposed", "draft_id": draft.draft_id, "object_id": draft.object_id,
        "relative_path": draft.relative_path, "element_id": element_id, "slot_id": slot_id,
        "artifact_sha256": fingerprint(asdict(draft)), "connections": [], "checks": {},
        "runtime_applied": False, "pf_comparison": "sin validar contra PF"}


def prepare_binding(draft, element_id, slot_id, target_ports, connections, preview_result):
    """Comprueba prueba aislada y compatibilidad. panel integration aplica la propuesta."""
    from .compiler_adapter import compile_draft
    compiled = compile_draft(draft)
    if compiled.status != "completed":
        raise EditorError("binding_compile_failed", "El draft no compila dentro de la cobertura")
    payload = preview_result.get("payload", {})
    if (preview_result.get("schema") != "pm-parity-result-v1" or preview_result.get("module") != "dsl_editor" or preview_result.get("status") != "completed"
            or payload.get("artifact_sha256") != fingerprint(asdict(draft))
            or payload.get("compiler_sha256") != compiler_fingerprint()
            or not payload.get("initialization_completed") or not payload.get("time_s")):
        raise EditorError("binding_preview_required", "Se requiere preview completo e inicialización de este draft")
    if any(c.get("passed") is not True for c in payload.get("analytical_checks", [])) or payload.get("interpreter_comparison", {}).get("passed") is False:
        raise EditorError("binding_preview_failed", "Prueba aislada con comprobación analítica o de intérprete fallida")
    own, targets = {p["id"]: p for p in draft.ports}, {p["id"]: p for p in target_ports}
    if len(targets) != len(target_ports):
        raise EditorError("invalid_port", "IDs de puertos del slot duplicados")
    checked, used, used_target = [], set(), set()
    for connection in connections:
        pid, tid = connection["draft_port_id"], connection["target_port_id"]
        if pid not in own or tid not in targets:
            raise EditorError("unknown_port", "Conexión refiere a ID de puerto inexistente")
        if pid in used or tid in used_target:
            raise EditorError("port_cardinality", "Puerto conectado más de una vez")
        left, right = own[pid], targets[tid]
        checked.append(check_connection(left, right, connection.get("converter")) if left["direction"] == "output"
                       else check_connection(right, left, connection.get("converter")))
        used.add(pid)
        used_target.add(tid)
    if used != own.keys():
        raise EditorError("missing_port_binding", "Todos los puertos del draft requieren conexión explícita")
    binding = propose_binding(draft, element_id, slot_id)
    binding.update(connections=checked, target_ports=deepcopy(target_ports), port_mapping=deepcopy(connections),
        preview_result=deepcopy(preview_result), checks={"ports": True, "units": True, "parameters": True,
        "coverage": True, "initialization": True, "isolated_preview": True}, preview_run_id=preview_result["run_id"])
    return binding


def instantiate_controller(model, draft, binding):
    """Materializa un power_machine-model-v1 privado. No habilita el controlador en RMS."""
    from .compiler_adapter import compile_artifact
    compile_artifact(draft)
    if model.get("schema") != "power_machine-model-v1":
        raise EditorError("invalid_model", "Se requiere power_machine-model-v1")
    all_rows = [r for value in model.values() if isinstance(value, list) for r in value if isinstance(r, dict)]
    ids = {r.get("object_id", r.get("id")) for r in all_rows}
    if draft.object_id in ids:
        raise EditorError("duplicate_identity", "El controlador ya existe en el snapshot")
    if (binding.get("artifact_sha256") != fingerprint(asdict(draft)) or binding.get("status") != "proposed"
            or not binding.get("checks") or not all(binding["checks"].get(k) for k in
                ("ports", "units", "parameters", "coverage", "initialization", "isolated_preview"))):
        raise EditorError("binding_not_prepared", "Binding no comprobado para este draft")
    if binding.get("element_id") not in ids:
        raise EditorError("unknown_element", "element_id no existe en el snapshot")
    slots = model.get("dsl_slots", [])
    matches = [s for s in slots if s.get("id") == binding.get("slot_id") and s.get("element_id") == binding.get("element_id")]
    if len(matches) != 1:
        raise EditorError("unknown_slot", "panel integration debe proporcionar catálogo de slots por ID")
    target_ports = matches[0].get("ports")
    if target_ports is None or target_ports != binding.get("target_ports"):
        raise EditorError("slot_ports_mismatch", "Puertos de slot ausentes o distintos de los comprobados")
    checked = prepare_binding(draft, binding["element_id"], binding["slot_id"], target_ports,
                              binding["port_mapping"], binding["preview_result"])
    if checked["connections"] != binding["connections"]:
        raise EditorError("binding_changed", "Conexiones modificadas después de comprobación")
    result = deepcopy(model)
    result.setdefault("controllers", []).append({"id": draft.object_id, "path": draft.relative_path,
        "object_id": draft.object_id, "relative_path": draft.relative_path, "class": "ElmDsl",
        "name": draft.metadata.get("name", draft.draft_id), "members": [binding["element_id"]],
        "params": {k: deepcopy(v["value"]) for k, v in draft.parameters.items()},
        "units": {k: v["unit"] for k, v in draft.parameters.items()},
        "dsl_editor": {"draft": asdict(draft), "binding": deepcopy(binding), "runtime_applied": False,
            "pf_comparison": "sin validar contra PF"}})
    return result
