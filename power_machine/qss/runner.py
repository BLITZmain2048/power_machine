"""Ejecución secuencial QSS con checkpoints anteriores a cada instante."""
from __future__ import annotations

import json
import math
import time
from dataclasses import asdict
from pathlib import Path

from ..loadflow import run_loadflow
from ..lf_status import energized_status
from ..benchmarks.ieee39_pf import LDF_OPTIONS
from .dispatch import dispatch_base, plan_dispatch
from .series import digest, model_digest


def _write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8")


def source_hashes():
    """Huella del paquete y del núcleo LF reutilizado, sin leer reportes."""
    root = Path(__file__).resolve().parents[2]
    paths = sorted(Path(__file__).parent.glob("*.py"))
    paths += [root / "power_machine" / (name + ".py") for name in
              ("loadflow", "lf_status", "elements", "results", "topology", "overrides")]
    return {str(path.relative_to(root)): digest(path.read_bytes()) for path in paths}


def _validate(model, series):
    if not series.steps:
        raise ValueError("Serie sin instantes")
    ids = set()
    for group in ("buses", "branches", "branches3", "switches", "loads", "gens", "shunts"):
        for item in model.get(group, []):
            if item["id"] in ids:
                raise ValueError(f"ID duplicado: {item['id']}")
            ids.add(item["id"])
            if item.get("object_id") != item["id"] or not item.get("relative_path"):
                raise ValueError(f"Falta object_id/relative_path coherente: {item['id']}")
    load_ids = {load["id"] for load in model["loads"]}
    previous_t_s = -math.inf
    for step in series.steps:
        if not math.isfinite(step.t_s) or step.t_s <= previous_t_s:
            raise ValueError("t_s debe ser finito y estrictamente creciente")
        if set(step.factors) != load_ids or any(not math.isfinite(x) for x in step.factors.values()):
            raise ValueError("Factores ausentes, no finitos o IDs de carga incompatibles")
        previous_t_s = step.t_s
    expected = series.provenance.get("model_semantic_sha256")
    if expected is not None and expected != model_digest(model):
        raise ValueError("Modelo cambiado después de leer la serie")


def run_series(model, series, output, *, rule="reference", warm_start=True,
               policy="continue", resume_from=None, start_index=0, end_index=None,
               options=None):
    """Ejecuta [start_index, end_index), sin sobrescribir un destino existente.

    warm_start usa el resultado anterior, incluso si contiene ausencias.
    Tras fallo, las pérdidas para despacho son las últimas resueltas y su
    índice se informa. Las salidas del fallo nunca se sustituyen por ellas.
    La reanudación recalcula start_index con el checkpoint previo guardado.
    El tiempo de ejecución se mide en segundos y no participa en identidad.
    """
    started = time.perf_counter()
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Destino existente, se requiere uno nuevo: {output}")
    if policy not in ("continue", "stop") or rule not in ("reference", "proportional"):
        raise ValueError("Política o despacho desconocidos")
    end_index = len(series.steps) if end_index is None else end_index
    if not 0 <= start_index < end_index <= len(series.steps):
        raise ValueError("Intervalo de índices inválido")
    if start_index and resume_from is None:
        raise ValueError("Un índice inicial distinto de cero requiere resume_from")
    _validate(model, series)
    lf_options = {**LDF_OPTIONS, **(options or {}), "warm_start": bool(warm_start)}
    if lf_options.get("q_limits") is not False or lf_options.get("missing") != "error":
        raise ValueError("QSS requiere q_limits=False y missing='error'")
    if "base_result" in lf_options:
        raise ValueError("base_result lo administra el encadenamiento QSS")
    hashes = source_hashes()
    config = {"rule": rule, "warm_start": bool(warm_start), "policy": policy,
              "lf_options": lf_options, "model_sha256": model_digest(model),
              "series": [asdict(step) for step in series.steps], "source_sha256": hashes}
    fingerprint = digest(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False).encode("utf-8"))
    if resume_from is not None:
        source = Path(resume_from) / "checkpoints" / f"{start_index:06d}.json"
        state = json.loads(source.read_text(encoding="utf-8"))
        state_sha256 = state.pop("state_sha256", None)
        if state_sha256 != model_digest(state):
            raise ValueError("SHA256 del estado del checkpoint no coincide")
        if state["fingerprint"] != fingerprint or state["index"] != start_index:
            raise ValueError("Checkpoint incompatible con modelo, serie, código u opciones")
        base_result = state["base_result"]
        previous_result = state["previous_result"]
        previous_loss_p_mw = state["previous_loss_p_mw"]
        loss_source_index = state["loss_source_index"]
    else:
        base_result = run_loadflow(model, options={**lf_options, "warm_start": False})
        previous_result = base_result
        previous_loss_p_mw = base_result["totals"]["branch_loss_p_mw"]
        loss_source_index = -1
    base = dispatch_base(model, base_result)
    output.mkdir(parents=True, exist_ok=False)
    (output / "checkpoints").mkdir()
    metadata = {"schema": "pm-qss-v1", "validation_status": "sin validar contra PF",
                "series_name": series.name, "fingerprint": fingerprint,
                "rule": rule, "policy": policy, "warm_start": bool(warm_start),
                "lf_options": lf_options, "provenance": series.provenance,
                "source_sha256": hashes, "dispatch_base": asdict(base),
                "start_index": start_index, "end_index_exclusive": end_index,
                "resume_from": str(resume_from) if resume_from is not None else None}
    _write_json(output / "run.json", metadata)
    records = []
    with (output / "step_results.jsonl").open("w", encoding="utf-8") as handle:
        for index in range(start_index, end_index):
            step = series.steps[index]
            state = {
                "fingerprint": fingerprint, "index": index, "base_result": base_result,
                "previous_result": previous_result, "previous_loss_p_mw": previous_loss_p_mw,
                "loss_source_index": loss_source_index}
            _write_json(output / "checkpoints" / f"{index:06d}.json",
                        {**state, "state_sha256": model_digest(state)})
            plan = plan_dispatch(model, step, base, previous_loss_p_mw, rule)
            tick = time.perf_counter()
            result = run_loadflow(model, plan["scenario"],
                                  {**lf_options, "base_result": previous_result})
            elapsed_s = time.perf_counter() - tick
            island_status = energized_status(result)
            voltages = [b["u_pu"] for b in result["buses"] if b["u_pu"] is not None]
            collapse = bool(voltages and min(voltages) < 0.5)
            failure = not island_status["ok"] or collapse
            record = {"index": index, "timestamp_iso": step.timestamp_iso, "t_s": step.t_s,
                      "factors": step.factors, "plan": plan, "result": result,
                      "energized_status": island_status,
                      "loss_source_index": loss_source_index, "elapsed_s": elapsed_s,
                      "collapse": collapse, "failure": failure,
                      "accepted": island_status["ok"] and not collapse
                      and result["max_mismatch_mva"] <= 1e-6}
            records.append(record)
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            previous_result = result
            if island_status["ok"]:
                previous_loss_p_mw = result["totals"]["branch_loss_p_mw"]
                loss_source_index = index
            if failure and policy == "stop":
                break
    from .report import write_report
    summary = write_report(output, model, records, metadata,
                           elapsed_s=time.perf_counter() - started)
    return {"records": records, "base_result": base_result, "summary": summary,
            "output": output, "metadata": metadata}
