"Explicit data, numerical methods and traceable results for shortcircuit studies."
import math

from .schema import SCError


def voltage_factor(un_kv, case, lv_tolerance_pct=None):
    if case not in ("maximum", "minimum"):
        raise SCError("invalid_case", "case must be maximum or minimum")
    if un_kv > 1:
        return 1.10 if case == "maximum" else 1.00
    if lv_tolerance_pct not in (6, 10):
        raise SCError("missing_sc_data", "Low voltage requires a documented +6% or +10% tolerance")
    return (1.05 if lv_tolerance_pct == 6 else 1.10) if case == "maximum" else 0.95


def transformer_factor(c_max, x_t_own_pu):
    if x_t_own_pu < 0:
        raise SCError("invalid_sc_value", "Negative transformer self reactance")
    return 0.95 * c_max / (1.0 + 0.6 * x_t_own_pu)


def generator_factor(un_kv, rated_kv, c_max, xd_sub_pu, sin_phi_rated):
    if rated_kv <= 0 or xd_sub_pu <= 0 or abs(sin_phi_rated) > 1:
        raise SCError("invalid_sc_value", "Invalid rated data for K_G")
    return (un_kv / rated_kv) * c_max / (1 + xd_sub_pu * sin_phi_rated)


def resistance_at_temperature(r20_ohm, alpha20_per_c, temperature_c):
    r = r20_ohm * (1 + alpha20_per_c * (temperature_c - 20))
    if r < 0:
        raise SCError("nonphysical_resistance", "Negative temperature-corrected resistance")
    return r


def peak_factor(r_ohm, x_ohm):
    if r_ohm < 0 or x_ohm < 0 or (r_ohm == 0 and x_ohm == 0):
        raise SCError("unsupported_peak_profile", "R/X is outside the radial peak domain", status="unsupported")
    return 1.02 if x_ohm == 0 else 1.02 + 0.98 * math.exp(-3 * r_ohm / x_ohm)


def decay_row(time_s, evidence=()):
    times = (0.02, 0.05, 0.10, 0.25)
    for i, t in enumerate(times):
        if math.isclose(time_s, t, rel_tol=0, abs_tol=1e-12):
            return i
    if time_s > 0.25 and evidence:
        return 3
    raise SCError("unsupported_break_time", "Break time has no supported normative rule", status="unsupported")


def synchronous_decay(ratio, time_s, evidence=()):
    i = decay_row(time_s, evidence)
    if ratio < 0:
        raise SCError("invalid_sc_value", "Negative Ik/Ir")
    if ratio <= 2:
        return 1.0
    a, b, c = ((0.84, 0.26, 0.26), (0.71, 0.51, 0.30),
               (0.62, 0.72, 0.32), (0.56, 0.94, 0.38))[i]
    return a + b * math.exp(-c * ratio)


def motor_decay(power_mw, pole_pairs, time_s, evidence=()):
    i = decay_row(time_s, evidence)
    if power_mw <= 0 or pole_pairs <= 0:
        raise SCError("invalid_motor_power", "Rated mechanical power and pole pairs must be positive")
    a, b = ((1.03, 0.12), (0.79, 0.12), (0.57, 0.12), (0.26, 0.10))[i]
    q = a + b * math.log(power_mw / pole_pairs)
    if q < 0:
        raise SCError("unsupported_motor_decay", "Negative q. The motor profile does not apply", status="unsupported")
    return min(1.0, q)


def thermal_factor(kappa, frequency_hz, duration_s):
    "Return m for constant AC with n=1, handling the kappa=2 limit without division by zero."
    if duration_s <= 0 or frequency_hz <= 0 or not 1 < kappa <= 2:
        raise SCError("invalid_thermal_input", "T/f must be positive and 1<kappa<=2")
    if kappa == 2:
        return 2.0
    q = 4 * frequency_hz * duration_s * math.log(kappa - 1)
    return 2 * math.expm1(q) / q
