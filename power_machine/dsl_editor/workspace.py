"""Revisiones inmutables, importación de solo lectura y biblioteca de control."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import uuid

from .schema import (ModelDraft, PROFILE, ROOT, EditorError, confined_path,
                     draft_from_dict, encode, fingerprint, validate_draft, compiler_fingerprint)


def new_draft(fields, parameters, ports, *, name="Controlador DSL"):
    draft_id = uuid.uuid4().hex
    return ModelDraft("pm-dsl-draft-v1", draft_id, "pm_dsl_" + uuid.uuid4().hex,
        "pm://dsl/" + draft_id, "ref_blkdef_fields_v1", encode(fields), deepcopy(parameters),
        tuple(deepcopy(ports)), None, {"origin": "pm_created", "ref_object_id": None,
            "ref_relative_path": None, "name": name, "ref_comparison": "sin validar contra la referencia externa"})


def parameter(value, unit, low=None, high=None, *, provenance="plantilla analítica local, sin validar contra la referencia externa"):
    return {"value": value, "unit": unit, "range": [low, high], "provenance": provenance}


def port(name, direction, unit, *, signal_kind="continuous_real", port_id=None):
    return {"id": port_id or uuid.uuid4().hex, "name": name, "direction": direction,
            "signal_kind": signal_kind, "unit": unit, "target_id": None, "cardinality": 1}


def controller_templates():
    """Cinco fixtures, no tipos de AVR/governor homologados en el simulador comercial de referencia."""
    return [{"id": name, "title": title, "source_format": "ref_blkdef_fields_v1",
             "ref_comparison": "sin validar contra la referencia externa"} for name, title in (
        ("gain", "Ganancia algebraica"), ("first_order", "Retardo de primer orden"),
        ("integrator", "Integrador"), ("limited_integrator", "Integrador limitado non-windup"),
        ("pi", "PI con límites de salida"))]


def template_draft(template_id, *, values=None, signal_unit="pu"):
    params = {}
    states = []
    if template_id == "gain":
        params = {"K": parameter(3., "1")}
        equations = ["y=K*u"]
    elif template_id == "first_order":
        params = {"K": parameter(2., "1"), "T": parameter(.5, "s", 0.)}
        states, equations = ["x"], ["limfix(T)=(0,)", "inc(x)=0", "x.=(K*u-x)/T", "y=x"]
    elif template_id in ("integrator", "limited_integrator"):
        params = {"K": parameter(1., "1/s")}
        states, equations = ["x"], ["inc(x)=0", "x.=K*u", "y=x"]
        if template_id == "limited_integrator":
            params.update(low=parameter(0., signal_unit), high=parameter(1., signal_unit))
            equations[-1] = "y=limstate(x,low,high)"
    elif template_id == "pi":
        params = {"Kp": parameter(2., "1"), "Ki": parameter(1., "1/s"),
                  "low": parameter(-10., signal_unit), "high": parameter(10., signal_unit)}
        states, equations = ["x"], ["inc(x)=0", "x.=Ki*u", "y=picontrol_const(x,low,high,Kp*u)"]
    else:
        raise EditorError("unknown_template", "Plantilla desconocida")
    for key, value in (values or {}).items():
        if key not in params:
            raise EditorError("unknown_parameter", f"Parámetro no declarado: {key}")
        params[key]["value"] = value
    if "low" in params and params["low"]["value"] > params["high"]["value"]:
        raise EditorError("invalid_limits", "Límites invertidos")
    fields = {"sInput": ["u"], "sOutput": ["y"], "sStates": states, "sParams": list(params), "sAddEquat": equations}
    draft = new_draft(fields, params, [port("u", "input", signal_unit), port("y", "output", signal_unit)], name=template_id)
    return replace(draft, metadata={**draft.metadata, "template_id": template_id,
        "state_units": {s: signal_unit for s in states}})


def revise_draft(draft, *, fields=None, parameters=None, ports=None):
    """La identidad permanece. La huella del artefacto incorpora parámetros."""
    return replace(draft, source_text=draft.source_text if fields is None else encode(fields),
        parameters=deepcopy(draft.parameters if parameters is None else parameters),
        ports=tuple(deepcopy(draft.ports if ports is None else ports)))


def compose_chain(drafts, connections):
    """Acopla hojas 1:1 por ID en BlkRef/BlkSig del compilador existente.

    Las conexiones enumeran source_port_id y target_port_id de cada par
    consecutivo. No introduce retardo ni resuelve ciclos algebraicos.
    """
    from .compiler_adapter import compile_artifact, parse_fields
    from .ports import check_connection
    if len(drafts) < 2 or len(connections) != len(drafts)-1 or len({d.draft_id for d in drafts}) != len(drafts):
        raise EditorError("invalid_composition", "Se requieren hojas distintas y un cable explícito por par consecutivo")
    artifacts = [compile_artifact(d) for d in drafts]
    if any(b.nodes or len(b.ir.inputs) != 1 or len(b.ir.outputs) != 1 for b in artifacts):
        raise EditorError("unsupported_composition", "Composición inicial solo de hojas con entrada y salida únicas", status="unsupported")
    for left, right, connection in zip(drafts, drafts[1:], connections):
        sources = {p["id"]: p for p in left.ports if p["direction"] == "output"}
        targets = {p["id"]: p for p in right.ports if p["direction"] == "input"}
        if connection.get("source_port_id") not in sources or connection.get("target_port_id") not in targets:
            raise EditorError("unknown_port", "Composición requiere los IDs de salida/entrada del par")
        check_connection(sources[connection["source_port_id"]], targets[connection["target_port_id"]])
    first = next(p for p in drafts[0].ports if p["direction"] == "input")
    last = next(p for p in drafts[-1].ports if p["direction"] == "output")
    root_draft = new_draft({}, {}, [port("u", "input", first["unit"], signal_kind=first["signal_kind"]),
                                   port("y", "output", last["unit"], signal_kind=last["signal_kind"])], name="Cadena DSL")
    root_path = root_draft.relative_path + "/chain.BlkDef"
    root_id = root_draft.object_id + "_type"
    objects = [{"object_id": root_id, "relative_path": root_path, "class": "BlkDef", "parent_id": "", "name": "Cadena"}]
    attributes, parameters, states, state_units, nodes = {}, {}, [], {}, []
    for index, (draft, block) in enumerate(zip(drafts, artifacts)):
        prefix = "b" + str(index) + "_"
        fields = parse_fields(draft)
        typ_path = root_path + "/type_" + draft.draft_id + ".BlkDef"
        typ_id = "pm_dsl_" + uuid.uuid4().hex
        ref_path = root_path + "/node_" + str(index) + ".BlkRef"
        ref_id = "pm_dsl_" + uuid.uuid4().hex
        objects.extend([{"object_id": typ_id, "relative_path": typ_path, "class": "BlkDef", "parent_id": "", "name": draft.draft_id},
                        {"object_id": ref_id, "relative_path": ref_path, "class": "BlkRef", "parent_id": root_id, "name": prefix}])
        attributes[typ_id] = fields
        attributes[ref_id] = {"typ_id": {"relative_path": typ_path}, "sStates": [prefix+s for s in block.state_names],
            "sParams": [prefix+p for p in fields["sParams"]], "sLowLimPar": [prefix+p for p in fields["sLowLimPar"]],
            "sUpLimPar": [prefix+p for p in fields["sUpLimPar"]]}
        parameters.update({prefix+k: deepcopy(v) for k, v in draft.parameters.items()})
        states.extend(prefix+s for s in block.state_names)
        state_units.update({prefix+s: draft.metadata.get("state_units", {}).get(s) for s in block.state_names})
        nodes.append(ref_path)
    fields = {"sInput": ["u"], "sOutput": ["y"], "sStates": states, "sParams": list(parameters), "sAddEquat": []}
    attributes[root_id] = fields
    sequence = [root_path, *nodes, root_path]
    for index, (source, target) in enumerate(zip(sequence, sequence[1:])):
        oid = "pm_dsl_" + uuid.uuid4().hex
        objects.append({"object_id": oid, "relative_path": root_path+"/wire_"+str(index)+".BlkSig",
                        "class": "BlkSig", "parent_id": root_id, "name": "wire_"+str(index)})
        attributes[oid] = {"pnodfrom": {"relative_path": source}, "pnodto": {"relative_path": target},
                           "inodfrom": 0, "inodto": 0}
    snapshot = {"objects": objects, "attributes": attributes}
    return replace(root_draft, source_text=encode(fields), parameters=parameters, metadata={**root_draft.metadata,
        "catalog_snapshot": snapshot, "catalog_sha256": fingerprint(snapshot), "type_path": root_path, "state_units": state_units,
        "composition": {"drafts": [{"draft_id": d.draft_id, "object_id": d.object_id, "relative_path": d.relative_path,
                                    "artifact_sha256": fingerprint(asdict(d))} for d in drafts], "connections": deepcopy(connections)}})


def import_draft(directory, dsl_path, *, port_specs=None, parameter_specs=None):
    """Importa identidad original y dependencias transitivas en un catálogo privado."""
    from power_machine.dynamics.dsl_compile import DynamicCatalog, _path
    from .compiler_adapter import FIELDS
    directory = Path(directory).resolve()
    catalog = DynamicCatalog(directory)
    compiled = catalog.compile_dsl(dsl_path)
    dsl = catalog.object_at(dsl_path)
    typ = catalog.object_at(_path(catalog.attrs(dsl)["typ_id"]))
    found = {}
    def collect(row):
        if row["object_id"] in found:
            return
        found[row["object_id"]] = row
        for child in catalog.children.get(row["object_id"], ()):
            collect(child)
        ref = _path(catalog.attrs(row).get("typ_id"))
        if ref:
            collect(catalog.object_at(ref))
    collect(typ)
    snapshot = {"objects": list(found.values()),
                "attributes": {oid: deepcopy(catalog.attrs(row)) for oid, row in found.items()}}
    fields = {k: deepcopy(v) for k, v in catalog.attrs(typ).items() if k in FIELDS}
    from power_machine.dynamics.dsl_compile import _names
    for key in FIELDS - {"sAddEquat"}:
        fields[key] = list(_names(fields.get(key)))
    specs = {k: parameter(deepcopy(v), None, provenance="export REF sin unidad declarada, sin validar contra la referencia externa")
             for k, v in compiled.parameters.items()}
    specs.update(deepcopy(parameter_specs or {}))
    ports = port_specs or [port(n, direction, None) for direction, group in
        (("input", compiled.ir.inputs), ("output", compiled.ir.outputs)) for n in group]
    manifest = directory / "manifest.json"
    origin_hash = hashlib.sha256(manifest.read_bytes()).hexdigest()
    source_inputs = [{"path": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                     for p in sorted(directory.iterdir()) if p.is_file()]
    draft = ModelDraft("pm-dsl-draft-v1", uuid.uuid4().hex, compiled.object_id, compiled.relative_path,
        "ref_blkdef_fields_v1", encode(fields), specs, tuple(deepcopy(ports)), origin_hash,
        {"origin": "external_reference", "ref_object_id": compiled.object_id, "ref_relative_path": compiled.relative_path,
         "catalog_snapshot": snapshot, "catalog_sha256": fingerprint(snapshot), "type_path": typ["relative_path"],
         "source_inputs": source_inputs, "ref_comparison": "sin validar contra la referencia externa"})
    validate_draft(draft)
    return draft


def _workspace_path(path):
    """Resuelve el destino expl?cito del workspace del usuario."""
    return Path(path).resolve()


def _write_unique(path, value):
    text = encode(value)  # Rechaza NaN antes de crear archivo.
    if path.exists():
        raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.parent / "staging" / (uuid.uuid4().hex + ".json")
    staging.parent.mkdir(parents=True, exist_ok=True)
    with staging.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(text + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    # Publicación atómica y exclusiva en el mismo volumen. El staging se
    # conserva como evidencia para cumplir la prohibición de borrar archivos.
    os.link(staging, path)


def save_draft(draft, path):
    validate_draft(draft)
    _write_unique(_workspace_path(path), asdict(draft))


def load_draft(path):
    draft = draft_from_dict(json.loads(_workspace_path(path).read_text(encoding="utf-8")))
    validate_draft(draft)
    return draft


def new_workspace(*, export_source_sha256=None):
    return {"schema": "pm-dsl-workspace-v1", "workspace_id": uuid.uuid4().hex,
        "compiler_profile": PROFILE, "compiler_version": compiler_fingerprint(), "export_source_sha256": export_source_sha256,
        "drafts": {}, "revisions": [], "bindings": [], "tests": [], "results": []}


def add_revision(workspace, draft):
    validate_draft(draft)
    result = deepcopy(workspace)
    revision = {"id": uuid.uuid4().hex, "draft_id": draft.draft_id, "draft": asdict(draft),
        "artifact_sha256": fingerprint(asdict(draft))}
    result["drafts"][draft.draft_id] = asdict(draft)
    result["revisions"].append(revision)
    # Resultados previos se conservan, su huella permite determinar vigencia.
    return result


def save_workspace(workspace, directory):
    validate_workspace(workspace)
    path = _workspace_path(Path(directory) / ("revision_" + uuid.uuid4().hex + ".json"))
    _write_unique(path, workspace)
    return path


def validate_workspace(workspace):
    if workspace.get("schema") != "pm-dsl-workspace-v1" or workspace.get("compiler_profile") != PROFILE:
        raise EditorError("invalid_workspace", "Workspace o perfil incompatible")
    encode(workspace)
    for key, data in workspace["drafts"].items():
        draft = draft_from_dict(data)
        validate_draft(draft)
        if key != draft.draft_id:
            raise EditorError("invalid_workspace", "Clave draft_id inconsistente")
    ids = [r["id"] for r in workspace["revisions"]]
    if len(ids) != len(set(ids)):
        raise EditorError("invalid_workspace", "Revisión duplicada")
    for revision in workspace["revisions"]:
        if fingerprint(revision["draft"]) != revision["artifact_sha256"]:
            raise EditorError("revision_hash_mismatch", "Huella de revisión incorrecta")


def load_workspace(path):
    workspace = json.loads(_workspace_path(path).read_text(encoding="utf-8"))
    validate_workspace(workspace)
    return workspace
