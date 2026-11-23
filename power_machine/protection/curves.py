"""Perfiles del estudio, sin sustituciones implícitas de curvas de fabricante."""
from __future__ import annotations

import bisect
import math

from .schema import CurveSpec, RelayStage, ProtectionError, number

IEC = {"normal_inverse": (0.14, 0.02), "very_inverse": (13.5, 1.0),
       "extremely_inverse": (80.0, 2.0), "long_inverse": (120.0, 1.0)}
IEEE = {"moderately_inverse": (0.0515, 0.1140, 0.02),
        "very_inverse": (19.61, 0.4910, 2.0), "extremely_inverse": (28.2, 0.1217, 2.0)}


def standard_curve(family, name):
    """Selección explícita del perfil IEC 2009 o IEEE 2018, coeficientes adimensionales."""
    if family == "iec" and name in IEC:
        coefficients = dict(zip(("K", "alpha"), IEC[name]))
        standard, edition = "IEC 60255-151", "2009"
    elif family == "ieee" and name in IEEE:
        coefficients = dict(zip(("A", "B", "p"), IEEE[name]))
        standard, edition = "IEEE C37.112", "2018"
    else:
        raise ProtectionError("unknown_curve", "Perfil y nombre de curva desconocidos")
    return CurveSpec(family, name, coefficients, {"standard": standard, "edition": edition,
        "profile": "protection", "coefficient_units": {k: "s" if k in ("K", "A", "B") else "1" for k in coefficients}, "time_unit": "s",
        "normative_status": "pending", "origin": "protection_profile"})


def primary_pickup(pickup_secondary_a, ct_primary_rated_a, ct_secondary_rated_a):
    """Convierte A secundarios a A primarios, ratio = nominal primario / secundario."""
    pickup = number(pickup_secondary_a, "pickup_secondary_a [A]", positive=True)
    ratio = number(ct_primary_rated_a, "ct_primary_rated_a [A]", positive=True) / number(
        ct_secondary_rated_a, "ct_secondary_rated_a [A]", positive=True)
    return pickup * ratio, ratio


def residual_current(i0_a):
    """Conversión explícita de magnitud I0 [A] a residual 3I0 [A]."""
    return 3 * number(i0_a, "i0_a [A]", nonnegative=True)


def sampled_points(stage):
    spec = stage.curve.coefficients
    if spec.get("current_unit") != "A_primary" or spec.get("time_unit") != "s":
        raise ProtectionError("invalid_sample_units", "Sampled exige A_primary y s", stage)
    points = spec.get("points", [])
    if len(points) < 2:
        raise ProtectionError("missing_samples", "Se requieren al menos dos puntos", stage)
    output = []
    for current, time in points:
        output.append((number(current, "sample_current_a", positive=True, stage=stage),
                       number(time, "sample_time_s", positive=True, stage=stage)))
    if any(b[0] <= a[0] for a, b in zip(output, output[1:])):
        raise ProtectionError("unordered_samples", "Abscisas sampled deben ser estrictamente crecientes", stage)
    if spec.get("interpolation") not in ("log_log", "left_step", "right_step"):
        raise ProtectionError("missing_step_semantics", "Declare interpolación log_log o semántica de escalón", stage)
    if spec.get("extrapolate", False):
        raise ProtectionError("unsupported_extrapolation", "Este perfil prohíbe extrapolación", stage)
    return output


def validate_stage(stage: RelayStage):
    """No inventa TC, dial, curva ni dirección cuando faltan en el origen."""
    if stage.measurement not in ("phase", "residual_3i0", "neutral"):
        raise ProtectionError("unsupported_measurement", "Medida no cubierta", stage)
    source = stage.curve.source
    if source.get("directional") and source.get("direction_allowed") is not True:
        raise ProtectionError("missing_direction", "Etapa direccional requiere criterio aportado", stage)
    if source.get("requires_ct") and stage.ct_ratio is None:
        raise ProtectionError("missing_ct", "Conversión secundaria requiere TC explícito", stage)
    if stage.ct_ratio is not None:
        number(stage.ct_ratio, "ct_ratio [1]", positive=True, stage=stage)
    family, coefficients = stage.curve.family, stage.curve.coefficients
    if family == "sampled":
        sampled_points(stage)
        if stage.pickup_primary_a is not None:
            number(stage.pickup_primary_a, "pickup_primary_a [A]", positive=True, stage=stage)
    else:
        number(stage.pickup_primary_a, "pickup_primary_a [A]", positive=True, stage=stage)
    if family in ("iec", "ieee"):
        number(stage.time_dial, "TMS" if family == "iec" else "TD", positive=True, stage=stage)
        keys = ("K", "alpha") if family == "iec" else ("A", "B", "p")
        if set(coefficients) != set(keys):
            raise ProtectionError("unknown_coefficients", "Coeficientes explícitos incompletos o desconocidos", stage)
        for key in keys:
            number(coefficients[key], key, positive=key != "B", nonnegative=key == "B", stage=stage)
    elif family == "definite":
        number(stage.definite_delay_s, "definite_delay_s [s]", nonnegative=True, stage=stage)
    elif family not in ("instantaneous", "sampled"):
        raise ProtectionError("unsupported_family", "Familia de curva fuera del perfil", stage, status="unsupported")
    for key in ("time_adder_s", "pickup_time_addition_s", "minimum_time_s", "maximum_time_s"):
        if key in source:
            number(source[key], key, nonnegative=True, stage=stage)
    if source.get("minimum_time_s", 0) > source.get("maximum_time_s", math.inf):
        raise ProtectionError("invalid_time_limits", "Límite mínimo mayor al máximo", stage)


def operating_time(stage: RelayStage, current_primary_a: float) -> float | None:
    """Tiempo ideal/ajustado [s], None significa sin operación o fuera de dominio."""
    current = number(current_primary_a, "current_primary_a [A]", nonnegative=True, stage=stage)
    if not stage.enabled:
        return None
    validate_stage(stage)
    family, coefficients = stage.curve.family, stage.curve.coefficients
    if family == "sampled":
        points = sampled_points(stage)
        if current < points[0][0] or current > points[-1][0]:
            return None
        if stage.pickup_primary_a is not None and current <= stage.pickup_primary_a:
            return None
        xs = [p[0] for p in points]
        idx = bisect.bisect_left(xs, current)
        if idx < len(points) and points[idx][0] == current:
            return points[idx][1]
        lo, hi = points[idx-1], points[idx]
        semantics = coefficients["interpolation"]
        if semantics == "left_step":
            return lo[1]
        if semantics == "right_step":
            return hi[1]
        weight = math.log(current/lo[0]) / math.log(hi[0]/lo[0])
        return math.exp(math.log(lo[1]) + weight * math.log(hi[1]/lo[1]))
    pickup = stage.pickup_primary_a
    if family in ("definite", "instantaneous"):
        if current < pickup:
            return None
        time = stage.definite_delay_s if family == "definite" else 0.0
    else:
        if current <= pickup:
            return None
        log_m = math.log(current/pickup)
        if family == "iec":
            time = stage.time_dial * coefficients["K"] / math.expm1(coefficients["alpha"]*log_m)
        else:
            time = stage.time_dial * (coefficients["A"] / math.expm1(coefficients["p"]*log_m) + coefficients["B"])
    source = stage.curve.source
    time += source.get("time_adder_s", 0.0)
    time = max(source.get("minimum_time_s", 0.0), time)
    if "maximum_time_s" in source:
        time = min(source["maximum_time_s"], time)
    time += source.get("pickup_time_addition_s", 0.0)
    return number(time, "operating_time_s [s]", nonnegative=True, stage=stage)


def operation(stage, current_primary_a):
    """Evaluación diagnosticable, ausentes nunca se convierten en infinito ni cero."""
    try:
        time = operating_time(stage, current_primary_a)
        reason = None
        if time is None:
            reason = "disabled" if not stage.enabled else "outside_domain" if stage.curve.family == "sampled" else "below_pickup"
        return {"object_id": stage.object_id, "relative_path": stage.relative_path,
                "current_primary_a": current_primary_a, "time_s": time,
                "operating": time is not None, "reason": reason, "extrapolated": False}, None
    except (ProtectionError, OverflowError) as exc:
        if isinstance(exc, OverflowError):
            exc = ProtectionError("numerical_overflow", "Fórmula fuera del rango numérico", stage)
        return {"object_id": stage.object_id, "relative_path": stage.relative_path,
                "current_primary_a": None, "time_s": None, "operating": False,
                "reason": exc.diagnostic["code"], "extrapolated": False}, exc.diagnostic
