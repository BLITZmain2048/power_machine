"""Punto de entrada para indicadores RMS sobre una corrida guardada."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path

from .analysis import (frequency_metrics, indicators_for, link_signals, oscillation_metrics, power_metrics,
                       profile_with_overrides, read_signals, voltage_metrics)
from .results import diagnostic, envelope, json_data
from .schema import DynMetricOptions


WORK_ROOT = Path(__file__).resolve().parents[2]


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def run_metrics(run_directory, options: DynMetricOptions, *, selected_signals=None, selected_element_id=None,
                model=None, cancel=None, progress=None):
    """Calcula indicadores disponibles, sin ejecutar una simulación RMS."""
    result = envelope(asdict(options))
    try:
        run_directory = Path(run_directory).resolve()
        profile = profile_with_overrides(options.profile)
        result["provenance"]["profile_name"] = profile["name"]
        result["provenance"]["profile_label"] = profile["label"]
        if progress:
            progress({"phase": "reading", "completed": 0, "total": 1, "fraction": 0.0})
        time_s, signals, source, event_times, metadata = read_signals(run_directory)
        signals = link_signals(signals, model or {}, metadata)
        primary_event_s = options.event_time_s if options.event_time_s is not None else (event_times[0] if event_times else None)
        all_event_times = tuple(sorted(set(event_times + ((primary_event_s,) if primary_event_s is not None else ()))))
        result["payload"]["event_times_s"] = list(event_times)
        result["provenance"]["inputs"].append({"path": str(source), "kind": "signals_csv"})
        result["provenance"]["inputs"][-1]["sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        metadata_hashes = {}
        for name in ("run_report.json", "caso.pm", "study.pm"):
            path = run_directory / name
            if path.is_file():
                metadata_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        result["provenance"]["metadata_sha256"] = metadata_hashes
        encoded_events = json.dumps({key: value.get("events", []) for key, value in metadata.items() if isinstance(value, dict)}, sort_keys=True, ensure_ascii=False).encode("utf-8")
        result["provenance"]["events_sha256"] = hashlib.sha256(encoded_events).hexdigest()
        result["provenance"]["options_sha256"] = hashlib.sha256(json.dumps(asdict(options), sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
        selected = set(selected_signals or [])
        if selected:
            unknown = selected - {signal["signal_id"] for signal in signals}
            if unknown:
                raise ValueError(f"Señales seleccionadas inexistentes: {sorted(unknown)}")
            signals = [signal for signal in signals if signal["signal_id"] in selected or signal["kind"] in ("frequency", "voltage", "power")]
        if selected_element_id:
            linked = [signal for signal in signals if signal.get("linked_object_id") == selected_element_id]
            if not linked:
                result["status"] = "partial"
                result["diagnostics"].append(diagnostic("signal_link_missing", "No hay vínculo unívoco con la barra elegida. Elija la señal directamente."))
                return result
            signals = linked
        processable = [signal for signal in signals if signal["kind"] in ("frequency", "voltage", "power") or signal["signal_id"] in selected]
        if not processable:
            result["status"] = "unsupported"
            result["diagnostics"].append(diagnostic("signals_unsupported", "No hay señales compatibles para indicadores"))
            return result
        for index, signal in enumerate(processable):
            if _cancelled(cancel):
                result["status"] = "cancelled"
                return json_data(result)
            kind = signal["kind"] if signal["kind"] != "selected" else "oscillation"
            metric_signal = {**signal, "kind": kind}
            if kind == "frequency":
                metrics, diagnostics = frequency_metrics(time_s, signal["values"], event_time_s=primary_event_s,
                                                         window_ms=options.rocof_window_ms, event_times=all_event_times)
            elif kind == "voltage":
                metrics, diagnostics = voltage_metrics(time_s, signal["values"], event_time_s=primary_event_s,
                                                       band_min_pu=options.voltage_band_min_pu,
                                                       band_max_pu=options.voltage_band_max_pu,
                                                       recovery_pu=options.voltage_recovery_pu)
            elif kind == "power":
                metrics, diagnostics = power_metrics(time_s, signal["values"], event_time_s=primary_event_s,
                                                     settling_band_pct=options.settling_band_pct)
            else:
                metrics, diagnostics = oscillation_metrics(time_s, signal["values"], event_time_s=primary_event_s,
                                                           event_times=all_event_times)
            result["payload"]["signals"].append({key: value for key, value in metric_signal.items() if key != "values"} | {"metrics": metrics})
            result["payload"]["indicators"].extend(indicators_for(metric_signal, metrics, profile))
            result["diagnostics"].extend(diagnostics)
            if progress:
                progress({"phase": "analysing", "completed": index + 1, "total": len(processable),
                          "fraction": (index + 1) / len(processable)})
        if result["diagnostics"]:
            result["status"] = "partial"
        return json_data(result)
    except (ValueError, OSError, UnicodeError) as exc:
        result["status"] = "invalid_input"
        result["diagnostics"].append(diagnostic("invalid_input", exc, level="error"))
        return json_data(result)
    except Exception as exc:
        result["status"] = "error"
        result["diagnostics"].append(diagnostic("internal_error", f"{type(exc).__name__}: {exc}", level="error"))
        return json_data(result)


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    """Valida la solicitud del trabajador y confina la lectura a la raíz."""
    try:
        if not isinstance(request, dict):
            raise ValueError("request debe ser un diccionario")
        if request.get("schema") != "pm-parity-request-v1" or request.get("module") != "dyn_metrics":
            raise ValueError("Esquema o módulo de request incompatible")
        root = Path(request["root"]).resolve()
        input_data = request.get("input", {})
        if not isinstance(input_data, dict) or not isinstance(input_data.get("run_directory"), str):
            raise ValueError("input.run_directory debe ser texto")
        run_directory = (root / input_data["run_directory"]).resolve()
        if not run_directory.is_relative_to(root):
            raise ValueError("run_directory está fuera de root")
        options = DynMetricOptions(**request.get("options", {}))
        snapshot = request.get("snapshot", {})
        model = snapshot.get("model", {}) if isinstance(snapshot, dict) else {}
        if isinstance(snapshot, dict) and snapshot.get("fingerprints"):
            fingerprints = snapshot["fingerprints"]
            def digest(value):
                return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
            if fingerprints.get("model_sha256") != digest(model) or fingerprints.get("study_sha256") != digest(snapshot.get("study", {})):
                raise ValueError("Fingerprint incompatible de instantánea")
        return run_metrics(run_directory, options, selected_signals=input_data.get("selected_signals"),
                           selected_element_id=input_data.get("selected_element_id"), model=model,
                           cancel=cancel, progress=progress)
    except (ValueError, KeyError, TypeError) as exc:
        result = envelope({})
        result["status"] = "invalid_input"
        result["diagnostics"].append(diagnostic("invalid_request", exc, level="error"))
        return result
