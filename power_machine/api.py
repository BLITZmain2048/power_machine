"""Public Python API for Power Machine power-system studies."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, field, is_dataclass
import csv
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable
from uuid import uuid4


_GROUPS = ("buses", "branches", "branches3", "switches", "gens", "loads", "shunts", "controllers")
_CASES = {"ieee39": Path(__file__).with_name("cases") / "ieee39" / "model.json"}


class PowerMachineError(Exception):
    """Base exception with a stable error code and a suggested recovery action."""

    code = "power_machine_error"

    def __init__(self, message: str, *, field: str | None = None, action: str | None = None) -> None:
        super().__init__(message)
        self.field, self.action = field, action

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": str(self), "field": self.field, "action": self.action}


class MissingStudyDataError(PowerMachineError):
    """Raised when a supported study needs data that was not supplied."""
    code = "missing_study_data"


class UnsupportedStudyError(PowerMachineError):
    """Raised when the selected method or model is outside the supported scope."""
    code = "unsupported_study"


class InvalidModelError(PowerMachineError):
    """Raised when a model cannot be read or does not meet the public contract."""
    code = "invalid_model"


class UnknownElementError(PowerMachineError):
    """Raised when an element identifier is absent from a network."""
    code = "unknown_element"


def _json_value(value: Any) -> Any:
    if is_dataclass(value):
        value = asdict(value)
    if hasattr(value, "tolist") and not isinstance(value, (str, bytes, bytearray)):
        value = value.tolist()
    elif hasattr(value, "item") and not isinstance(value, (str, bytes, bytearray)):
        value = value.item()
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, complex):
        return {"real": _json_value(value.real), "imag": _json_value(value.imag)}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, bool)) or value is None:
        return value
    return str(value)


def default_output_dir() -> Path:
    """Return the configured user output directory without creating it."""
    from .resources import output_root
    return output_root()


def _output_path(path: str | Path | None, category: str) -> Path:
    if path is None:
        return default_output_dir() / category / uuid4().hex
    candidate = Path(path).expanduser()
    return candidate if candidate.is_absolute() else default_output_dir() / candidate


@dataclass(frozen=True)
class StudyTable:
    """Stable records table with units and optional pandas conversion."""
    name: str
    records: tuple[dict[str, Any], ...] = ()
    units: dict[str, str] = field(default_factory=dict)

    def to_records(self) -> list[dict[str, Any]]:
        """Return a deep-copied list of JSON-safe records."""
        return deepcopy(_json_value(list(self.records)))

    def select(self, *columns: str) -> "StudyTable":
        """Return a table containing only the requested columns."""
        unknown = [column for column in columns if not any(column in row for row in self.records)]
        if unknown:
            raise KeyError("Unknown table column: " + ", ".join(unknown))
        return StudyTable(self.name, tuple({key: row.get(key) for key in columns} for row in self.records),
                          {key: self.units[key] for key in columns if key in self.units})

    def to_csv(self, path: str | Path) -> Path:
        """Write UTF-8 CSV records and return the effective destination."""
        target = _output_path(path, "tables")
        target.parent.mkdir(parents=True, exist_ok=True)
        fields = list(dict.fromkeys(key for row in self.records for key in row))
        with target.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(self.to_records())
        return target

    def to_dataframe(self):
        """Return a pandas DataFrame when the optional ``tables`` extra is installed."""
        try:
            import pandas as pd
        except ModuleNotFoundError as exc:
            raise ImportError("Install optional table support with: pip install power-machine[tables]") from exc
        return pd.DataFrame(self.to_records())

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "units": deepcopy(self.units), "records": self.to_records()}


@dataclass
class StudyResult:
    """Common result header shared by every public study result."""
    status: str
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)

    def require_complete(self) -> "StudyResult":
        """Require complete study coverage without treating electrical violations as errors."""
        if self.status != "completed" or not self.coverage.get("complete", self.status == "completed"):
            raise UnsupportedStudyError("Study coverage is incomplete", field="coverage",
                                        action="Review diagnostics and provide the missing study data")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Return a strict JSON-safe representation without engine internals."""
        return _json_value({name: value for name, value in self.__dict__.items() if not name.startswith("_")})


@dataclass
class LoadflowResult(StudyResult):
    buses: StudyTable = field(default_factory=lambda: StudyTable("buses"))
    branches: StudyTable = field(default_factory=lambda: StudyTable("branches"))
    generators: StudyTable = field(default_factory=lambda: StudyTable("generators"))
    loads: StudyTable = field(default_factory=lambda: StudyTable("loads"))
    islands: StudyTable = field(default_factory=lambda: StudyTable("islands"))
    summary: dict[str, Any] = field(default_factory=dict)
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)


@dataclass
class ContingencyResult(StudyResult):
    cases: StudyTable = field(default_factory=lambda: StudyTable("cases"))
    summary: dict[str, Any] = field(default_factory=dict)
    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def rank(self, by: tuple[str, ...] = ("unserved_mw", "max_loading_pct")) -> StudyTable:
        """Return cases with readable outage labels, sorted by descending severity."""
        rows = self.cases.to_records()
        return StudyTable("cases", tuple(sorted(rows, key=lambda row: tuple(row.get(key) or 0 for key in by), reverse=True)),
                          deepcopy(self.cases.units))


@dataclass
class ShortCircuitResult(StudyResult):
    faults: StudyTable = field(default_factory=lambda: StudyTable("faults"))
    summary: dict[str, Any] = field(default_factory=dict)
    _raw: Any = field(default=None, repr=False)


@dataclass
class RmsResult(StudyResult):
    samples: StudyTable = field(default_factory=lambda: StudyTable("samples"))
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class CctResult(StudyResult):
    trials: StudyTable = field(default_factory=lambda: StudyTable("trials"))
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class QuasiDynamicResult(StudyResult):
    records: StudyTable = field(default_factory=lambda: StudyTable("records"))
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenericStudyResult(StudyResult):
    tables: dict[str, StudyTable] = field(default_factory=dict)
    summary: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LoadflowOptions:
    """Load-flow options. Voltage is in p.u. and tolerances are in p.u."""
    q_limits: bool = True
    max_iter: int = 30
    tolerance_pu: float = 1e-8

    def to_engine(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ContingencyOptions:
    """N-1/N-2 limits. Voltage is p.u., loading is percent and powers are MW."""
    orders: tuple[int, ...] = (1,)
    max_cases: int = 10000
    voltage_min_pu: float = 0.95
    voltage_max_pu: float = 1.05
    loading_limit_pct: float = 100.0
    q_limits: bool = True
    continue_on_failure: bool = True
    island_reference: str = "largest_generator"

    def __post_init__(self) -> None:
        if self.island_reference not in {"largest_generator", "none"}:
            raise InvalidModelError(
                "island_reference must be 'largest_generator' or 'none'",
                field="island_reference",
                action="Use one of the documented island reference policies",
            )


@dataclass(frozen=True)
class ShortCircuitOptions:
    """IEC or ANSI short-circuit options. Frequency is Hz and fault impedance is ohm."""
    method: str = "iec60909"
    case: str = "maximum"
    frequency_hz: float = 50.0
    profile: str = "iec_radial_v1"
    min_break_time_s: float | None = None
    fault_duration_s: float | None = None

    def to_engine(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RmsOptions:
    """RMS options. Times are seconds and output is written outside the repository."""
    t_start_s: float | None = None
    output: str | Path | None = None
    output_every: int = 1
    monitored: tuple[str, ...] | None = None
    linear_solver: str = "compatibility"
    retry_steps: bool = False
    min_step_s: float = 1e-6
    max_step_retries: int = 12

    def __post_init__(self) -> None:
        if self.linear_solver not in {"compatibility", "numpy", "scipy", "auto"}:
            raise InvalidModelError(
                "linear_solver must be compatibility, numpy, scipy, or auto",
                field="linear_solver",
                action="Choose one of the documented RMS linear solvers",
            )
        if not isinstance(self.retry_steps, bool):
            raise InvalidModelError("retry_steps must be boolean", field="retry_steps", action="Use true or false")
        if isinstance(self.min_step_s, bool) or not math.isfinite(float(self.min_step_s)) or self.min_step_s <= 0:
            raise InvalidModelError("min_step_s must be a finite positive number", field="min_step_s",
                                    action="Provide a time in seconds greater than zero")
        if isinstance(self.max_step_retries, bool) or not isinstance(self.max_step_retries, int) or self.max_step_retries < 0:
            raise InvalidModelError("max_step_retries must be a non-negative integer", field="max_step_retries",
                                    action="Provide 0 or a positive whole number")


@dataclass(frozen=True)
class CctOptions:
    """CCT thresholds in seconds, milliseconds, degrees and p.u."""
    lower_clearing_s: float = 0.05
    upper_clearing_s: float = 0.35
    resolution_ms: float = 5.0
    end_s: float = 5.0
    step_ms: float = 2.0
    max_angle_from_coi_deg: float = 180.0
    recovery_min_voltage_pu: float = 0.90
    recovery_window_s: float = 0.20


@dataclass(frozen=True)
class QuasiDynamicOptions:
    """Sequential QSS controls. Output is always outside the repository."""
    rule: str = "reference"
    warm_start: bool = True
    policy: str = "continue"
    resume_from: str | Path | None = None
    start_index: int = 0
    end_index: int | None = None
    loadflow_options: dict[str, Any] = field(default_factory=lambda: {"q_limits": False, "missing": "error"})
    output: str | Path | None = None


class Network:
    """Editable in-memory network with explicit, reproducible model access."""
    def __init__(self, model: dict[str, Any], *, name: str = "custom", source_dir: str | Path | None = None) -> None:
        if not isinstance(model, dict) or not isinstance(model.get("buses"), list):
            raise InvalidModelError("Model must contain a buses list", field="model", action="Load a valid model.json")
        self._model, self.name = deepcopy(model), str(name)
        self._source_dir = Path(source_dir).resolve() if source_dir else None
        self._scenario: dict[str, dict[str, Any]] = {}

    def _index(self) -> dict[str, tuple[str, dict[str, Any]]]:
        return {str(row["id"]): (group, row) for group in _GROUPS for row in self._model.get(group, []) if row.get("id")}

    def summary(self) -> dict[str, Any]:
        """Return stable network metadata and element counts."""
        return _json_value({"name": self.name, "schema": self._model.get("schema"), "meta": self._model.get("meta", {}),
                            "counts": {group: len(self._model.get(group, [])) for group in _GROUPS}})

    def elements(self, kind: str | None = None) -> list[dict[str, Any]]:
        """List elements by storage group or electrical class."""
        return [_json_value({**deepcopy(row), "group": group}) for group in _GROUPS for row in self._model.get(group, [])
                if kind is None or kind in (group, row.get("class"))]

    def get(self, element_id: str) -> dict[str, Any]:
        """Return one element by stable identifier."""
        try: group, row = self._index()[str(element_id)]
        except KeyError as exc: raise UnknownElementError(f"Unknown element: {element_id}", field="element_id", action="Use Network.elements()") from exc
        return _json_value({**deepcopy(row), "group": group})

    def set(self, element_id: str, **attrs: Any) -> "Network":
        """Apply editable attributes to an element and return this network."""
        if not attrs: raise InvalidModelError("At least one attribute is required", field="attrs", action="Provide editable attributes")
        try: _, row = self._index()[str(element_id)]
        except KeyError as exc: raise UnknownElementError(f"Unknown element: {element_id}", field="element_id", action="Use Network.elements()") from exc
        for key, value in attrs.items():
            if key in {"id", "object_id", "group"}: raise InvalidModelError(f"{key} is immutable", field=key, action="Edit an operational attribute")
            if key in {"outserv", "closed"}:
                if not isinstance(value, bool): raise InvalidModelError(f"{key} must be boolean", field=key, action="Use true or false")
                self._scenario.setdefault(key, {})[str(element_id)] = value
            elif key == "p_mw" and row.get("class") in {"ElmSym", "ElmGenstat", "ElmPvsys"}:
                self._scenario.setdefault("gen_p_mw", {})[str(element_id)] = value
            elif key == "v_set_pu": self._scenario.setdefault("v_set_pu", {})[str(element_id)] = value
            elif key == "load_scale": self._scenario.setdefault("load_scale", {})[str(element_id)] = value
            row[key] = deepcopy(value)
        return self

    def switch(self, element_id: str, closed: bool) -> "Network":
        """Open or close a switch element."""
        return self.set(element_id, closed=closed)

    def copy(self) -> "Network":
        """Return an independent network copy."""
        other = Network(self._model, name=self.name, source_dir=self._source_dir); other._scenario = deepcopy(self._scenario); return other

    def to_model(self) -> dict[str, Any]:
        """Return a deep copy of the current model for advanced integrations."""
        return deepcopy(self._model)

    def save(self, path: str | Path) -> Path:
        """Save JSON. Relative paths resolve under the user output directory."""
        target = _output_path(path, "networks"); target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(_json_value(self._model), ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
        return target


def list_cases() -> list[str]:
    """List included study cases."""
    return sorted(name for name, path in _CASES.items() if path.is_file())


def load_case(name_or_path: str | Path) -> Network:
    """Load an included case, model JSON, or a PowerFactory CSV folder."""
    name = str(name_or_path)
    if name.lower() in _CASES: path, label = _CASES[name.lower()], name.lower()
    else:
        path = Path(name_or_path).expanduser()
        if path.is_dir() and not (path / "model.json").is_file(): return import_powerfactory(path)
        path = path / "model.json" if path.is_dir() else path; label = path.stem
    if not path.is_file(): raise InvalidModelError(f"Case was not found: {name_or_path}", field="name_or_path", action="Use list_cases() or provide model.json")
    from .study import load_network
    _restore_public("loadflow")
    _restore_public("contingency")
    _restore_public("shortcircuit")
    try: model = load_network(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc: raise InvalidModelError(str(exc), field="model.json", action="Check the model file") from exc
    return Network(model, name=label, source_dir=path.parent)


def import_powerfactory(csv_dir: str | Path, overrides: str | Path | None = None) -> Network:
    """Build a network from a user-provided PowerFactory CSV export."""
    from .builder import build_model
    source = Path(csv_dir).expanduser().resolve()
    try: model = build_model(source, enrich_from=overrides)
    except (OSError, ValueError, KeyError) as exc: raise InvalidModelError(str(exc), field="csv_dir", action="Provide a complete PowerFactory CSV export") from exc
    dynamic = source / "dynamic"
    if (dynamic / "manifest.json").is_file(): model["_dynamic_dir"] = str(dynamic)
    return Network(model, name=source.name, source_dir=source)


def _provenance(net: Network) -> dict[str, Any]: return {"network": net.name, "schema": net._model.get("schema")}
def _coverage(status: str, **values: Any) -> dict[str, Any]: return {"complete": status == "completed", **values}
def _diagnostics(raw: dict[str, Any]) -> list[dict[str, Any]]:
    return _json_value(raw.get("diagnostics", [{"code": "engine_warning", "level": "warning", "message": text} for text in raw.get("warnings", [])]))


def _restore_public(name: str) -> None:
    """Keep a public function from being shadowed by its engine submodule."""
    package = sys.modules.get("power_machine")
    if package is not None:
        setattr(package, name, globals()[name])


def loadflow(net: Network, options: LoadflowOptions | None = None) -> LoadflowResult:
    """Run AC load flow and return typed tables with p.u., MW, Mvar and percent units."""
    _require_network(net)
    from .loadflow import run_loadflow
    _restore_public("loadflow")
    from .lf_status import energized_status
    from .loading import branch_loading
    options = options or LoadflowOptions(); raw = run_loadflow(net._model, scenario=net._scenario, options=options.to_engine())
    status = "completed" if energized_status(raw).get("ok") else "partial"; loading = branch_loading(net._model, raw)
    branches = [{**row, "loading_pct": (loading.get(row["id"], {}) or {}).get("loading_pct")} for row in raw.get("branches", [])]
    return LoadflowResult(status, _diagnostics(raw), _coverage(status, converged=bool(raw.get("converged"))), _provenance(net),
        StudyTable("buses", tuple(raw.get("buses", [])), {"u_pu": "p.u.", "angle_deg": "deg"}),
        StudyTable("branches", tuple(branches), {"p_from_mw": "MW", "q_from_mvar": "Mvar", "loading_pct": "%"}),
        StudyTable("generators", tuple(raw.get("gens", [])), {"p_mw": "MW", "q_mvar": "Mvar"}),
        StudyTable("loads", tuple(raw.get("loads", [])), {"p_mw": "MW", "q_mvar": "Mvar"}),
        StudyTable("islands", tuple(raw.get("islands", []))), {"converged": raw.get("converged"), "max_mismatch_mva": raw.get("max_mismatch_mva")}, raw)


def contingency(net: Network, elements: list[str] | None = None, criterion: str = "N-1", *, options: ContingencyOptions | None = None) -> ContingencyResult:
    """Run N-1 or N-2 contingencies with stable cases records."""
    _require_network(net); from .contingency import ContingencyOptions as EngineOptions, run_contingencies
    _restore_public("contingency")
    options = options or ContingencyOptions(orders=(1,) if criterion.upper() == "N-1" else (2,))
    engine = EngineOptions(**asdict(options)); raw = run_contingencies(net._model, net._scenario, engine, selected_ids=elements,
        filter_reason="Explicit public API selection" if elements else None)
    status = raw.get("status", "completed"); payload = raw.get("payload", raw); engine_rows = payload.get("cases", []) if isinstance(payload, dict) else []
    rows = []
    for case in engine_rows:
        metrics = case.get("metrics", {}) if isinstance(case, dict) else {}
        outage_ids: list[str] = []
        for outage in case.get("outages", ()):
            if not isinstance(outage, dict):
                continue
            identifier = outage.get("object_id")
            if identifier is not None:
                outage_ids.append(str(identifier))
            elif isinstance(outage.get("member_ids"), list):
                outage_ids.extend(str(member) for member in outage["member_ids"])
        outage_ids = list(dict.fromkeys(outage_ids))
        rows.append({**case,
                     "outage_ids": outage_ids,
                     "label": " + ".join(outage_ids) if outage_ids else "Unnamed outage",
                     "unserved_mw": metrics.get("unsupplied_p_mw"),
                     "max_loading_pct": metrics.get("max_loading_pct"),
                     "min_voltage_pu": metrics.get("min_u_pu"),
                     "max_voltage_pu": metrics.get("max_u_pu"),
                     "severity": metrics.get("severity"),
                     "disconnected_generation_mw": metrics.get("disconnected_generation_p_mw"),
                     "newly_disconnected_generation_mw": metrics.get("newly_disconnected_generation_p_mw"),
                     "disconnected_generator_ids": metrics.get("disconnected_generator_ids", []),
                     "alert_priority": metrics.get("alert_priority")})
    return ContingencyResult(status, _diagnostics(raw), _coverage(status, evaluated=len(rows)), _provenance(net),
        StudyTable("cases", tuple(rows), {"unserved_mw": "MW", "max_loading_pct": "%", "min_voltage_pu": "p.u.",
                                            "disconnected_generation_mw": "MW", "newly_disconnected_generation_mw": "MW",
                                            "alert_priority": "rank"}), payload.get("summary", {}), raw)


def shortcircuit(net: Network, bus: str | None = None, fault: str = "3ph", *, sc_data: Any = None,
                 options: ShortCircuitOptions | None = None, selected_bus_ids: list[str] | None = None) -> ShortCircuitResult:
    """Run one fault or a bus sweep using explicit short-circuit source data."""
    _require_network(net)
    if sc_data is None: raise MissingStudyDataError("Short-circuit source data is required", field="sc_data", action="Provide a ShortCircuitData document or JSON path")
    from .shortcircuit import FaultSpec, ShortCircuitData, ShortCircuitOptions as EngineOptions, run_shortcircuit
    _restore_public("shortcircuit")
    from .shortcircuit.data import from_dict
    if isinstance(sc_data, (str, Path)): sc_data = json.loads(Path(sc_data).read_text(encoding="utf-8-sig"))
    data = sc_data if isinstance(sc_data, ShortCircuitData) else from_dict(sc_data); options = options or ShortCircuitOptions()
    if bus is not None:
        raw = run_shortcircuit(net._model, FaultSpec(str(bus), str(fault)), EngineOptions(**options.to_engine()), data)
        row = asdict(raw); status = row.get("status", "completed")
        return ShortCircuitResult(status, row.get("diagnostics", []), _coverage(status, evaluated=1), _provenance(net), StudyTable("faults", (row,), {"Ik_initial_sym_ka": "kA"}), row, raw)
    from .sc_sweep import run_sweep
    engine_options = options.to_engine() | {"fault_kind": fault}
    raw = run_sweep(net._model, {"scenario": deepcopy(net._scenario)}, engine_options, asdict(data), selected_bus_ids=selected_bus_ids,
                    root=Path(__file__).resolve().parents[1]); status = raw.get("status", "completed")
    return ShortCircuitResult(status, _diagnostics(raw), _coverage(status, evaluated=len(raw.get("rows", []))), _provenance(net), StudyTable("faults", tuple(raw.get("rows", [])), {"ik_initial_sym_ka": "kA"}), raw, raw)


def rms(net: Network, events: list[dict[str, Any]], *, t_end_s: float, step_s: float, options: RmsOptions | None = None) -> RmsResult:
    """Run RMS in seconds, using IEEE 39 classical data or explicit dynamic data."""
    _require_network(net); options = options or RmsOptions()
    if net._model.get("meta", {}).get("benchmark") == "ieee39":
        from .benchmarks.ieee39_dynamics import ClassicalSystem, simulate
        data_path = Path(__file__).with_name("cases") / "ieee39" / "dynamic_data.json"; data = json.loads(data_path.read_text(encoding="utf-8-sig"))
        event = "BASE" if not events else "E39-DIN-01" if [row.get("kind") for row in events] == ["short_circuit", "clear_short_circuit"] else None
        if event is None: raise UnsupportedStudyError("IEEE 39 classical RMS supports BASE or its documented fault event", field="events", action="Use explicit dynamic data for custom events")
        run = simulate(ClassicalSystem(net._model, data), event=event, end_s=float(t_end_s), step_s=float(step_s))
        return RmsResult("completed", [], _coverage("completed"), _provenance(net), StudyTable("samples", tuple(asdict(s) for s in run.samples), {"time_s": "s"}), run.report)
    dynamic_dir = net._model.get("_dynamic_dir")
    if not dynamic_dir:
        raise MissingStudyDataError("Dynamic data is missing", field="_dynamic_dir", action="Load a case with dynamic/manifest.json")
    if net._source_dir is None:
        raise MissingStudyDataError("The dynamic study root is unknown", field="net", action="Load the network from its case directory")
    from .study import (SimEvent, SimOptions, StudyCase, _scenario, _translate_event,
                        default_monitored, element_index, run_loadflow as run_study_loadflow,
                        validate_study)
    from .dynamics.runner import RMSRunner
    try:
        translated = [SimEvent(float(row["time_s"]), str(row["kind"]), str(row["target_id"]),
                               deepcopy(row.get("params", {}))) for row in events]
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidModelError("Each RMS event needs time_s, kind, and target_id", field="events",
                                action="Use the documented RMS event record") from exc
    study = StudyCase(
        name=f"{net.name}-rms",
        scenario=deepcopy(net._scenario),
        events=translated,
        options=SimOptions(t_start_s=options.t_start_s if options.t_start_s is not None else 0.0,
                           t_end_s=float(t_end_s), dt_s=float(step_s), output_every=options.output_every),
        monitored=list(options.monitored or default_monitored(net._model)),
    )
    destination = _output_path(options.output, "rms")
    try:
        model = net.to_model()
        messages = validate_study(net._source_dir, study, model)
        errors = [message["message"] for message in messages if message.get("level") == "error"]
        if errors:
            raise ValueError("\n".join(errors))
        operating = run_study_loadflow(net._source_dir, study, model)
        if any(row.get("status") in {"not_converged", "no_convergente"} for row in operating.get("islands", [])):
            raise ValueError("The RMS operating-point load flow did not converge")
        point = {
            "bus_voltages": {row["id"]: row["u_pu"] * complex(math.cos(math.radians(row["angle_deg"])), math.sin(math.radians(row["angle_deg"])))
                             for row in operating["buses"] if row.get("energized")},
            "generator_powers": {row["id"]: complex(row["p_mw"], row["q_mvar"])
                                 for row in operating["gens"] if row.get("p_mw") is not None},
        }
        model = _scenario(model, study.scenario)
        index = element_index(model)
        engine_events = [_translate_event(event, index, model["meta"]["s_base_mva"]) for event in study.events]
        runner = RMSRunner(net._source_dir, events=engine_events, monitored=study.monitored,
                           tstart=study.options.t_start_s, dt_s=study.options.dt_s, model=model,
                           operating_point=point, linear_solver=options.linear_solver,
                           retry_steps=options.retry_steps, min_step_s=options.min_step_s,
                           max_step_retries=options.max_step_retries)
        run = runner.run(end_s=study.options.t_end_s, output=destination, output_every=options.output_every)
        run["signals_csv"] = str(destination / "signals.csv")
        run["summary_json"] = str(destination / "summary.json")
        (destination / "summary.json").write_text(
            json.dumps(_json_value(run), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise UnsupportedStudyError("The dynamic model cannot run the requested RMS study", field="events",
                                    action="Check the dynamic manifest, events, and operating point") from exc
    signals = Path(run["signals_csv"])
    try:
        with signals.open(encoding="utf-8-sig", newline="") as stream:
            samples = tuple(csv.DictReader(stream))
    except OSError as exc:
        raise UnsupportedStudyError("RMS completed without a readable signal table", field="output",
                                    action="Check write permissions for POWER_MACHINE_OUTPUT_DIR") from exc
    status = "partial" if run.get("cancelled") or run.get("failure") else "completed"
    diagnostics = [{"code": "rms_failure", "level": "error", "message": run["failure"]}] if run.get("failure") else []
    return RmsResult(status, diagnostics, _coverage(status, samples=len(samples)), _provenance(net),
                     StudyTable("samples", samples, {"time_s": "s"}), _json_value(run))


def pv_curve(net: Network, *, options: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None) -> GenericStudyResult:
    """Run a PV curve with engine options expressed in MW and p.u."""
    return _voltage(net, "PV", options or {}, inputs or {})
def qv_curve(net: Network, *, options: dict[str, Any] | None = None, inputs: dict[str, Any] | None = None) -> GenericStudyResult:
    """Run a QV curve with engine options expressed in Mvar and p.u."""
    return _voltage(net, "QV", options or {}, inputs or {})
def _voltage(net: Network, kind: str, options: dict[str, Any], inputs: dict[str, Any]) -> GenericStudyResult:
    _require_network(net); from .voltage_stability import run_voltage_stability
    raw = run_voltage_stability(net._model, net._scenario, {**inputs, "curve_type": kind}, options); status = raw.get("status", "completed")
    points = raw.get("payload", {}).get("points", [])
    return GenericStudyResult(status, _diagnostics(raw), _coverage(status, evaluated=len(points)), _provenance(net), {"points": StudyTable("points", tuple(points))}, raw.get("payload", {}))


def sensitivities(net: Network, injection_bus_id: str, **options: Any) -> GenericStudyResult:
    """Run local voltage and flow sensitivities at one injection bus."""
    _require_network(net); from .sensitivities import run_sensitivities; from .sensitivities.schema import SensitivityOptions
    raw = run_sensitivities(net._model, net._scenario, SensitivityOptions(injection_bus_id, **options)); status = raw.get("status", "completed")
    return GenericStudyResult(status, _diagnostics(raw), _coverage(status), _provenance(net), {}, raw.get("payload", raw))


def cct(net: Network, fault: dict[str, Any], *, options: CctOptions | None = None) -> CctResult:
    """Run a typed CCT study for a supported dynamic profile."""
    _require_network(net)
    if not isinstance(fault, dict):
        raise InvalidModelError("fault must be a dictionary", field="fault", action="Provide a CCT fault request")
    from .cct import CCTInput, CCTOptions as EngineOptions, run_cct
    settings = options or CctOptions()
    try:
        request = CCTInput(**deepcopy(fault))
        if net._model.get("meta", {}).get("benchmark") == "ieee39" and request.engine not in {"auto", "ieee39_classical"}:
            raise UnsupportedStudyError("IEEE 39 CCT supports the classical engine only", field="fault.engine",
                                        action="Use engine='ieee39_classical' or engine='auto'")
        raw = run_cct(net.to_model(), deepcopy(net._scenario), EngineOptions(**asdict(settings)), request,
                      root=net._source_dir)
    except UnsupportedStudyError:
        raise
    except (TypeError, ValueError, KeyError) as exc:
        code = getattr(exc, "code", "")
        error_type = MissingStudyDataError if str(code).startswith("missing_") else InvalidModelError
        raise error_type(str(exc), field="fault" if "fault" in str(exc).lower() else "options",
                         action="Use a supported CCT profile and complete fault data") from exc
    status = raw.get("status", "partial")
    diagnostics = [{"code": "cct_validity", "level": "warning", "message": message}
                   for message in raw.get("validity_messages", [])]
    if raw.get("reason"):
        diagnostics.append({"code": "cct_incomplete", "level": "warning", "message": raw["reason"]})
    summary = {key: value for key, value in raw.items() if key not in {"trials", "stable_trials", "unstable_trials", "not_evaluable_trials"}}
    provenance = {**_provenance(net), "dynamic_data": "included IEEE 39 classical parameters" if net._model.get("meta", {}).get("benchmark") == "ieee39" else "user supplied"}
    return CctResult(status, diagnostics, _coverage(status, trials=len(raw.get("trials", []))), provenance,
                     StudyTable("trials", tuple(raw.get("trials", [])), {"clearing_s": "s"}), summary)


def quasi_dynamic(net: Network, series: Any, *, options: QuasiDynamicOptions | None = None) -> QuasiDynamicResult:
    """Run sequential quasi-dynamic load-flow steps for a user-supplied Series."""
    _require_network(net)
    from .qss.series import Series
    from .qss.runner import run_series
    if not isinstance(series, Series):
        raise MissingStudyDataError("quasi_dynamic requires a qss.Series", field="series",
                                    action="Create Series and SeriesStep records or use qss.read_series")
    settings = options or QuasiDynamicOptions()
    destination = _output_path(settings.output, "qss")
    try:
        raw = run_series(net.to_model(), series, destination, rule=settings.rule, warm_start=settings.warm_start,
                         policy=settings.policy, resume_from=settings.resume_from, start_index=settings.start_index,
                         end_index=settings.end_index, options=deepcopy(settings.loadflow_options))
    except FileExistsError as exc:
        raise InvalidModelError("The quasi-dynamic output directory already exists", field="options.output",
                                action="Provide a new output directory") from exc
    except (TypeError, ValueError, KeyError, OSError) as exc:
        raise InvalidModelError(str(exc), field="series", action="Provide complete, increasing user series data") from exc
    records = raw.get("records", [])
    status = "completed" if records and all(row.get("accepted") for row in records) else "partial"
    diagnostics = [{"code": "qss_step_failed", "level": "warning", "message": f"Step {row.get('index')} was not accepted"}
                   for row in records if not row.get("accepted")]
    summary = _json_value(raw.get("summary", {}))
    summary["output"] = str(raw.get("output", destination))
    return QuasiDynamicResult(status, diagnostics, _coverage(status, steps=len(records)),
                              {**_provenance(net), "series": series.name, "series_provenance": series.provenance},
                              StudyTable("records", tuple(records), {"t_s": "s", "elapsed_s": "s"}), summary)


def compare_powerfactory(net: Network, pf_results_dir: str | Path | None = None, study: str = "loadflow") -> GenericStudyResult:
    """Compare a fresh calculation against a PowerFactory export."""
    from .resources import pf_data_root
    _require_network(net); folder = pf_data_root(pf_results_dir); study = study.lower()
    if study == "loadflow":
        from .pfcompare.lf import compare_results; result = loadflow(net); comparison = compare_results(net.to_model(), result._raw, _study_folder(folder, "E39-LF-01", "buses.csv"))
    elif study == "network":
        from .pfcompare.net import compare_network; result = loadflow(net); comparison, audits = compare_network(net.to_model(), result._raw, _study_folder(folder, "E39-LF-01", "buses.csv")); comparison = {"comparison": comparison, "audits": audits}
    elif study == "shortcircuit":
        from .pfcompare.cc import build_supplement, calculate_faults, compare_shortcircuit
        ref = _study_folder(folder, "E39-CC-01", "fault_summary.csv"); root = next((p for p in (folder, *folder.parents) if (p / "modelo").is_dir()), None)
        if root is None: raise MissingStudyDataError("PowerFactory SC model data is missing", field="pf_results_dir", action="Provide the complete IEC export")
        model_path = root / "model.json"
        if not model_path.is_file():
            raise MissingStudyDataError("PowerFactory SC model is missing", field="pf_results_dir", action="Provide model.json with the complete IEC export")
        reference_model = json.loads(model_path.read_text(encoding="utf-8-sig"))
        comparison = compare_shortcircuit(reference_model, calculate_faults(reference_model, build_supplement(reference_model, root)), ref)
    else: raise UnsupportedStudyError("Unknown comparison study", field="study", action="Use loadflow, shortcircuit, or network")
    return GenericStudyResult("completed", [], _coverage("completed"), _provenance(net), {"comparison": StudyTable("comparison", tuple(comparison if isinstance(comparison, list) else comparison.get("rows", [])))}, {"study": study, "comparison": comparison})


def _study_folder(folder: Path, name: str, marker: str) -> Path:
    if (folder / marker).is_file(): return folder
    if (folder / name / marker).is_file(): return folder / name
    raise MissingStudyDataError(f"Reference file is missing: {marker}", field="pf_results_dir", action="Provide a complete PowerFactory export")
def _require_network(net: Network) -> None:
    if not isinstance(net, Network): raise InvalidModelError("net must be a Network", field="net", action="Load a case first")


__all__ = ["PowerMachineError", "MissingStudyDataError", "UnsupportedStudyError", "InvalidModelError", "UnknownElementError", "StudyTable", "StudyResult", "LoadflowResult", "ContingencyResult", "ShortCircuitResult", "RmsResult", "CctResult", "QuasiDynamicResult", "GenericStudyResult", "LoadflowOptions", "ContingencyOptions", "ShortCircuitOptions", "RmsOptions", "CctOptions", "QuasiDynamicOptions", "Network", "default_output_dir", "list_cases", "load_case", "import_powerfactory", "loadflow", "shortcircuit", "contingency", "pv_curve", "qv_curve", "sensitivities", "rms", "cct", "quasi_dynamic", "compare_powerfactory"]
