"""Independent study campaigns with deterministic, process-safe execution."""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import multiprocessing
import os
import time
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator, Mapping


_WORKER_MODEL: dict[str, Any] | None = None
_WORKER_STUDY: str | None = None
_WORKER_OPTIONS: dict[str, Any] | None = None
_WORKER_PREPARED: Any = None
_FINAL_STATUSES = frozenset({"completed", "limit_violation", "not_converged", "failed", "invalid_input", "unsupported", "timeout"})
_CSV_COLUMNS = ("id", "status", "iterations", "residual", "violations", "max_loading_pct", "v_min_pu", "v_max_pu", "unserved_mw", "duration_s", "seed", "detail_path", "error_code")


@dataclass(frozen=True)
class Scenario:
    """Declarative, stable input for one independent study execution.

    ``out_of_service`` contains element identifiers. Power and reactive-power
    maps accept generator or load identifiers. ``overrides`` is retained as a
    low-level compatibility escape hatch for supported engine deltas.
    """

    id: str
    out_of_service: tuple[str, ...] = ()
    p_mw: Mapping[str, float] = field(default_factory=dict)
    q_mvar: Mapping[str, float] = field(default_factory=dict)
    setpoints: Mapping[str, float] = field(default_factory=dict)
    load_multipliers: Mapping[str, float] = field(default_factory=dict)
    seed: int | None = None
    overrides: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("batch_invalid_scenario_id: scenario id must be a non-empty string")
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
            raise ValueError("batch_invalid_scenario_seed: seed must be an integer or null")
        object.__setattr__(self, "out_of_service", tuple(str(value) for value in self.out_of_service))
        for name in ("p_mw", "q_mvar", "setpoints", "load_multipliers", "overrides"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ValueError(f"batch_invalid_scenario: {name} must be a mapping")

    def to_dict(self) -> dict[str, Any]:
        """Return an immutable-input-compatible JSON dictionary."""
        return _json_value(asdict(self))


@dataclass(frozen=True)
class BatchOptions:
    """Execution controls for :func:`run_batch`."""

    workers: int | None = None
    start_method: str = "spawn"
    chunk_size: int = 32
    result_detail: str = "summary"
    on_failure: str = "continue"
    resume: bool = False
    seed: int | None = None
    blas_threads: int = 1
    timeout_s: float | None = None

    def __post_init__(self) -> None:
        workers = self.workers if self.workers is not None else max(1, (os.cpu_count() or 1))
        if isinstance(workers, bool) or not isinstance(workers, int) or workers < 1:
            raise ValueError("batch_invalid_workers: workers must be a positive integer")
        if self.start_method != "spawn":
            raise ValueError("batch_invalid_start_method: only spawn is supported")
        if isinstance(self.chunk_size, bool) or not isinstance(self.chunk_size, int) or self.chunk_size < 1:
            raise ValueError("batch_invalid_chunk_size: chunk_size must be a positive integer")
        if self.result_detail not in {"summary", "full"}:
            raise ValueError("batch_invalid_result_detail: result_detail must be summary or full")
        if self.on_failure not in {"continue", "stop"}:
            raise ValueError("batch_invalid_failure_policy: on_failure must be continue or stop")
        if self.seed is not None and (isinstance(self.seed, bool) or not isinstance(self.seed, int)):
            raise ValueError("batch_invalid_seed: seed must be an integer or null")
        if isinstance(self.blas_threads, bool) or not isinstance(self.blas_threads, int) or self.blas_threads < 1:
            raise ValueError("batch_invalid_blas_threads: blas_threads must be a positive integer")
        if self.timeout_s is not None and (not isinstance(self.timeout_s, (int, float)) or self.timeout_s <= 0 or not math.isfinite(self.timeout_s)):
            raise ValueError("batch_invalid_timeout: timeout_s must be a positive finite number or null")
        object.__setattr__(self, "workers", workers)


@dataclass(frozen=True)
class BatchResult:
    """Campaign summary with JSON-safe records and optional detail references."""

    records: tuple[dict[str, Any], ...]
    model_sha256: str
    study: str
    study_sha256: str
    output: str | None = None
    details: Mapping[str, str] = field(default_factory=dict)

    @property
    def table(self) -> tuple[dict[str, Any], ...]:
        """Return the ordered campaign summary table."""
        return self.records

    def to_records(self) -> list[dict[str, Any]]:
        """Return a deep JSON-safe copy of the summary table."""
        return copy.deepcopy(list(self.records))

    def to_csv(self, path: str | Path) -> Path:
        """Write the summary table to a UTF-8 CSV file."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(self.records)
        return target

    def worst(self, by: str = "max_loading_pct") -> dict[str, Any] | None:
        """Return the worst completed row by descending numeric metric."""
        values = [row for row in self.records if isinstance(row.get(by), (int, float)) and not isinstance(row.get(by), bool)]
        return copy.deepcopy(max(values, key=lambda row: (float(row[by]), row["id"]))) if values else None


def monte_carlo_load(model: dict[str, Any], n: int, distribution: Mapping[str, Any], seed: int | None = None) -> Iterator[Scenario]:
    """Yield deterministic load-multiplier scenarios without changing ``model``.

    Distribution supports ``normal`` and ``uniform`` forms, with optional
    ``load_ids``. A normal distribution uses ``mean`` and ``std``. A uniform
    distribution uses ``low`` and ``high``.
    """
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise ValueError("batch_invalid_monte_carlo_count: n must be a non-negative integer")
    if not isinstance(distribution, Mapping):
        raise ValueError("batch_invalid_distribution: distribution must be a mapping")
    load_ids = tuple(str(value) for value in distribution.get("load_ids", [item["id"] for item in model.get("loads", [])]))
    kind = str(distribution.get("kind", "normal")).lower()
    if kind not in {"normal", "uniform"}:
        raise ValueError("batch_invalid_distribution: kind must be normal or uniform")
    import numpy as np

    root = np.random.SeedSequence(seed)
    for number, child in enumerate(root.spawn(n)):
        generator = np.random.default_rng(child)
        if kind == "normal":
            mean, std = float(distribution.get("mean", 1.0)), float(distribution.get("std", 0.05))
            values = generator.normal(mean, std, len(load_ids))
        else:
            low, high = float(distribution.get("low", 0.9)), float(distribution.get("high", 1.1))
            values = generator.uniform(low, high, len(load_ids))
        yield Scenario(id=f"mc-{number:06d}", load_multipliers=dict(zip(load_ids, map(float, values))),
                       seed=_seed_from_sequence(child))


def run_batch(model: dict[str, Any], study: str, scenarios: Iterable[Scenario], *, study_options: Mapping[str, Any] | None = None, options: BatchOptions | None = None, output: str | Path | None = None, progress: Callable[[dict[str, Any]], Any] | None = None, cancel: Any = None) -> BatchResult:
    """Run independent load-flow or contingency scenarios in spawned workers."""
    if hasattr(model, "to_model"):
        model = model.to_model()
    if not isinstance(model, dict):
        raise TypeError("batch_invalid_model: model must be a dictionary or Network")
    study = str(study).lower()
    if study not in {"loadflow", "contingency"}:
        raise ValueError("batch_unsupported_study: supported studies are loadflow and contingency")
    settings = options or BatchOptions()
    if not isinstance(settings, BatchOptions):
        raise TypeError("batch_invalid_options: options must be BatchOptions or null")
    study_options = copy.deepcopy(dict(study_options or {}))
    scenario_list = [_coerce_scenario(value) for value in scenarios]
    identifiers = [scenario.id for scenario in scenario_list]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("batch_duplicate_scenario_id: scenario ids must be unique")
    _strict_json(model)
    _strict_json(study_options)
    destination = Path(output) if output is not None else None
    if destination is not None:
        destination.mkdir(parents=True, exist_ok=True)
        (destination / "checkpoints").mkdir(exist_ok=True)
        if settings.result_detail == "full":
            (destination / "details").mkdir(exist_ok=True)
    model_hash = _fingerprint(model)
    study_hash = _fingerprint({"study": study, "options": study_options})
    records: dict[str, dict[str, Any]] = {}
    detail_paths: dict[str, str] = {}
    pending: list[Scenario] = []
    for scenario in scenario_list:
        key = _scenario_key(model_hash, study_hash, scenario)
        stored = _read_checkpoint(destination, key) if settings.resume else None
        if stored is not None:
            records[scenario.id] = stored["record"]
            if stored.get("detail_path"):
                detail_paths[scenario.id] = stored["detail_path"]
        else:
            pending.append(scenario)
    _emit(progress, "batch_started", len(records), len(scenario_list))
    if pending and not _cancelled(cancel):
        if settings.workers == 1:
            prepared = _compile_parent_model(model)
            for scenario in pending:
                if _cancelled(cancel):
                    break
                result = _run_one_parent(model, study, study_options, scenario, settings, prepared)
                row, reference = _store_result(result, destination, model_hash, study_hash, scenario, settings)
                records[scenario.id] = row
                if reference:
                    detail_paths[scenario.id] = reference
                _emit(progress, "scenario_completed", len(records), len(scenario_list), scenario.id)
                if settings.on_failure == "stop" and row["status"] not in {"completed", "limit_violation"}:
                    break
        else:
            _run_parallel(model, study, study_options, pending, settings, destination, model_hash, study_hash,
                          records, detail_paths, progress, cancel, len(scenario_list))
    for scenario in scenario_list:
        if scenario.id not in records:
            records[scenario.id] = _cancelled_record(scenario.id, "cancelled" if _cancelled(cancel) else "skipped")
    ordered = tuple(records[scenario.id] for scenario in scenario_list)
    result = BatchResult(ordered, model_hash, study, study_hash, str(destination) if destination else None, detail_paths)
    if destination is not None:
        _atomic_json(destination / "batch_summary.json", {"model_sha256": model_hash, "study": study,
                     "study_sha256": study_hash, "records": result.to_records(), "details": detail_paths})
        result.to_csv(destination / "summary.csv")
    _emit(progress, "batch_finished", len(ordered), len(ordered))
    return result


def _run_parallel(model: dict[str, Any], study: str, study_options: dict[str, Any], pending: list[Scenario], settings: BatchOptions, destination: Path | None, model_hash: str, study_hash: str, records: dict[str, dict[str, Any]], detail_paths: dict[str, str], progress: Callable[[dict[str, Any]], Any] | None, cancel: Any, total: int) -> None:
    context = multiprocessing.get_context(settings.start_method)
    chunks = [pending[index:index + settings.chunk_size] for index in range(0, len(pending), settings.chunk_size)]
    next_chunk, inflight, stop = 0, {}, False
    with ProcessPoolExecutor(max_workers=settings.workers, mp_context=context, initializer=_worker_initializer,
                             initargs=(model, study, study_options, settings.blas_threads)) as executor:
        while (next_chunk < len(chunks) or inflight) and not _cancelled(cancel):
            while not stop and next_chunk < len(chunks) and len(inflight) < settings.workers * 2:
                future = executor.submit(_worker_chunk, chunks[next_chunk], asdict(settings))
                inflight[future] = (time.monotonic(), chunks[next_chunk])
                next_chunk += 1
            if not inflight:
                break
            done, _ = wait(inflight, timeout=0.05, return_when=FIRST_COMPLETED)
            now = time.monotonic()
            for future, (started, chunk) in list(inflight.items()):
                if settings.timeout_s is not None and now - started > settings.timeout_s:
                    del inflight[future]
                    future.cancel()
                    for scenario in chunk:
                        if scenario.id not in records:
                            row, reference = _store_result(_failure_result(scenario, "timeout", "batch_timeout"), destination, model_hash, study_hash, scenario, settings)
                            records[scenario.id] = row
                            if reference:
                                detail_paths[scenario.id] = reference
            for future in done:
                if future not in inflight:
                    continue
                _, chunk = inflight.pop(future)
                try:
                    results = future.result()
                except Exception as exc:
                    results = [_failure_result(scenario, "failed", "batch_worker_failure", exc) for scenario in chunk]
                for scenario, result in zip(chunk, results):
                    row, reference = _store_result(result, destination, model_hash, study_hash, scenario, settings)
                    records[scenario.id] = row
                    if reference:
                        detail_paths[scenario.id] = reference
                    _emit(progress, "scenario_completed", len(records), total, scenario.id)
                    if settings.on_failure == "stop" and row["status"] not in {"completed", "limit_violation"}:
                        stop = True
            if stop:
                for future in inflight:
                    future.cancel()
                break


def _worker_initializer(model: dict[str, Any], study: str, study_options: dict[str, Any], blas_threads: int) -> None:
    global _WORKER_MODEL, _WORKER_OPTIONS, _WORKER_PREPARED, _WORKER_STUDY
    for name in ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ[name] = str(blas_threads)
    _WORKER_MODEL, _WORKER_STUDY, _WORKER_OPTIONS = model, study, study_options
    try:
        from power_machine.compiled import compile_network
        _WORKER_PREPARED = compile_network(model)
    except (ImportError, AttributeError, TypeError, ValueError):
        _WORKER_PREPARED = None


def _worker_chunk(scenarios: list[Scenario], options: dict[str, Any]) -> list[dict[str, Any]]:
    settings = BatchOptions(**options)
    assert _WORKER_MODEL is not None and _WORKER_STUDY is not None and _WORKER_OPTIONS is not None
    return [_run_one(_WORKER_MODEL, _WORKER_STUDY, _WORKER_OPTIONS, scenario, settings, _WORKER_PREPARED) for scenario in scenarios]


def _compile_parent_model(model: dict[str, Any]) -> Any:
    """Use the same prepared-network path in serial and spawned executions."""
    try:
        from power_machine.compiled import compile_network
        return compile_network(model)
    except (ImportError, AttributeError, TypeError, ValueError):
        return None


def _run_one_parent(model: dict[str, Any], study: str, study_options: dict[str, Any], scenario: Scenario, settings: BatchOptions, prepared: Any) -> dict[str, Any]:
    return _run_one(model, study, study_options, scenario, settings, prepared)


def _run_one(model: dict[str, Any], study: str, study_options: dict[str, Any], scenario: Scenario, settings: BatchOptions, prepared: Any) -> dict[str, Any]:
    started = time.perf_counter()
    seed = _scenario_seed(settings.seed, scenario)
    try:
        materialized = _apply_scenario(model, scenario)
        if study == "loadflow":
            raw = _run_loadflow(materialized, scenario, study_options, prepared)
            record = _loadflow_record(raw, materialized)
        else:
            from power_machine.contingency import ContingencyOptions
            from power_machine.contingency.metrics import collect_metrics, normalize_loadflow
            options = ContingencyOptions(**study_options)
            loadflow = _run_loadflow(materialized, scenario, {"q_limits": options.q_limits}, prepared)
            normalized = normalize_loadflow(loadflow, materialized)
            metrics, violations, diagnostics, status = collect_metrics(materialized, normalized, options)
            raw = {"status": status, "payload": {"case": {"case_id": scenario.id, "status": status,
                   "metrics": metrics, "violations": list(violations), "loadflow": normalized},
                   "diagnostics": diagnostics}}
            record = _contingency_record(raw)
        return {"record": {"id": scenario.id, **record, "seed": seed,
                            "duration_s": time.perf_counter() - started, "error_code": None},
                "detail": raw}
    except Exception as exc:
        return _failure_result(scenario, "failed", "batch_scenario_failure", exc, seed=seed,
                               duration_s=time.perf_counter() - started)


def _run_loadflow(model: dict[str, Any], scenario: Scenario, options: dict[str, Any], prepared: Any) -> dict[str, Any]:
    if prepared is not None:
        return prepared.run_loadflow(scenario=_prepared_delta(model, scenario), options=copy.deepcopy(options))
    from power_machine.loadflow import run_loadflow
    return run_loadflow(model, options=copy.deepcopy(options))


def _prepared_delta(model: dict[str, Any], scenario: Scenario) -> dict[str, dict[str, Any]]:
    """Translate the public declarative fields to compiled-network deltas."""
    index = {item["id"]: item for group in ("loads", "gens") for item in model.get(group, [])}
    delta: dict[str, dict[str, Any]] = {"outserv": {item: 1 for item in scenario.out_of_service}}
    for name, values in scenario.overrides.items():
        if name not in {"outserv", "closed"}:
            raise ValueError(f"batch_unsupported_override: {name}")
        delta.setdefault(name, {}).update(values)
    for name, values in (("p_mw", scenario.p_mw), ("q_mvar", scenario.q_mvar)):
        for identifier, value in values.items():
            item = index.get(identifier)
            if item is None:
                raise ValueError(f"batch_unknown_element: {identifier}")
            if item.get("class") == "ElmLod":
                target = "load_p_mw" if name == "p_mw" else "load_q_mvar"
            else:
                target = "gen_p_mw" if name == "p_mw" else "gen_q_mvar"
            delta.setdefault(target, {})[identifier] = value
    if scenario.setpoints:
        delta["v_set_pu"] = dict(scenario.setpoints)
    if scenario.load_multipliers:
        delta["load_scale"] = dict(scenario.load_multipliers)
    return {name: values for name, values in delta.items() if values}


def _apply_scenario(model: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    result = copy.deepcopy(model)
    index = {item["id"]: item for group in ("buses", "switches", "branches", "branches3", "shunts", "loads", "gens") for item in result.get(group, [])}
    def item_for(identifier: str) -> dict[str, Any]:
        try:
            return index[str(identifier)]
        except KeyError as exc:
            raise ValueError(f"batch_unknown_element: {identifier}") from exc
    for identifier in scenario.out_of_service:
        item_for(identifier)["outserv"] = 1
    for name, changes in scenario.overrides.items():
        for identifier, value in changes.items():
            item = item_for(identifier)
            if name == "outserv": item["outserv"] = int(bool(value))
            elif name == "closed": item["closed"] = bool(value)
            else: raise ValueError(f"batch_unsupported_override: {name}")
    for identifier, value in scenario.p_mw.items():
        item = item_for(identifier)
        _set_power(item, "p", float(value))
    for identifier, value in scenario.q_mvar.items():
        item = item_for(identifier)
        _set_power(item, "q", float(value))
    for identifier, value in scenario.setpoints.items():
        item = item_for(identifier)
        if item.get("class") not in {"ElmSym", "ElmGenstat", "ElmPvsys"}:
            raise ValueError(f"batch_invalid_setpoint_target: {identifier}")
        item["v_set_pu"] = float(value); item.setdefault("params", {})["usetp"] = float(value)
    for identifier, value in scenario.load_multipliers.items():
        item = item_for(identifier)
        if item.get("class") != "ElmLod":
            raise ValueError(f"batch_invalid_load_target: {identifier}")
        multiplier = float(value)
        if not math.isfinite(multiplier) or multiplier < 0:
            raise ValueError(f"batch_invalid_load_multiplier: {identifier}")
        item["scale"] = multiplier; item.setdefault("params", {})["scale0"] = multiplier
    return result


def _set_power(item: dict[str, Any], axis: str, value: float) -> None:
    if not math.isfinite(value):
        raise ValueError("batch_nonfinite_power: power changes must be finite")
    if item.get("class") == "ElmLod":
        field, parameter = ("p_mw", "plini") if axis == "p" else ("q_mvar", "qlini")
        item[field] = value; item.setdefault("params", {})[parameter] = value
    elif item.get("class") in {"ElmSym", "ElmGenstat", "ElmPvsys"}:
        field, parameter = ("p_mw", "pgini") if axis == "p" else ("q_mvar", "qgini")
        item[field] = value
        scale, units = float(item.get("params", {}).get("scale0", 1)), float(item.get("params", {}).get("ngnum", 1))
        divisor = 1000 if item.get("class") == "ElmPvsys" else 1
        item.setdefault("params", {})[parameter] = value * divisor / (scale * units)
    else:
        raise ValueError(f"batch_invalid_power_target: {item.get('id')}")


def _loadflow_record(raw: dict[str, Any], model: dict[str, Any]) -> dict[str, Any]:
    from power_machine.loading import branch_loading
    buses = [float(row["u_pu"]) for row in raw.get("buses", []) if isinstance(row.get("u_pu"), (int, float))]
    loads = branch_loading(model, raw)
    loading = [row["loading_pct"] for row in loads.values() if row.get("loading_pct") is not None]
    return {"status": "completed" if raw.get("converged") else "not_converged", "iterations": raw.get("iterations"),
            "residual": raw.get("max_mismatch_mva"), "violations": 0, "max_loading_pct": max(loading, default=None),
            "v_min_pu": min(buses, default=None), "v_max_pu": max(buses, default=None), "unserved_mw": None}


def _contingency_record(raw: dict[str, Any]) -> dict[str, Any]:
    payload = raw.get("payload", {})
    cases = ([payload.get("case")] if payload.get("case") else
             [payload.get("base")] + list(payload.get("cases", [])))
    cases = [case for case in cases if isinstance(case, dict)]
    metrics = [case.get("metrics", {}) for case in cases]
    finite = lambda values: [float(value) for value in values if isinstance(value, (int, float)) and math.isfinite(value)]
    return {"status": raw.get("status", "failed"), "iterations": None, "residual": None,
            "violations": sum(len(case.get("violations", [])) for case in cases),
            "max_loading_pct": max(finite(metric.get("max_loading_pct") for metric in metrics), default=None),
            "v_min_pu": min(finite(metric.get("min_u_pu") for metric in metrics), default=None),
            "v_max_pu": max(finite(metric.get("max_u_pu") for metric in metrics), default=None),
            "unserved_mw": max(finite(metric.get("unsupplied_p_mw") for metric in metrics), default=None)}


def _failure_result(scenario: Scenario, status: str, code: str, exc: Exception | None = None, *, seed: int | None = None, duration_s: float = 0.0) -> dict[str, Any]:
    return {"record": {"id": scenario.id, "status": status, "iterations": None, "residual": None, "violations": 0,
                        "max_loading_pct": None, "v_min_pu": None, "v_max_pu": None, "unserved_mw": None,
                        "duration_s": duration_s, "seed": seed, "error_code": code},
            "detail": {"code": code, "message": f"{type(exc).__name__}: {exc}" if exc else code}}


def _store_result(result: dict[str, Any], destination: Path | None, model_hash: str, study_hash: str, scenario: Scenario, settings: BatchOptions) -> tuple[dict[str, Any], str | None]:
    row, detail = _json_value(result["record"]), _json_value(result["detail"])
    reference = None
    key = _scenario_key(model_hash, study_hash, scenario)
    if destination is not None:
        if settings.result_detail == "full":
            detail_file = destination / "details" / f"{_safe_name(scenario.id)}-{key[:12]}.json"
            _atomic_json(detail_file, detail)
            reference = str(detail_file)
            row["detail_path"] = reference
        else:
            row["detail_path"] = None
        _atomic_json(destination / "checkpoints" / f"{key}.json", {"record": row, "detail_path": reference})
    else:
        row["detail_path"] = None
    return row, reference


def _read_checkpoint(destination: Path | None, key: str) -> dict[str, Any] | None:
    if destination is None:
        return None
    path = destination / "checkpoints" / f"{key}.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        _strict_json(value)
        return value if isinstance(value.get("record"), dict) else None
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _cancelled(cancel: Any) -> bool:
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _cancelled_record(identifier: str, status: str) -> dict[str, Any]:
    return {"id": identifier, "status": status, "iterations": None, "residual": None, "violations": 0,
            "max_loading_pct": None, "v_min_pu": None, "v_max_pu": None, "unserved_mw": None,
            "duration_s": 0.0, "seed": None, "detail_path": None, "error_code": f"batch_{status}"}


def _coerce_scenario(value: Scenario | Mapping[str, Any]) -> Scenario:
    if isinstance(value, Scenario): return value
    if isinstance(value, Mapping): return Scenario(**value)
    raise TypeError("batch_invalid_scenario: scenarios must contain Scenario instances or mappings")


def _scenario_seed(root_seed: int | None, scenario: Scenario) -> int:
    import numpy as np
    entropy = scenario.seed if scenario.seed is not None else (0 if root_seed is None else root_seed)
    digest = hashlib.sha256(scenario.id.encode("utf-8")).digest()
    sequence = np.random.SeedSequence([int(entropy), *[int.from_bytes(digest[offset:offset + 4], "big") for offset in range(0, 16, 4)]])
    return _seed_from_sequence(sequence)


def _seed_from_sequence(sequence: Any) -> int:
    return int(sequence.generate_state(1, dtype="uint64")[0])


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _scenario_key(model_hash: str, study_hash: str, scenario: Scenario) -> str:
    return _fingerprint({"model": model_hash, "study": study_hash, "scenario": scenario.to_dict()})


def _json_value(value: Any) -> Any:
    if is_dataclass(value): value = asdict(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)): value = value.tolist()
    elif hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)): value = value.item()
    if isinstance(value, Mapping): return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)): return [_json_value(item) for item in value]
    if isinstance(value, float): return value if math.isfinite(value) else None
    if isinstance(value, Path): return str(value)
    if isinstance(value, (str, int, bool)) or value is None: return value
    return str(value)


def _strict_json(value: Any) -> None:
    json.dumps(value, ensure_ascii=False, allow_nan=False)


def _atomic_json(path: Path, value: Any) -> None:
    _strict_json(value)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "_" for character in value)[:80] or "scenario"


def _emit(callback: Callable[[dict[str, Any]], Any] | None, code: str, completed: int, total: int, scenario_id: str | None = None) -> None:
    if callback:
        callback({"code": code, "completed": completed, "total": total, "fraction": completed / total if total else 1.0,
                  "scenario_id": scenario_id})
