'Campaign support for power-system critical clearing time.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import copy
import json
from pathlib import Path
import time

from .results import fingerprint
from .runner import _run_pm_trial


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def _read_checkpoint(path: Path, signature: str) -> dict:
    if not path.exists():
        return {"schema": "pm-cct-campaign-v1", "signature": signature, "trials": [],
                "status": "running", "started_wall_s": time.time()}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("schema") != "pm-cct-campaign-v1" or value.get("signature") != signature:
        raise _coded_error(ValueError, 'invalid_campaign_data', 'CCT checkpoint belongs to another configuration')
    if not isinstance(value.get("trials"), list):
        raise _coded_error(ValueError, 'invalid_campaign_data', 'CCT checkpoint has no trial list')
    return value


def _record(duration_s: float, result: dict | None, elapsed_s: float) -> dict:
    if result is None:
        return {"clearing_s": duration_s, "classification": "not_evaluable", "stable": None,
                "reason": 'Cancellation requested between runs', "wall_s": elapsed_s}
    return {**result, "wall_s": elapsed_s}


def _summary(trials: list[dict], resolution_s: float) -> dict:
    stable = [row for row in trials if row.get("classification") == "stable"]
    unstable = [row for row in trials if row.get("classification") == "unstable"]
    no_eval = [row for row in trials if row.get("classification") == "not_evaluable"]
    ordered = sorted(trials, key=lambda row: row["clearing_s"])
    bracket = None
    for left, right in zip(ordered, ordered[1:]):
        if left.get("classification") == "stable" and right.get("classification") == "unstable":
            bracket = (left, right)
            break
    return {"stable_trials": stable, "unstable_trials": unstable,
            "not_evaluable_trials": no_eval, "bracket": bracket,
            "resolution_s": resolution_s}


def run_campaign(root, model, study, request, options, *, checkpoint_path,
                      expansion_s=(.1, .2, .4, .8), cancel=None, progress=None):
    'Expand clearing-time trials, then bisect a bracketed CCT frontier. Write checkpoints atomically. Resume only compatible completed trials and retain numerical failures without reclassifying them.'
    root = Path(root).resolve()
    checkpoint_path = Path(checkpoint_path).resolve()
    if not checkpoint_path.is_relative_to(root):
        raise _coded_error(ValueError, 'invalid_campaign_data', 'CCT checkpoint must remain inside the working directory')
    durations = tuple(float(value) for value in expansion_s)
    if not durations or any(value <= 0 for value in durations) or tuple(sorted(durations)) != durations:
        raise _coded_error(ValueError, 'invalid_campaign_data', 'CCT expansion times must be positive and increasing')
    signature = fingerprint({"model": model, "study": study, "request": request.__dict__,
                             "options": options.__dict__, "expansion_s": durations})
    checkpoint = _read_checkpoint(checkpoint_path, signature)
    completed = {round(float(row["clearing_s"]), 12) for row in checkpoint["trials"]}

    def save(status="running", reason=None):
        checkpoint["status"] = status
        checkpoint["reason"] = reason
        checkpoint["updated_wall_s"] = time.time()
        checkpoint["summary"] = _summary(checkpoint["trials"], options.resolution_s)
        _write_json(checkpoint_path, checkpoint)

    def evaluate(duration_s: float, phase: str):
        key = round(duration_s, 12)
        if key in completed:
            return next(row for row in checkpoint["trials"] if round(float(row["clearing_s"]), 12) == key)
        if cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()):
            save("partial", 'Cancellation requested between runs')
            return None
        if progress:
            progress({"phase": phase, "completed": len(checkpoint["trials"]), "total": None,
                      "clearing_s": duration_s, "checkpoint": str(checkpoint_path.relative_to(root))})
        started = time.monotonic()
        checkpoint["active_trial"] = {"phase": phase, "clearing_s": duration_s,
                                      "started_wall_s": time.time(), "simulated_time_s": None}
        save()
        last_persist_s = [started]
        def engine_progress(*values):
            if len(values) >= 2:
                checkpoint["active_trial"]["simulated_time_s"] = float(values[0])
                checkpoint["active_trial"]["end_s"] = float(values[1])
            if time.monotonic() - last_persist_s[0] >= 30.0:
                save()
                last_persist_s[0] = time.monotonic()
            if progress:
                progress(*values)
        try:
            raw = _run_pm_trial(root, model, study, request, options, duration_s, cancel, engine_progress)
            row = _record(duration_s, raw, time.monotonic() - started)
        except Exception as exc:
            row = {"clearing_s": duration_s, "classification": "not_evaluable", "stable": None,
                   "reason": f'{type(exc).__name__}: {exc}', "trajectory": [],
                   "wall_s": time.monotonic() - started}
        checkpoint.pop("active_trial", None)
        checkpoint["trials"].append(row)
        completed.add(key)
        save()
        return row

    for duration_s in durations:
        if evaluate(duration_s, "expansion") is None:
            return checkpoint
    state = _summary(checkpoint["trials"], options.resolution_s)
    if state["not_evaluable_trials"]:
        save("partial", 'Expansion contains unevaluable trials')
        return checkpoint
    bracket = state["bracket"]
    if bracket is None:
        save("partial", 'Expansion found no stable/unstable bracket')
        return checkpoint
    lower, upper = bracket[0]["clearing_s"], bracket[1]["clearing_s"]
    while upper - lower > options.resolution_s:
        middle = (lower + upper) / 2.0
        row = evaluate(middle, "bisection")
        if row is None:
            return checkpoint
        if row["classification"] == "not_evaluable":
            save("partial", 'Bisection stopped after an unevaluable trial')
            return checkpoint
        if row["classification"] == "stable":
            lower = middle
        else:
            upper = middle
    checkpoint["cct_interval_s"] = {"stable_lower_s": lower, "unstable_upper_s": upper,
                                      "resolution_ms": (upper-lower)*1000.0}
    save("completed")
    return checkpoint


