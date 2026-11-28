'Classical ref support for power-system modal analysis.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass
import numpy as np

from ..loadflow import _stamp


@dataclass
class ClassicalModel:
    'Interleaved [nu, delta] states in a frame relative to the reference rotor.'

    a: np.ndarray
    y_reduced: np.ndarray
    y_augmented: np.ndarray
    bus_voltage_pu: np.ndarray
    internal_voltage_pu: np.ndarray
    h_system_s: np.ndarray
    pm_system_pu: np.ndarray
    x0: np.ndarray
    states: list[dict]
    machines: list[dict]
    reference_index: int
    omega_nom_rad_s: float
    mechanical_convention: str

    def electrical_power_pu(self, delta_rad):
        'Return injected electrical power at internal nodes on the system base.'
        e = abs(self.internal_voltage_pu) * np.exp(1j * np.asarray(delta_rad))
        return (e * np.conj(self.y_reduced @ e)).real

    def derivative(self, x):
        'Return speed deviation in p.u./s and angle in rad/s. Mechanical power is constant and damping is explicitly zero. ref_native uses Tm=Pm/(1+nu), Te=Pe. ideal_power divides both torques by speed.'
        x = np.asarray(x, dtype=float)
        nu, delta = x[0::2], x[1::2]
        if x.shape != self.x0.shape or np.any(1 + nu <= 0):
            raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Invalid classical state')
        pe = self.electrical_power_pu(delta)
        result = np.empty_like(x)
        te = pe if self.mechanical_convention == "ref_native" else pe / (1 + nu)
        result[0::2] = (self.pm_system_pu / (1 + nu) - te) / (2 * self.h_system_s)
        result[1::2] = self.omega_nom_rad_s * (nu - nu[self.reference_index])
        return result


def build_classical_model(model, loadflow, native_types, configuration, *,
                          mechanical_convention="ref_native"):
    'Build the complete network and a 2N-state swing model without fitting parameters. Hsys=Hmachine*Sgn*ngnum/Sbase. Convert x-prime to system MVA and bus-voltage bases. Use consumed-load admittance conj(S)/abs(V_LF)^2. Require the explicitly supported connected network profile.'
    if mechanical_convention not in ("ref_native", "ideal_power"):
        raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Unsupported mechanical convention')
    if not loadflow["converged"]:
        raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Load flow must converge before modal analysis')
    if model.get("branches3") or model.get("switches") or model.get("shunts"):
        raise _coded_error(ValueError, 'invalid_classical_ref_data', 'IEEE39 classical profile excludes three-winding branches, switches and external shunts')
    buses = model["buses"]
    by_id = {b["id"]: b for b in buses}
    bus_idx = {b["id"]: i for i, b in enumerate(buses)}
    if any(b.get("outserv") for b in buses):
        raise _coded_error(ValueError, 'invalid_classical_ref_data', 'This profile does not support out-of-service buses')
    generators = sorted(model["gens"], key=lambda g: g["relative_path"])
    if any(g.get("outserv") for g in generators):
        raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Out-of-service machines are not supported')
    ref = [i for i, g in enumerate(generators) if g["ref"]]
    if len(ref) != 1:
        raise _coded_error(ValueError, 'invalid_classical_ref_data', 'A rotor reference is required')
    reference_index = ref[0]
    s_base = float(model["meta"]["s_base_mva"])
    omega_nom = 2 * np.pi * float(model["meta"]["f_nom_hz"])
    if s_base <= 0 or omega_nom <= 0:
        raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Bases must be positive')
    lf_bus = {b["id"]: b for b in loadflow["buses"]}
    lf_gen = {g["id"]: g for g in loadflow["gens"]}
    voltage = np.array([lf_bus[b["id"]]["u_pu"] * np.exp(
        1j * np.deg2rad(lf_bus[b["id"]]["angle_deg"])) for b in buses])
    nbus, ngen = len(buses), len(generators)
    y = np.zeros((nbus + ngen, nbus + ngen), complex)
    for branch in model["branches"]:
        if branch.get("outserv"):
            continue
        ids, _, matrix = _stamp(branch, by_id, s_base)
        idx = [bus_idx[k] for k in ids]
        y[np.ix_(idx, idx)] += matrix
    for load in model["loads"]:
        if not load.get("outserv"):
            k = bus_idx[load["bus"]]
            consumption = complex(load["p_mw"], load["q_mvar"]) * load["scale"] / s_base
            y[k, k] += consumption.conjugate() / abs(voltage[k]) ** 2
    machines, states, e, hsys, pm = [], [], [], [], []
    for i, gen in enumerate(generators):
        native = native_types[gen["type_id"]]
        cfg = configuration[gen["path"]]
        for key in ("h", "sgn", "xds"):
            value = native[key]["value"]
            cfg_key = "xstr" if key == "xds" else key
            if value is None or cfg[cfg_key]["value"] != value:
                raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Classical trial does not preserve native data ' + key)
        if cfg["model_inp"]["value"] != "cls" or cfg["i_speedVar"]["value"] != 2:
            raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Incompatible classical configuration')
        if any(cfg[k]["value"] != 0 for k in ("rstr", "dkd", "dpe", "dpu")):
            raise _coded_error(ValueError, 'missing_classical_ref_data', 'Classical profile requires explicit ra=0 and D=0')
        if native["h"]["unit"] != "s" or native["sgn"]["unit"] != "MVA":
            raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Unrecognized inertia or power units')
        if native["xds"]["unit"] != "p.u." or native["ugn"]["unit"] != "kV":
            raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Unrecognized reactance or voltage units')
        units = float(gen["params"]["ngnum"])
        rating = float(native["sgn"]["value"]) * units
        h = float(native["h"]["value"])
        k = bus_idx[gen["bus"]]
        x = float(native["xds"]["value"]) * s_base / rating * (
            float(native["ugn"]["value"]) / by_id[gen["bus"]]["un_kv"]) ** 2
        if min(h, rating, x) <= 0:
            raise _coded_error(ValueError, 'invalid_classical_ref_data', 'Inertia, power and reactance must be positive')
        power = complex(lf_gen[gen["id"]]["p_mw"], lf_gen[gen["id"]]["q_mvar"]) / s_base
        internal = voltage[k] + 1j * x * np.conj(power / voltage[k])
        e.append(internal)
        hsys.append(h * rating / s_base)
        pm.append(power.real)
        admittance = 1 / (1j * x)
        y[k, k] += admittance
        y[nbus+i, nbus+i] += admittance
        y[k, nbus+i] -= admittance
        y[nbus+i, k] -= admittance
        identity = {"object_id": gen["object_id"], "relative_path": gen["relative_path"]}
        machines.append({**identity, "name": gen["name"], "type_path": gen["type_id"],
                         "h_machine_s": h, "rating_total_mva": rating,
                         "h_system_s": hsys[-1], "xd_prime_system_pu": x,
                         "pm_system_pu": pm[-1], "d_explicit": 0.0,
                         "torque_speed_derivative_per_s": -pm[-1] / (2 * hsys[-1])})
        for variable, unit in (("nu", "p.u."), ("delta", "rad")):
            states.append({**identity, "index": len(states), "variable": variable, "unit": unit})
    e, hsys, pm = np.array(e), np.array(hsys), np.array(pm)
    reduced = y[nbus:, nbus:] - y[nbus:, :nbus] @ np.linalg.solve(y[:nbus, :nbus], y[:nbus, nbus:])
    coupling = e[:, None] * np.conj(reduced * e[None, :])
    stiffness = coupling.imag.copy()
    np.fill_diagonal(stiffness, 0)
    np.fill_diagonal(stiffness, -stiffness.sum(axis=1))
    a = np.zeros((2 * ngen, 2 * ngen))
    a[0::2, 1::2] = -stiffness / (2 * hsys[:, None])
    a[1::2, 0::2] = omega_nom * np.eye(ngen)
    a[1::2, 2 * reference_index] -= omega_nom
    if mechanical_convention == "ref_native":
        a[0::2, 0::2] = np.diag(-pm / (2 * hsys))
    else:
        pe = (e * np.conj(reduced @ e)).real
        a[0::2, 0::2] = np.diag((pe - pm) / (2 * hsys))
    x0 = np.zeros(2 * ngen)
    x0[1::2] = np.angle(e) - np.angle(e[reference_index])
    return ClassicalModel(a, reduced, y, voltage, e, hsys, pm, x0,
                          states, machines, reference_index, omega_nom, mechanical_convention)
