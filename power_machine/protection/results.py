"""Envoltura común, enlace por extremo shortcircuit y TCC CSV/SVG autónomas."""
from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
import csv
import hashlib
import html
import json
import math
from pathlib import Path
from power_machine.resources import confined_output
import uuid

import numpy as np

from .schema import ProtectionError, diagnostic, number
from .settings import confined_path, WORKSPACE_ROOT


def json_value(value):
    if is_dataclass(value):
        value = asdict(value)
    if isinstance(value, dict):
        return {str(k): json_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list, np.ndarray)):
        return [json_value(v) for v in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, (complex, np.complexfloating)):
        return {"real": number(value.real, "fasor.real"), "imag": number(value.imag, "fasor.imag"), "unit": "A"}
    if isinstance(value, float) and not math.isfinite(value):
        raise ProtectionError("nonfinite_json", "JSON no admite NaN/Infinity")
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(json_value(value), ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def envelope(status, model, study, options, payload, diagnostics=(), *, variant_sha256=None, inputs=(), stages=()):
    formulas = [{"standard": s.curve.source.get("standard"), "edition": s.curve.source.get("edition"),
        "profile": s.curve.family, "object_id": s.object_id, "relative_path": s.relative_path,
        "coefficients": s.curve.coefficients, "coefficient_units": s.curve.source.get("coefficient_units"),
        "time_dial_kind": "TMS" if s.curve.family == "iec" else "TD" if s.curve.family == "ieee" else None,
        "source": s.curve.source} for s in stages]
    return json_value({"schema": "power_machine-parity-result-v1", "module": "protection",
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")+"_"+uuid.uuid4().hex,
        "model_sha256": fingerprint(model), "variant_sha256": variant_sha256,
        "study_sha256": fingerprint(study), "options": options, "status": status,
        "units": {"current_primary_a": "A", "time_s": "s", "margin_s": "s", "ct_ratio": "1"},
        "provenance": {"inputs": list(inputs), "formulas": formulas, "analytical_checks": [],
            "normative_evidence": [], "pf_comparison": "sin validar contra PF"},
        "diagnostics": diagnostics, "payload": payload})


def _phasor_ka(value):
    if not isinstance(value, dict) or value.get("unit") != "kA":
        raise ProtectionError("invalid_sc_units", "Contribución requiere fasor real/imag/unit=kA")
    return complex(number(value.get("real"), "real [kA]"), number(value.get("imag"), "imag [kA]"))


def currents_from_shortcircuit(stages, result, *, model_sha256, study_sha256, variant_sha256=None, terminal_mapping):
    """Usa contribución de rama en puerto protegido. Nunca usa Ik total de falla."""
    if not str(result.get("schema")).endswith("-parity-result-v1") or result.get("module") != "shortcircuit" or result.get("status") not in ("completed", "partial"):
        raise ProtectionError("invalid_shortcircuit_result", "Resultado shortcircuit sin contribuciones utilizables")
    for key, expected in (("model_sha256", model_sha256), ("study_sha256", study_sha256), ("variant_sha256", variant_sha256)):
        if result.get(key) != expected:
            raise ProtectionError("shortcircuit_hash_mismatch", "Contribuciones de otro modelo/estudio/variante")
    currents, diagnostics = {}, []
    for stage in stages:
        if not stage.enabled:
            continue
        try:
            mapping = terminal_mapping.get(stage.object_id)
            if not mapping or not mapping.get("provenance") or not stage.protected_element_id or not stage.terminal_id:
                raise ProtectionError("missing_terminal_mapping", "Enlace CC exige ID, extremo y procedencia", stage)
            matches = [r for r in result["payload"].get("contributions", [])
                       if r.get("kind") == "branch" and r.get("object_id") == stage.protected_element_id]
            if len(matches) != 1:
                raise ProtectionError("missing_terminal_contribution", "Contribución de rama ausente o ambigua", stage)
            terminal = matches[0].get("terminals", {}).get(mapping.get("side"))
            if not terminal or terminal.get("bus_id") != stage.terminal_id:
                raise ProtectionError("terminal_identity_mismatch", "Extremo CC no coincide con terminal protegido", stage)
            if stage.measurement == "phase":
                phase = mapping.get("phase")
                if phase not in ("a", "b", "c"):
                    raise ProtectionError("missing_phase_selection", "Declare fase a/b/c, no se asume el máximo", stage)
                value = _phasor_ka(terminal.get("phase_currents_ka", {}).get(phase))
                conversion = "abs(I_phase_kA)*1000"
            elif stage.measurement == "residual_3i0":
                value = 3 * _phasor_ka(terminal.get("sequence_currents_ka", {}).get("0"))
                conversion = "abs(3*I0_kA)*1000"
            else:
                value = _phasor_ka(terminal.get("neutral_current_ka"))
                conversion = "abs(I_neutral_kA)*1000"
            currents[stage.object_id] = {"object_id": stage.object_id, "relative_path": stage.relative_path,
                "protected_element_id": stage.protected_element_id, "terminal_id": stage.terminal_id,
                "current_primary_a": abs(value)*1000, "measurement": stage.measurement,
                "conversion": conversion, "sc_run_id": result["run_id"], "mapping": mapping}
        except ProtectionError as exc:
            diagnostics.append(exc.diagnostic)
    return currents, diagnostics


def tcc_geometry(series, *, width=900, height=600):
    """Geometría compartida Canvas/SVG. Cero ideal no tiene coordenada logarítmica."""
    valid = [(i, t) for row in series for i, t in row["points"]
             if i is not None and t is not None and i > 0 and t > 0 and math.isfinite(i) and math.isfinite(t)]
    if not valid:
        return {"width": width, "height": height, "series": [], "ticks_x": [], "ticks_y": [], "omitted": sum(len(s["points"]) for s in series)}
    low_x, high_x = min(math.log10(i) for i, _ in valid), max(math.log10(i) for i, _ in valid)
    low_y, high_y = min(math.log10(t) for _, t in valid), max(math.log10(t) for _, t in valid)
    if high_x == low_x:
        high_x += 0.5
        low_x -= 0.5
    if high_y == low_y:
        high_y += 0.5
        low_y -= 0.5
    sx = lambda i: 80 + (math.log10(i)-low_x)/(high_x-low_x)*(width-300)
    sy = lambda t: height-65 - (math.log10(t)-low_y)/(high_y-low_y)*(height-130)
    curves, omitted = [], 0
    for index, row in enumerate(series):
        segments, segment = [], []
        for current, time in row["points"]:
            if current is None or time is None or current <= 0 or time <= 0 or not math.isfinite(current) or not math.isfinite(time):
                if segment:
                    segments.append(segment)
                    segment = []
                omitted += 1
            else:
                segment.append((sx(current), sy(time)))
        if segment:
            segments.append(segment)
        curves.append({"label": row.get("label", row["object_id"]), "object_id": row["object_id"],
                       "segments": segments, "dash": ((), (6, 3), (2, 3), (9, 3, 2, 3))[index % 4]})
    return {"width": width, "height": height, "series": curves, "omitted": omitted,
        "ticks_x": [(sx(10**n), f"{10**n:g}") for n in range(math.ceil(low_x), math.floor(high_x)+1)],
        "ticks_y": [(sy(10**n), f"{10**n:g}") for n in range(math.ceil(low_y), math.floor(high_y)+1)]}


def export_points_csv(series, path):
    path = confined_path(WORKSPACE_ROOT, path)
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(("object_id", "relative_path", "current", "current_unit", "time_s", "operating"))
        for row in series:
            for current, time in row["points"]:
                writer.writerow((row["object_id"], row.get("relative_path"), current, row.get("current_unit", "A_primary"),
                                 time, time is not None))


def export_svg(series, path):
    units = {s.get("current_unit", "A_primary") for s in series}
    if len(units) != 1:
        raise ProtectionError("mixed_tcc_units", "No superponer unidades secundarias y primarias")
    geometry = tcc_geometry(series)
    content = ['<svg xmlns="http://www.w3.org/2000/svg" width="900" height="600" viewBox="0 0 900 600">',
        '<rect width="900" height="600" fill="white"/>',
        '<path d="M80 65 V535 H680" fill="none" stroke="black"/>']
    for x, label in geometry["ticks_x"]:
        content.append(f'<text x="{x:.6f}" y="555" text-anchor="middle">{label}</text>')
    for y, label in geometry["ticks_y"]:
        content.append(f'<text x="70" y="{y:.6f}" text-anchor="end">{label}</text>')
    for idx, row in enumerate(geometry["series"]):
        dash = ' stroke-dasharray="'+','.join(str(n) for n in row["dash"])+ '"' if row["dash"] else ''
        for segment in row["segments"]:
            pts = " ".join(f"{x:.6f},{y:.6f}" for x, y in segment)
            content.append(f'<polyline points="{pts}" fill="none" stroke="black"{dash}/>')
        content.append(f'<text x="700" y="{85+idx*22}">{html.escape(str(row["label"]))}</text>')
    label = "Corriente primaria [A]" if units == {"A_primary"} else "Corriente secundaria [A], plantilla"
    content.extend([f'<text x="330" y="585">{label}, log10</text>',
        '<text x="20" y="35">Tiempo [s], log10</text>',
        f'<text x="80" y="25">Sin validar contra PF. Puntos sin posición logarítmica: {geometry["omitted"]}</text>', '</svg>'])
    with confined_path(WORKSPACE_ROOT, path).open("x", encoding="utf-8") as stream:
        stream.write("\n".join(content)+"\n")


def write_result(root, result, *, historical=False):
    run_id = result["run_id"]
    if not run_id or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in run_id):
        raise ProtectionError("invalid_run_id", "Identificador de corrida inválido")
    folder = "results/archive/protection" if historical else "results/parity/protection"
    directory = confined_output(Path(root).expanduser().resolve() / folder / run_id, root)
    payload = json.dumps(json_value(result), ensure_ascii=False, indent=2, allow_nan=False)+"\n"
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir(exist_ok=False)
    temp = directory/"result.json.tmp"
    with temp.open("x", encoding="utf-8") as stream:
        stream.write(payload)
    temp.replace(directory/"result.json")
    return directory/"result.json"
