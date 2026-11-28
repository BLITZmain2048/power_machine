"""Ingestión de solo lectura, identidad por ruta y mapping de instalación explícito."""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path

from .curves import primary_pickup, validate_stage
from .schema import CurveSpec, RelayStage, ProtectionError, diagnostic, number

TIME_COLUMNS = ("time_equation_s", "time_setting_limited_s", "time_with_pickup_s")
SNAPSHOT_SCHEMA = "power_machine-protection-inventory-v1"


def file_sha256(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024*1024), b""):
            hasher.update(block)
    return hasher.hexdigest()


def identity(project_path, full_path, location=None, source_id=None):
    prefix = project_path.rstrip("\\")+"\\"
    relative = full_path[len(prefix):] if full_path.startswith(prefix) and location != "library_template" and "\\Library.IntPrjfolder\\" not in full_path else None
    return {"object_id": source_id or "prot_"+hashlib.sha256((project_path+"\n"+full_path).encode("utf-8")).hexdigest(),
        "relative_path": relative, "source_full_path": full_path, "source_object_id": source_id,
        "identity_origin": "ref_export_id" if source_id else "ref_path_derived"}


def _csv(path):
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        header = reader.fieldnames or []
        rows = []
        for index, row in enumerate(reader, 2):
            rows.append({"source_row": index, **{k: None if v in ("", "NULL", "null") else v for k, v in row.items()}})
    return header, rows


def read_protection_exports(directory) -> dict:
    """Lee una corrida seleccionada, nunca elige latest ni escribe en el origen."""
    directory = Path(directory).resolve()
    manifest_path = directory/"manifest.json"
    if not manifest_path.is_file():
        raise ProtectionError("explicit_run_required", "Seleccione una corrida con manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    project = manifest.get("project", {}).get("path")
    if not project:
        raise ProtectionError("missing_project_path", "manifest requiere project.path textual")
    manifest_hash = file_sha256(manifest_path)
    inputs = []
    inventory_files = []
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path.suffix.lower() in (".csv", ".json"):
            inventory_files.append({"path": str(path), "sha256": file_sha256(path)})
    header, object_rows = _csv(directory/"objects.csv")
    if "path" in header and "class" in header:
        dialect = "ref_protection_path_v1"
        path_key, id_key = "path", "object_id"
    elif {"source_full_path", "object_id", "class"} <= set(header):
        dialect = "explicit_identity_v1"
        path_key, id_key = "source_full_path", "object_id"
    else:
        raise ProtectionError("unknown_dialect", "Cabecera objects.csv no reconocida")
    native = {}
    if (directory/"inventory.json").is_file():
        data = json.loads((directory/"inventory.json").read_text(encoding="utf-8-sig"))
        native = {o["path"]: o for o in data.get("objects", [])}
    objects, diagnostics, seen = [], [], set()
    for row in object_rows:
        path = row.get(path_key)
        if not path:
            diagnostics.append(diagnostic("missing_object_path", "Fila sin ruta de origen"))
            continue
        obj = {**identity(project, path, source_id=row.get(id_key)), "class": row["class"],
               "source_row": row["source_row"], "raw": row, "attributes": native.get(path, {}).get("attributes", {})}
        if obj["object_id"] in seen:
            raise ProtectionError("duplicate_object_id", "Identidad de objeto duplicada", obj)
        seen.add(obj["object_id"])
        objects.append(obj)
    report_path = directory/"numeric/numeric_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8-sig")) if report_path.is_file() else {}
    point_path = directory/"numeric/curve_points_secondary.csv"
    numeric_header, raw_points = _csv(point_path) if point_path.is_file() else ([], [])
    if raw_points and (not {"curve_id", "element_path", *TIME_COLUMNS} <= set(numeric_header)
                       or not ({"current_secondary_A", "current_primary_A"} & set(numeric_header))):
        raise ProtectionError("unknown_numeric_dialect", "Cabecera numérica incompleta")
    points_by_curve = defaultdict(list)
    for row in raw_points:
        points_by_curve[row["curve_id"]].append(row)
    object_index = {o["source_full_path"]: o for o in objects}
    summaries = {r["element_path"]: r for r in report.get("curves", [])}
    stages = []
    for obj in objects:
        if obj["class"] not in ("RelToc", "RelIoc"):
            continue
        path = obj["source_full_path"]
        summary = summaries.get(path, {})
        curve_id = summary.get("curve_id")
        points = points_by_curve.get(curve_id, [])
        relay_path = next(("\\".join(path.split("\\")[:i+1]) for i in range(len(path.split("\\"))-2, -1, -1)
                           if path.split("\\")[i].endswith(".ElmRelay")), None)
        location = summary.get("object_location")
        ref = identity(project, path, location, obj["source_object_id"])
        characteristic_path = summary.get("characteristic_path") or None
        type_path = summary.get("type_path") or obj["raw"].get("type_path")
        cubicle_path = next(("\\".join(path.split("\\")[:i+1]) for i in range(len(path.split("\\"))-2, -1, -1)
                             if path.split("\\")[i].endswith(".StaCubic")), None)
        row = {**ref, "relay_id": identity(project, relay_path)["object_id"] if relay_path else None,
            "relay_source_full_path": relay_path, "curve_id": curve_id, "characteristic_path": characteristic_path,
            "characteristic_id": object_index.get(characteristic_path, {}).get("object_id"),
            "type_path": type_path, "type_id": object_index.get(type_path, {}).get("object_id"),
            "ct_id": None, "cubicle_id": identity(project, cubicle_path)["object_id"] if cubicle_path else None,
            "cubicle_source_full_path": cubicle_path,
            "object_location": location, "manifest_sha256": manifest_hash,
            "summary": summary, "raw_object": obj, "points": points,
            "calculation_status": "not_calculable", "missing": ["mapping", "ct_ratio", "time_semantics"]}
        if not summary:
            row["missing"].append("numeric_characteristic")
        if summary.get("status") != "derived":
            row["missing"].append("supported_characteristic")
        if summary.get("characteristic_path") and summary["characteristic_path"] not in object_index:
            row["missing"].append("characteristic_reference")
        stages.append(row)
    if manifest.get("status") != "completed":
        diagnostics.append(diagnostic("source_run_incomplete", "manifest.status="+str(manifest.get("status"))))
    inputs = inventory_files
    return {"schema": SNAPSHOT_SCHEMA, "source_directory": str(directory), "manifest": manifest,
        "source_manifest_sha256": manifest_hash, "inputs": inputs, "dialect": dialect,
        "column_mapping": {"object_path": path_key, "source_id": id_key if id_key in header else None,
                           "current_a": "current_primary_A" if "current_primary_A" in numeric_header else "current_secondary_A",
                           "time_columns": list(TIME_COLUMNS)},
        "objects": objects, "stages": stages, "numeric_report": {k: v for k, v in report.items() if k != "curves"},
        "coverage": {"objects": len(objects), "raw_object_rows": len(object_rows), "stages": len(stages),
            "points": len(raw_points), "calculable_installed": 0, "class_counts": dict(Counter(o["class"] for o in objects))},
        "diagnostics": diagnostics}


def compile_stages(inventory, mapping, *, time_semantics, model=None):
    """Mapping por source_full_path con TC/puerto. No usa nombres ni piso gráfico."""
    if inventory.get("schema") != SNAPSHOT_SCHEMA or time_semantics not in TIME_COLUMNS:
        raise ProtectionError("invalid_time_semantics", "Declare ecuación, ajuste limitado o tiempo con pickup")
    stages, coverage, diagnostics = [], [], []
    model_index = {e.get("id", e.get("object_id")): e for v in (model or {}).values() if isinstance(v, list)
                   for e in v if isinstance(e, dict) and (e.get("id") or e.get("object_id"))}
    for row in inventory["stages"]:
        entry = mapping.get(row["source_full_path"])
        missing = []
        try:
            if entry is None:
                raise ProtectionError("missing_mapping", "Falta mapping source_full_path a red y TC", row)
            if not entry.get("provenance"):
                raise ProtectionError("mapping_provenance", "Mapping requiere procedencia", row)
            for key in ("protected_element_id", "terminal_id", "measurement"):
                if not entry.get(key):
                    raise ProtectionError("missing_mapping_field", "Falta "+key, row)
            if model is not None:
                element = model_index.get(entry["protected_element_id"])
                if element is None or entry["terminal_id"] not in {element.get(k) for k in ("bus", "bus_from", "bus_to", "bus_hv", "bus_mv", "bus_lv")}:
                    raise ProtectionError("mapping_model_mismatch", "Elemento/terminal no corresponden al modelo", row)
            tc_path = entry.get("ct_source_full_path")
            if tc_path and not any(o["source_full_path"] == tc_path and o["class"] == "StaCt" for o in inventory["objects"]):
                raise ProtectionError("broken_ct_reference", "Referencia de TC ausente del inventario", row)
            summary = row["summary"]
            if "characteristic_reference" in row["missing"]:
                raise ProtectionError("broken_characteristic_reference", "Característica referida ausente del inventario", row)
            if summary.get("status") != "derived" or not row["points"]:
                raise ProtectionError("unsupported_export_curve", "Etapa sin puntos/semántica calculable", row)
            is_primary = inventory["column_mapping"]["current_a"] == "current_primary_A"
            if is_primary:
                pickup = number(summary.get("pickup_primary_A"), "pickup_primary_A", positive=True, stage=row)
                ratio = None
            else:
                pickup, ratio = primary_pickup(summary.get("pickup_secondary_A"), entry.get("ct_primary_rated_a"), entry.get("ct_secondary_rated_a"))
            points = []
            for point in row["points"]:
                if point.get("element_path") != row["source_full_path"]:
                    raise ProtectionError("point_identity_mismatch", "Punto de otra etapa", row)
                current_key = "current_primary_A" if is_primary else "current_secondary_A"
                current = number(float(point[current_key]) if point[current_key] is not None else None,
                                 current_key, positive=True, stage=row)
                time = number(float(point[time_semantics]) if point[time_semantics] is not None else None,
                              time_semantics, positive=True, stage=row)
                points.append((current if is_primary else current*ratio, time))
            curve = CurveSpec("sampled", row["curve_id"], {"points": points,
                "current_unit": "A_primary", "time_unit": "s", "interpolation": entry.get("interpolation", "log_log"), "extrapolate": False},
                {"source_full_path": row["source_full_path"], "curve_id": row["curve_id"],
                 "manifest_sha256": row["manifest_sha256"], "time_semantics": time_semantics,
                 "origin": "derived_from_exported_settings_not_native_vertices", "requires_ct": not is_primary,
                 "mapping": deepcopy(entry), "directional": bool(summary.get("tripping_direction_setting")),
                 "direction_allowed": entry.get("direction_allowed"), "edition": "export_settings"})
            if summary.get("block_outserv") not in (0, 1) or summary.get("ancestor_outserv") not in (True, False):
                raise ProtectionError("missing_service_state", "Estado de bloque/ancestro ausente, no se interpreta como deshabilitado", row)
            enabled = summary["block_outserv"] == 0 and summary["ancestor_outserv"] is False
            stage = RelayStage(row["object_id"], row["relative_path"], row["relay_id"],
                entry["protected_element_id"], entry["terminal_id"], pickup, summary.get("time_dial"), curve,
                enabled, entry["measurement"], ratio)
            validate_stage(stage)
            stages.append(stage)
        except (ProtectionError, ValueError, TypeError) as exc:
            if not isinstance(exc, ProtectionError):
                exc = ProtectionError("invalid_export_number", str(exc), row)
            missing.append(exc.diagnostic["code"])
            diagnostics.append(exc.diagnostic)
        coverage.append({"object_id": row["object_id"], "relative_path": row["relative_path"],
            "source_full_path": row["source_full_path"], "curve_id": row["curve_id"],
            "status": "not_calculable" if missing else "calculable", "missing": missing,
            "export_missing": [m for m in row["missing"] if m not in ("mapping", "ct_ratio", "time_semantics")]})
    return tuple(stages), coverage, diagnostics


def template_points(inventory, curve_id, *, time_semantics):
    """Vista secundaria de plantilla, no convierte a A primarios sin TC."""
    if time_semantics not in TIME_COLUMNS:
        raise ProtectionError("invalid_time_semantics", "Piso gráfico no es tiempo de operación")
    row = next((s for s in inventory["stages"] if s["curve_id"] == curve_id), None)
    if row is None:
        raise ProtectionError("unknown_curve_id", "curve_id ausente")
    return {"object_id": row["object_id"], "relative_path": row["relative_path"], "curve_id": curve_id,
        "current_unit": "A_secondary", "time_unit": "s", "scope": "template_only",
        "time_semantics": time_semantics, "points": [[float(p["current_secondary_A"]) if p["current_secondary_A"] else None,
            float(p[time_semantics]) if p[time_semantics] else None] for p in row["points"]]}
