"Explicit data, numerical methods and traceable results for shortcircuit studies."
import math

import numpy as np

from .schema import SCError


def rl_parameters(r_ohm, x_ohm, frequency_hz):
    if r_ohm < 0 or x_ohm < 0 or frequency_hz <= 0 or r_ohm + x_ohm == 0:
        raise SCError("invalid_rl_data", "RL requires nonnegative R/X, nonzero Z and positive frequency")
    omega = 2 * math.pi * frequency_hz
    tau = None if r_ohm == 0 else x_ohm / (omega * r_ohm)
    return math.atan2(x_ohm, r_ohm), tau, omega


def rl_current_ka(time_s, ik_ka, r_ohm, x_ohm, frequency_hz=50.0, alpha_rad=0.0):
    "Return instantaneous RL current in kA including the initial DC component."
    phi, tau, omega = rl_parameters(r_ohm, x_ohm, frequency_hz)
    t = np.asarray(time_s, dtype=float)
    if not np.all(np.isfinite(t)) or np.any(t < 0):
        raise SCError("invalid_rl_time", "RL time must be finite and nonnegative")
    dc = np.ones_like(t) if tau is None else (np.where(t == 0, 1.0, 0.0) if tau == 0 else np.exp(-t / tau))
    return math.sqrt(2) * ik_ka * (np.sin(omega * t + alpha_rad - phi) - math.sin(alpha_rad - phi) * dc)


def rl_window_rms_ka(ik_ka, r_ohm, x_ohm, frequency_hz, alpha_rad, t1_s, t2_s, steps=4096):
    "Integrate by trapezoidal quadrature and check h/2. This is not a breaker-duty rating."
    if not 0 <= t1_s < t2_s or steps < 32 or steps > 1048576:
        raise SCError("invalid_rl_window", "Invalid RL window or steps outside 32..1048576")
    values = []
    for n in (steps, steps * 2):
        t = np.linspace(t1_s, t2_s, n + 1)
        i = rl_current_ka(t, ik_ka, r_ohm, x_ohm, frequency_hz, alpha_rad)
        area = (np.sum(i[1:-1]**2) + (i[0]**2 + i[-1]**2) / 2) * (t2_s - t1_s) / n
        values.append(math.sqrt(float(area) / (t2_s - t1_s)))
    relative_change = abs(values[1] - values[0]) / max(values[1], 1e-300)
    if relative_change >= 1e-5:
        raise SCError("rl_quadrature_not_converged", "h/2 changes RMS by at least 1e-5 relative", status="numerical_failure")
    return {"rms_window_ka": values[1], "quadrature_relative_change": relative_change,
            "steps": steps * 2, "t1_s": t1_s, "t2_s": t2_s}
