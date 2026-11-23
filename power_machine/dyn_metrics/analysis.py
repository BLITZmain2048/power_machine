"""Lectura y cálculo puro de indicadores RMS, sin interfaz gráfica."""
from __future__ import annotations

import csv
import json
import math
from pathlib import Path

import numpy as np

from .results import diagnostic


REFERENCE_PROFILE = {
    "name": "reference_profile",
    "label": "referencial, verificar contra la criterios aplicables al estudio",
    "frequency_nadir_min_hz": 49.0,
    "frequency_zenith_max_hz": 51.0,
    "rocof_abs_max_hz_per_s": 1.0,
    "voltage_min_pu": 0.9,
    "voltage_max_pu": 1.1,
    "voltage_recovery_max_s": 3.0,
    "oscillation_damping_min": 0.03,
    "power_overshoot_max_pct": 20.0,
    "power_settling_max_s": 5.0,
}


def read_signals(run_directory: Path):
    """Lee formatos pm y IEEE 39 y conserva celdas vacías como NaN."""
    source = run_directory / "signals.csv"
    if not source.is_file():
        raise ValueError("La corrida no contiene signals.csv")
    with source.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise ValueError("signals.csv está vacío") from exc
        if not header or header[0] not in ("time_s", "time|b:tnow|s"):
            raise ValueError("La primera columna debe ser tiempo en s")
        side_index = header.index("sample_side") if "sample_side" in header else None
        numeric_indices = [index for index, name in enumerate(header) if name != "sample_side"]
        columns = {index: [] for index in numeric_indices}
        sample_sides = []
        for row in reader:
            if len(row) != len(header):
                raise ValueError("signals.csv contiene una fila con columnas incompletas")
            for index in numeric_indices:
                raw = row[index]
                if not raw.strip():
                    columns[index].append(float("nan"))
                else:
                    try:
                        columns[index].append(float(raw))
                    except ValueError as exc:
                        raise ValueError(f"Valor no numérico en {header[index]}") from exc
            if side_index is not None:
                sample_sides.append(row[side_index].strip())
    time_s = np.asarray(columns[0], dtype=float)
    if len(time_s) < 2 or not np.all(np.isfinite(time_s)) or np.any(np.diff(time_s) < 0):
        raise ValueError("La señal de tiempo requiere al menos dos muestras finitas y ordenadas")
    signals = []
    for index in numeric_indices:
        if index == 0:
            continue
        name = header[index]
        signals.append({"signal_id": name, "values": np.asarray(columns[index], dtype=float), **signal_identity(name)})
    metadata = _read_metadata(run_directory)
    return time_s, signals, source, _event_times(metadata, time_s, sample_sides), metadata


def _read_metadata(run_directory):
    result = {}
    for name in ("run_report.json", "caso.pm", "study.pm"):
        path = run_directory / name
        if path.is_file():
            try:
                result[name] = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
    return result


def _event_times(metadata, time_s, sample_sides):
    values = []
    for name in ("study.pm", "caso.pm"):
        study = metadata.get(name, {})
        if isinstance(study, dict) and isinstance(study.get("events"), list):
            for event in study["events"]:
                if isinstance(event, dict) and isinstance(event.get("time_s"), (int, float)):
                    values.append(float(event["time_s"]))
    def visit(value, key=""):
        if isinstance(value, dict):
            for name, item in value.items():
                visit(item, name.casefold())
        elif isinstance(value, list):
            for item in value:
                visit(item, key)
        elif key in {"event_time_s", "fault_start_s", "fault_clear_s", "clearing_time_s"} and isinstance(value, (int, float)):
            values.append(float(value))
    for data in metadata.values():
        visit(data)
    for instant_s, side in zip(time_s, sample_sides):
        if side in ("pre_event", "post_event"):
            values.append(float(instant_s))
    return tuple(sorted({value for value in values if math.isfinite(value)}))


def signal_identity(signal_id: str) -> dict:
    """Clasifica solo por nombre publicado, sin inventar identificación PF."""
    lower = signal_id.casefold()
    if "|" in signal_id:
        parts = signal_id.rsplit("|", 2)
        relative_path, variable, unit = parts if len(parts) == 3 else (signal_id, "", "")
        name = relative_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        if variable == "m:fehz":
            kind = "frequency"
        elif variable == "m:u":
            kind = "voltage"
        elif variable.startswith("m:p"):
            kind = "power"
        else:
            kind = "selected"
        return {"label": name, "relative_path": relative_path, "object_id": None,
                "variable": variable, "unit": unit, "kind": kind}
    prefix = signal_id.split("_", 1)[0]
    if "frequency_hz" in lower:
        kind, unit = "frequency", "Hz"
    elif lower.endswith("_u_pu"):
        kind, unit = "voltage", "p.u."
    elif lower.endswith("_p_mw"):
        kind, unit = "power", "MW"
    else:
        kind, unit = "selected", "1"
    return {"label": signal_id, "relative_path": None, "object_id": prefix,
            "variable": signal_id, "unit": unit, "kind": kind}


def link_signals(signals, model, metadata):
    """Vincula rutas PF exactas y nombres de barra IEEE sin adivinar ambigüedades."""
    elements = []
    for group in ("buses", "branches", "branches3", "gens", "loads", "switches", "shunts"):
        elements.extend(model.get(group, []) if isinstance(model, dict) else [])
    by_path, by_name = {}, {}
    by_id = {}
    for item in elements:
        object_id = item.get("object_id", item.get("id"))
        if not object_id:
            continue
        by_id[str(object_id)] = str(object_id)
        path = str(item.get("relative_path", item.get("path", ""))).replace("/", "\\").casefold()
        if path:
            by_path.setdefault(path, []).append(object_id)
        by_name.setdefault(str(item.get("name", item.get("id", ""))).casefold(), []).append(object_id)
        by_name.setdefault(str(item.get("id", "")).casefold(), []).append(object_id)
    columns = metadata.get("run_report.json", {}).get("columns", {}) if isinstance(metadata.get("run_report.json", {}), dict) else {}
    for signal in signals:
        candidates = []
        if signal.get("relative_path"):
            candidates.extend(by_path.get(signal["relative_path"].replace("/", "\\").casefold(), []))
        source = columns.get(signal["signal_id"], {})
        if not candidates and source.get("object_id") in by_id:
            candidates.append(by_id[source["object_id"]])
        if not candidates and source.get("relative_path"):
            candidates.extend(by_path.get(str(source["relative_path"]).replace("/", "\\").casefold(), []))
        prefix = signal["signal_id"].split("_", 1)[0].casefold()
        if not candidates:
            candidates.extend(by_name.get(prefix, []))
        candidates = sorted(set(candidates))
        signal["link_candidates"] = candidates
        signal["linked_object_id"] = candidates[0] if len(candidates) == 1 else None
        signal["link_status"] = "linked" if len(candidates) == 1 else "ambiguous" if candidates else "unlinked"
    return signals


def finite_samples(time_s, values):
    mask = np.isfinite(time_s) & np.isfinite(values)
    return time_s[mask], values[mask]


def frequency_metrics(time_s, values, *, event_time_s, window_ms, event_times=()):
    raw_time, raw_values = np.asarray(time_s, dtype=float), np.asarray(values, dtype=float)
    time_s, values = finite_samples(raw_time, raw_values)
    result = {
        "nadir_hz": None, "nadir_time_s": None, "zenith_hz": None, "final_hz": None,
        "time_to_nadir_s": None, "rocof_min_hz_per_s": None,
        "rocof_max_hz_per_s": None, "rocof_abs_max_hz_per_s": None,
    }
    if not len(values):
        return result, [diagnostic("frequency_empty", "Frecuencia sin muestras finitas")]
    nadir_index, zenith_index = int(np.argmin(values)), int(np.argmax(values))
    result.update(nadir_hz=float(values[nadir_index]), nadir_time_s=float(time_s[nadir_index]), zenith_hz=float(values[zenith_index]),
                  final_hz=float(values[-1]))
    if event_time_s is not None:
        result["time_to_nadir_s"] = float(time_s[nadir_index] - event_time_s)
    if event_time_s is None:
        return result, [diagnostic("event_time_missing", "ROCOF no evaluable sin hora de evento")]
    window_s = window_ms / 1000.0
    slopes = []
    for x, y in _continuous_segments(raw_time, raw_values, event_times):
        x, y = x[x >= event_time_s + .010], y[x >= event_time_s + .010]
        for instant_s in x:
            mask = (x >= instant_s - window_s) & (x <= instant_s)
            wx, wy = x[mask], y[mask]
            if len(wx) >= 3 and wx[-1] - wx[0] >= window_s - 1e-12:
                slopes.append(float(np.polyfit(wx - wx.mean(), wy, 1)[0]))
    if not slopes:
        return result, [diagnostic("rocof_insufficient", "ROCOF no evaluable con las muestras posteriores a 10 ms")]
    result["rocof_min_hz_per_s"] = min(slopes)
    result["rocof_max_hz_per_s"] = max(slopes)
    result["rocof_abs_max_hz_per_s"] = max(abs(slope) for slope in slopes)
    return result, []


def voltage_metrics(time_s, values, *, event_time_s, band_min_pu, band_max_pu, recovery_pu):
    raw_time, raw_values = np.asarray(time_s, dtype=float), np.asarray(values, dtype=float)
    time_s, values = finite_samples(raw_time, raw_values)
    result = {"min_pu": None, "min_time_s": None, "max_pu": None, "time_outside_band_s": None,
              "recovery_time_s": None}
    if not len(values):
        return result, [diagnostic("voltage_empty", "Tensión sin muestras finitas")]
    minimum_index = int(np.argmin(values))
    result["min_pu"], result["min_time_s"], result["max_pu"] = float(values[minimum_index]), float(time_s[minimum_index]), float(values.max())
    known, unknown = 0., 0.
    for i in range(len(raw_time)-1):
        duration = raw_time[i+1] - raw_time[i]
        if not np.isfinite(raw_values[i]) or not np.isfinite(raw_values[i+1]): unknown += duration
        elif raw_values[i] < band_min_pu or raw_values[i] > band_max_pu or raw_values[i+1] < band_min_pu or raw_values[i+1] > band_max_pu: known += duration
    result["time_outside_band_s"] = known if unknown == 0 else None
    result["time_outside_band_known_s"], result["time_outside_band_unknown_s"] = known, unknown
    if event_time_s is None:
        return result, [diagnostic("event_time_missing", "Recuperación de tensión no evaluable sin hora de evento")]
    for x, y in _continuous_segments(raw_time, raw_values, ()): 
        indices = np.flatnonzero((x >= event_time_s) & (y >= recovery_pu))
        for index in indices:
            if np.all(y[index:] >= recovery_pu) and x[-1] == raw_time[-1]:
                result["recovery_time_s"] = float(x[index] - event_time_s)
                break
        if result["recovery_time_s"] is not None:
            break
    if result["recovery_time_s"] is None:
        return result, [diagnostic("voltage_not_recovered", "La tensión no alcanzó recuperación sostenida")]
    return result, []


def power_metrics(time_s, values, *, event_time_s, settling_band_pct):
    raw_time, raw_values = np.asarray(time_s, dtype=float), np.asarray(values, dtype=float)
    time_s, values = finite_samples(raw_time, raw_values)
    result = {"baseline_mw": None, "final_mw": None, "overshoot_pct": None,
              "settling_time_s": None, "settling_known_s": 0.0, "settling_unknown_s": 0.0}
    if not len(values):
        return result, [diagnostic("power_empty", "Potencia sin muestras finitas")]
    if event_time_s is None:
        return result, [diagnostic("event_time_missing", "Potencia transitoria no evaluable sin hora de evento")]
    before = values[time_s < event_time_s]
    after = values[time_s >= event_time_s]
    after_time = time_s[time_s >= event_time_s]
    for i in range(len(raw_time) - 1):
        if raw_time[i] < event_time_s:
            continue
        duration = raw_time[i + 1] - raw_time[i]
        if np.isfinite(raw_values[i]) and np.isfinite(raw_values[i + 1]):
            result["settling_known_s"] += float(duration)
        else:
            result["settling_unknown_s"] += float(duration)
    if not len(before) or not len(after):
        return result, [diagnostic("power_baseline_missing", "Faltan muestras antes o después del evento")]
    baseline = float(np.median(before))
    final = float(np.median(after[max(0, int(len(after) * 0.9)):]))
    amplitude = final - baseline
    result.update(baseline_mw=baseline, final_mw=final)
    if abs(amplitude) <= 1e-12:
        return result, [diagnostic("power_step_zero", "Sobrepaso no evaluable sin escalón de potencia")]
    normalized = (after - baseline) / amplitude
    result["overshoot_pct"] = float(max(0.0, np.max(normalized) - 1.0) * 100.0)
    tolerance_mw = abs(amplitude) * settling_band_pct / 100.0
    settled = np.abs(after - final) <= tolerance_mw
    for index in np.flatnonzero(settled):
        if result["settling_unknown_s"] == 0 and np.all(settled[index:]):
            result["settling_time_s"] = float(after_time[index] - event_time_s)
            break
    if result["settling_time_s"] is None:
        return result, [diagnostic("power_not_settled", "La potencia no se asentó dentro de la ventana disponible")]
    return result, []


def oscillation_metrics(time_s, values, *, event_time_s, event_times=()):
    time_s, values = finite_samples(time_s, values)
    result = {"frequency_hz": None, "damping_ratio": None, "peak_count": 0,
              "cycles_used": None, "window_start_s": None, "window_end_s": None,
              "method": "picos_logaritmicos", "fit_r2": None, "residual_rms": None}
    segments = _continuous_segments(time_s, values, event_times)
    if event_time_s is not None:
        segments = [(x, y) for x, y in segments if x[0] >= event_time_s]
    if not segments:
        return result, [diagnostic("oscillation_window_missing", "Oscilación no evaluable sin tramo continuo")]
    time_s, values = max(segments, key=lambda item: item[0][-1] - item[0][0])
    result["window_start_s"], result["window_end_s"] = float(time_s[0]), float(time_s[-1])
    if len(values) < 5:
        return result, [diagnostic("oscillation_insufficient", "Oscilación no evaluable con menos de cinco muestras")]
    centered = values - np.mean(values[-max(3, len(values) // 5):])
    derivative = np.diff(centered)
    peaks = np.flatnonzero((derivative[:-1] * derivative[1:] <= 0.0)) + 1
    peaks = peaks[np.abs(centered[peaks]) > max(1e-12, 0.02 * np.max(np.abs(centered)))]
    result["peak_count"] = int(len(peaks))
    if len(peaks) < 5:
        return result, [diagnostic("oscillation_peaks_missing", "Oscilación no evaluable sin al menos cinco picos")]
    intervals = np.diff(time_s[peaks])
    intervals = intervals[intervals > 0]
    if not len(intervals):
        return result, [diagnostic("oscillation_period_missing", "Oscilación no evaluable por tiempos repetidos")]
    frequency_hz = 1.0 / (2.0 * float(np.median(intervals)))
    cycles_used = (time_s[peaks][-1] - time_s[peaks][0]) * frequency_hz
    if cycles_used < 2.0:
        return result, [diagnostic("oscillation_cycles_insufficient", "Oscilación no evaluable con menos de dos ciclos")]
    amplitudes = np.abs(centered[peaks])
    log_amplitudes = np.log(amplitudes)
    coefficients = np.polyfit(time_s[peaks] - time_s[peaks][0], log_amplitudes, 1)
    fitted = np.polyval(coefficients, time_s[peaks] - time_s[peaks][0])
    residual = log_amplitudes - fitted
    total = float(np.sum((log_amplitudes - np.mean(log_amplitudes)) ** 2))
    slope = float(coefficients[0])
    sigma = -slope
    omega = 2.0 * math.pi * frequency_hz
    result["frequency_hz"] = frequency_hz
    result["damping_ratio"] = sigma / math.hypot(sigma, omega)
    result["cycles_used"] = float(cycles_used)
    result["fit_r2"] = 1.0 - float(np.sum(residual ** 2)) / total if total > 0 else None
    result["residual_rms"] = float(math.sqrt(np.mean(residual ** 2)))
    return result, []


def _continuous_segments(time_s, values, event_times):
    mask = np.isfinite(time_s) & np.isfinite(values)
    positive = np.diff(time_s[mask])
    positive = positive[positive > 0]
    nominal = float(np.median(positive)) if len(positive) else float("inf")
    segments, start, previous = [], None, None
    for index, (instant_s, valid) in enumerate(zip(time_s, mask)):
        split = previous is not None and (instant_s - previous > 1.5 * nominal or any(previous < event_s <= instant_s for event_s in event_times))
        if not valid or split:
            if start is not None and index - start > 1:
                segments.append((time_s[start:index], values[start:index]))
            start = None
        if valid and start is None:
            start = index
        if valid:
            previous = instant_s
    if start is not None and len(time_s) - start > 1:
        segments.append((time_s[start:], values[start:]))
    return segments


def _masked_duration(time_s, active):
    if len(time_s) < 2:
        return 0.0
    return float(sum((time_s[index + 1] - time_s[index]) for index in range(len(time_s) - 1)
                     if active[index] and active[index + 1]))


def profile_with_overrides(overrides):
    profile = dict(REFERENCE_PROFILE)
    for key, value in overrides.items():
        if key not in profile:
            raise ValueError(f"Campo de perfil no reconocido: {key}")
        if key in ("name", "label"):
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{key} debe ser texto no vacío")
        elif not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise ValueError(f"{key} debe ser numérico y finito")
        profile[key] = value
    return profile


def indicators_for(signal, metrics, profile):
    mapping = {
        "frequency": (("frequency_nadir_min_hz", "nadir_hz", ">=", "Hz"),
                      ("frequency_zenith_max_hz", "zenith_hz", "<=", "Hz"),
                      ("rocof_abs_max_hz_per_s", "rocof_abs_max_hz_per_s", "<=", "Hz/s")),
        "voltage": (("voltage_min_pu", "min_pu", ">=", "p.u."),
                    ("voltage_max_pu", "max_pu", "<=", "p.u."),
                    ("voltage_recovery_max_s", "recovery_time_s", "<=", "s")),
        "oscillation": (("oscillation_damping_min", "damping_ratio", ">=", "1"),),
        "power": (("power_overshoot_max_pct", "overshoot_pct", "<=", "%"),
                  ("power_settling_max_s", "settling_time_s", "<=", "s")),
    }
    rows = []
    for threshold_key, metric_key, relation, unit in mapping[signal["kind"]]:
        value, threshold = metrics.get(metric_key), profile[threshold_key]
        if value is None:
            state = "no evaluable"
        elif relation == ">=":
            state = "dentro del umbral configurado" if value >= threshold else "fuera del umbral configurado"
        else:
            state = "dentro del umbral configurado" if value <= threshold else "fuera del umbral configurado"
        rows.append({
            "indicator_id": f"{signal['signal_id']}:{metric_key}", "signal_id": signal["signal_id"],
            "kind": signal["kind"], "label": signal["label"], "state": state,
            "value": value, "unit": unit, "threshold": threshold,
            "threshold_unit": unit, "profile": profile["name"],
            "note": "referencial, verificar contra la criterios aplicables al estudio, sin verificación normativa",
        })
    return rows
