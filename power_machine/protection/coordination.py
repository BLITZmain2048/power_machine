"""Margen estático de sobrecorriente, sin afirmar selectividad universal o RMS."""
from __future__ import annotations

import math
import numpy as np

from .curves import operation, sampled_points
from .schema import CoordinationResult, ProtectionError, diagnostic, number


def cancelled(cancel):
    return cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else bool(cancel()))


def current_grid(stages, minimum_a, maximum_a, *, points_per_decade=400):
    """Malla [A primarios] con al menos 400 intervalos/década y umbrales exactos y laterales."""
    low = number(minimum_a, "minimum_a [A]", positive=True)
    high = number(maximum_a, "maximum_a [A]", positive=True)
    if high <= low or isinstance(points_per_decade, bool) or not isinstance(points_per_decade, int) or points_per_decade < 400:
        raise ProtectionError("invalid_grid", "Rango creciente y al menos 400 puntos por década")
    count = int(math.ceil(math.log10(high/low)*points_per_decade)) + 1
    if count > 200000:
        raise ProtectionError("grid_budget", "Malla supera presupuesto de 200000 puntos")
    values = list(np.geomspace(low, high, count))
    for stage in stages:
        boundaries = []
        if stage.pickup_primary_a is not None and math.isfinite(stage.pickup_primary_a):
            boundaries.append(stage.pickup_primary_a)
        if stage.curve.family == "sampled":
            boundaries.extend(x for x, _ in sampled_points(stage))
        for boundary in boundaries:
            values.extend(x for x in (np.nextafter(boundary, 0), boundary,
                          np.nextafter(boundary, math.inf)) if low <= x <= high)
    return np.unique(values)


def pair_point(primary, backup, pair, current_primary_a, current_backup_a=None):
    """Corrientes propias por etapa, iguales solo al solicitar una malla común explícita."""
    current_backup_a = current_primary_a if current_backup_a is None else current_backup_a
    p, pd = operation(primary, current_primary_a)
    b, bd = operation(backup, current_backup_a)
    covered = p["operating"] and b["operating"]
    margin = b["time_s"] - (p["time_s"] + pair.breaker_time_s) if covered else None
    return {"current_primary_a": current_primary_a, "current_backup_a": current_backup_a,
        "primary_time_s": p["time_s"], "backup_time_s": b["time_s"], "margin_s": margin,
        "covered": covered, "pass": margin >= pair.required_margin_s if covered else None,
        "reason": None if covered else {"primary": p["reason"], "backup": b["reason"]}}, [d for d in (pd, bd) if d]


def _crossings(primary, backup, pair, rows):
    """Bisección solo en intervalos continuos, precisión relativa de corriente 1e-7."""
    discontinuities = {s.pickup_primary_a for s in (primary, backup) if s.pickup_primary_a is not None}
    for stage in (primary, backup):
        if stage.curve.family == "sampled" and stage.curve.coefficients["interpolation"] != "log_log":
            discontinuities.update(x for x, _ in sampled_points(stage))
    roots, jumps = [], []
    for left, right in zip(rows, rows[1:]):
        if not left["covered"] or not right["covered"]:
            continue
        lo, hi = left["current_primary_a"], right["current_primary_a"]
        fl, fh = left["margin_s"]-pair.required_margin_s, right["margin_s"]-pair.required_margin_s
        if fl == 0:
            roots.append(lo)
        if fh == 0:
            roots.append(hi)
        if fl * fh >= 0:
            continue
        if any(lo <= x <= hi for x in discontinuities):
            jumps.append({"from_a": lo, "to_a": hi, "reason": "discontinuous_threshold"})
            continue
        for _ in range(80):
            middle = (lo+hi)/2
            row, _ = pair_point(primary, backup, pair, middle)
            fm = row["margin_s"]-pair.required_margin_s
            if fm == 0 or hi-lo <= middle*1e-7:
                lo = hi = middle
                break
            if fl*fm < 0:
                hi = middle
            else:
                lo, fl = middle, fm
        roots.append((lo+hi)/2)
    return sorted(set(roots)), jumps


def coordinate(stages, pairs, currents_primary_a: np.ndarray, *, cancel=None, progress=None):
    currents = np.asarray(currents_primary_a, dtype=float)
    if currents.ndim != 1 or currents.size == 0 or not np.all(np.isfinite(currents)) or np.any(currents <= 0) or np.any(np.diff(currents) <= 0):
        raise ProtectionError("invalid_grid", "Malla de corriente primaria positiva y estrictamente creciente")
    stage_index = {s.object_id: s for s in stages}
    if len(stage_index) != len(stages):
        raise ProtectionError("duplicate_stage_id", "IDs de etapa duplicados")
    output, diagnostics = [], []
    for index, pair in enumerate(pairs):
        number(pair.breaker_time_s, "breaker_time_s [s]", nonnegative=True)
        number(pair.required_margin_s, "required_margin_s [s]", nonnegative=True)
        if pair.primary_stage_id not in stage_index or pair.backup_stage_id not in stage_index:
            raise ProtectionError("missing_pair_stage", "Par refiere una etapa ausente")
        primary, backup = stage_index[pair.primary_stage_id], stage_index[pair.backup_stage_id]
        if primary.measurement != backup.measurement:
            raise ProtectionError("incompatible_measurements", "Par mezcla medidas distintas", primary)
        rows = []
        for current in currents:
            if cancelled(cancel):
                return CoordinationResult("cancelled", tuple(currents), tuple(output), tuple(diagnostics))
            row, diags = pair_point(primary, backup, pair, float(current))
            rows.append(row)
            diagnostics.extend(diags)
        valid = [r for r in rows if r["covered"]]
        minimum = min(valid, key=lambda r: r["margin_s"]) if valid else None
        crossings, jumps = _crossings(primary, backup, pair, rows)
        output.append({"primary_stage_id": primary.object_id, "backup_stage_id": backup.object_id,
            "breaker_time_s": pair.breaker_time_s, "required_margin_s": pair.required_margin_s,
            "rows": rows, "minimum_margin_s": minimum["margin_s"] if minimum else None,
            "minimum_current_a": minimum["current_primary_a"] if minimum else None,
            "crossings_primary_a": crossings, "threshold_transitions": jumps,
            "coverage": {"evaluated": len(rows), "covered": len(valid), "uncovered": len(rows)-len(valid)},
            "grid_margin_pass": all(r["pass"] for r in valid) if valid else None,
            "scope": "explicit_current_grid_only", "extrapolations": []})
        if progress:
            progress({"stage": "coordination", "pairs_done": index+1, "pairs_total": len(pairs)})
    unique = {tuple(d.items()): d for d in diagnostics}
    partial = bool(unique) or any(p["coverage"]["uncovered"] for p in output)
    return CoordinationResult("partial" if partial else "completed", tuple(float(x) for x in currents),
                              tuple(output), tuple(unique.values()))
