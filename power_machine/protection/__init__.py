"""API pública protection, worker serializable sin acceso a tkinter ni simulador comercial de referencia."""
from __future__ import annotations

from copy import deepcopy

import numpy as np

from .schema import CurveSpec, RelayStage, CoordinationPair, CoordinationResult, ProtectionError, diagnostic
from .curves import operating_time, operation, standard_curve, validate_stage
from .coordination import coordinate, current_grid, pair_point, cancelled
from .io_exports import read_protection_exports, compile_stages
from .settings import save_settings, load_settings, new_workspace, effective_stages, confined_path
from .results import fingerprint, envelope, currents_from_shortcircuit, write_result


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    """Snapshot completo, hashes verificados y corrientes primarias identificadas."""
    model, study, options, payload, inputs, stages = {}, {}, {}, {}, [], ()
    variant = None
    try:
        # Rechaza no finitos antes de copiar o publicar datos de la request.
        fingerprint(request)
        request = deepcopy(request)
        if not str(request.get("schema")).endswith("-parity-request-v1") or request.get("module") != "protection":
            raise ProtectionError("request_schema", "Request incompatible con protection")
        root = confined_path(request["root"], ".")
        snapshot = request["snapshot"]
        model, study = snapshot["model"], snapshot["study"]
        hashes = snapshot["fingerprints"]
        variant = hashes.get("variant_sha256")
        for key, value in (("model_sha256", model), ("study_sha256", study)):
            if hashes.get(key) != fingerprint(value):
                raise ProtectionError("snapshot_hash_mismatch", "Huella incompatible: "+key)
        options = request.get("options", {})
        data = request["input"]
        diagnostics = []
        if cancelled(cancel):
            return envelope("cancelled", model, study, options, {}, variant_sha256=variant)
        if "inventory" in data:
            inventory = data["inventory"]
            if data.get("inventory_sha256") != fingerprint(inventory):
                raise ProtectionError("inventory_hash_mismatch", "Snapshot del inventario alterado")
            stages, coverage, diagnostics = compile_stages(inventory, data.get("mapping", {}),
                time_semantics=data.get("time_semantics"), model=model)
            diagnostics = list(inventory.get("diagnostics", []))+diagnostics
            workspace = new_workspace(stages, source_manifest_sha256=inventory["source_manifest_sha256"], model_sha256=fingerprint(model))
            payload["inventory_coverage"] = coverage
            payload["source_export"] = {"manifest_sha256": inventory["source_manifest_sha256"],
                "status": inventory["manifest"].get("status"), "project": inventory["manifest"].get("project"),
                "dialect": inventory["dialect"], "time_semantics": data.get("time_semantics"),
                "coverage": inventory["coverage"]}
            inputs.extend(inventory["inputs"])
        else:
            workspace = data["workspace"]
            if data.get("workspace_sha256") != fingerprint(workspace):
                raise ProtectionError("workspace_hash_mismatch", "Workspace alterado")
            stages = effective_stages(workspace)
            inputs.append({"path": "snapshot/workspace", "sha256": fingerprint(workspace)})
        if workspace.get("model_sha256") not in (None, fingerprint(model)):
            raise ProtectionError("workspace_model_mismatch", "Workspace asociado a otro modelo")
        pairs = tuple(CoordinationPair(**p) for p in data.get("pairs", workspace.get("pairs", [])))
        stage_coverage = []
        for stage in stages:
            try:
                if stage.enabled:
                    validate_stage(stage)
                missing = []
            except ProtectionError as exc:
                diagnostics.append(exc.diagnostic)
                missing = [exc.diagnostic["code"]]
            stage_coverage.append({"object_id": stage.object_id, "relative_path": stage.relative_path,
                "status": "disabled" if not stage.enabled else "not_calculable" if missing else "calculable",
                "missing": missing})
        payload.update(revision=workspace["revision"], workspace_sha256=fingerprint(workspace), coverage=stage_coverage)
        status = "partial" if diagnostics else "completed"
        if "shortcircuit_result" in data:
            sc = data["shortcircuit_result"]
            if data.get("shortcircuit_sha256") != fingerprint(sc):
                raise ProtectionError("shortcircuit_hash_mismatch", "Resultado CC alterado")
            elements = {e.get("id", e.get("object_id")): e for group in model.values() if isinstance(group, list)
                        for e in group if isinstance(e, dict) and (e.get("id") or e.get("object_id"))}
            for stage in stages:
                if not stage.enabled:
                    continue
                item = elements.get(stage.protected_element_id)
                if item is None or stage.terminal_id not in {item.get("bus_from"), item.get("bus_to")}:
                    raise ProtectionError("mapping_model_mismatch", "Etapa de falla sin elemento/terminal correspondiente en modelo", stage)
            currents, diags = currents_from_shortcircuit(stages, sc, model_sha256=fingerprint(model),
                study_sha256=fingerprint(study), variant_sha256=variant, terminal_mapping=data.get("terminal_mapping", {}))
            diagnostics.extend(diags)
            inputs.append({"path": "snapshot/shortcircuit/"+sc["run_id"], "sha256": fingerprint(sc)})
            payload["fault_currents"] = currents
            payload["fault_times"] = [operation(s, currents[s.object_id]["current_primary_a"])[0]
                                      for s in stages if s.object_id in currents]
            fault_pairs = []
            index = {s.object_id: s for s in stages}
            for pair in pairs:
                if pair.primary_stage_id not in index or pair.backup_stage_id not in index:
                    raise ProtectionError("missing_pair_stage", "Par de falla refiere etapa ausente")
                if pair.primary_stage_id not in currents or pair.backup_stage_id not in currents:
                    fault_pairs.append({"primary_stage_id": pair.primary_stage_id, "backup_stage_id": pair.backup_stage_id,
                                        "covered": False, "reason": "disabled" if not index[pair.primary_stage_id].enabled or not index[pair.backup_stage_id].enabled else "missing_terminal_contribution"})
                else:
                    # La validación de configuración se comparte con el barrido estático.
                    coordinate(stages, (pair,), np.array([1.0]))
                    row, diags = pair_point(index[pair.primary_stage_id], index[pair.backup_stage_id], pair,
                        currents[pair.primary_stage_id]["current_primary_a"], currents[pair.backup_stage_id]["current_primary_a"])
                    diagnostics.extend(diags)
                    fault_pairs.append({"primary_stage_id": pair.primary_stage_id, "backup_stage_id": pair.backup_stage_id, **row})
            payload["fault_pairs"] = fault_pairs
            payload["current_scope"] = "shortcircuit_protected_terminal_contribution_static"
            if diags or any(not p["covered"] for p in fault_pairs):
                status = "partial"
        else:
            if "currents_primary_a" not in data and "current_range_a" not in data:
                raise ProtectionError("explicit_current_grid_required", "Aporte malla primaria o enlace de terminal shortcircuit")
            grid = current_grid(stages, *data["current_range_a"]) if "current_range_a" in data else np.asarray(data["currents_primary_a"], dtype=float)
            # Enriquece una malla explícita para no buscar mínimo/cruces entre solo dos puntos.
            if len(grid) > 1:
                grid = np.unique(np.concatenate((grid, current_grid(stages, float(grid[0]), float(grid[-1])))))
            coordination = coordinate(stages, pairs, grid, cancel=cancel, progress=progress)
            diagnostics.extend(coordination.diagnostics)
            status = coordination.status if coordination.status != "completed" else status
            payload["coordination"] = coordination
            series = []
            for index, stage in enumerate(stages):
                points = []
                for current in grid:
                    if cancelled(cancel):
                        return envelope("cancelled", model, study, options, payload, diagnostics,
                                        variant_sha256=variant, inputs=inputs, stages=stages)
                    row, diag = operation(stage, float(current))
                    points.append((float(current), row["time_s"]))
                    if diag and diag not in diagnostics:
                        diagnostics.append(diag)
                series.append({"object_id": stage.object_id, "relative_path": stage.relative_path,
                    "label": stage.object_id, "current_unit": "A_primary", "points": points})
                if progress:
                    progress({"stage": "curves", "done": index+1, "total": len(stages)})
            payload["series"] = series
            payload["current_scope"] = "explicit_primary_current_grid"
            payload["grid_min_density_per_decade"] = 400 if len(grid) > 1 else None
            if coordination.status == "cancelled":
                status = "cancelled"
        if diagnostics and status == "completed":
            status = "partial"
        if not stages:
            status = "partial"
        result = envelope(status, model, study, options, payload, diagnostics,
            variant_sha256=variant, inputs=inputs, stages=stages)
        if options.get("persist") and status != "cancelled":
            write_result(root, result, historical=options.get("historical", False))
        return result
    except (ProtectionError, KeyError, TypeError, ValueError, OSError) as exc:
        if not isinstance(exc, ProtectionError):
            exc = ProtectionError("invalid_request", str(exc))
        # Los datos inválidos no vuelven a contaminar la envoltura de error.
        try:
            fingerprint((model, study, options))
        except (ProtectionError, TypeError, ValueError):
            model, study, options = {}, {}, {}
        return envelope(exc.status, model, study, options, {}, (exc.diagnostic,), variant_sha256=variant)
