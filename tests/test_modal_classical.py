"""Propiedades del swing clásico y SMIB, sin validar contra la referencia externa.

Fixtures propias de red recíproca sin pérdidas y fuerzas en base 100 MVA.
δ está en radianes y ν es desviación de velocidad en p.u. de 50 Hz.
Las máquinas conservan su velocidad, incluida la referencia angular.
"""
import copy
from dataclasses import replace
import json

import numpy as np
import pytest

from power_machine.modal import (ModalOptions, OperatingPoint, StateRef, linearize,
                             reduce_angle_reference, solve_modes)
from power_machine.modal.eigen import match_eigenvalues


class ClassicalMachines:
    """δ'=ωs ν, 2Hsys ν'=Pm-Pe-Dsys ν, potencia inyectada positiva.

    Hsys=Hmaq Smaq/Sbase en segundos, Dsys en p.u. potencia/p.u. velocidad.
    Pe_i=sum_j E_i E_j B_ij sin(δ_i-δ_j), B_ij>0 en p.u. de sistema.
    """
    def __init__(self, inertia_s, coupling_pu, damping_pu, delta_rad):
        self.inertia_s = inertia_s
        self.coupling_pu = coupling_pu
        self.damping_pu = damping_pu
        self.omega_nom_rad_per_s = 2*np.pi*50.
        self.p_mechanical_pu = self.electrical_power(delta_rad)

    def electrical_power(self, delta_rad):
        return np.sum(self.coupling_pu*np.sin(delta_rad[:, None]-delta_rad[None, :]), axis=1)

    def clone(self):
        return copy.deepcopy(self)

    def evaluate(self, x, z, time_s):
        n = len(self.inertia_s)
        delta_rad, speed_deviation_pu = x[:n], x[n:]
        acceleration_pu_per_s = (self.p_mechanical_pu-self.electrical_power(delta_rad)
                                 -self.damping_pu*speed_deviation_pu)/(2*self.inertia_s)
        return np.r_[self.omega_nom_rad_per_s*speed_deviation_pu, acceleration_pu_per_s], np.empty(0)


def classical_fixture(n, seed):
    rng = np.random.default_rng(seed)
    s_base_mva = 100.
    s_machine_mva = rng.uniform(80., 220., n)
    h_machine_s = rng.uniform(3., 6., n)
    h_system_s = h_machine_s*s_machine_mva/s_base_mva
    e_pu = rng.uniform(.95, 1.1, n)
    susceptance_pu = rng.uniform(.5, 2., (n, n))
    susceptance_pu = np.triu(susceptance_pu, 1)
    susceptance_pu += susceptance_pu.T
    coupling_pu = e_pu[:, None]*e_pu[None, :]*susceptance_pu
    delta_rad = rng.uniform(-.25, .25, n)
    stiffness_pu_per_rad = -coupling_pu*np.cos(delta_rad[:, None]-delta_rad[None, :])
    np.fill_diagonal(stiffness_pu_per_rad, -np.sum(stiffness_pu_per_rad, axis=1))
    states = tuple(StateRef(i, f"classic_g{i % n}", f"analytical/classic/g{i % n}",
                            "delta_rad" if i < n else "speed_deviation_pu",
                            "rad" if i < n else "p.u.", 1.) for i in range(2*n))
    point = OperatingPoint(np.r_[delta_rad, np.zeros(n)], np.empty(0), 0., states,
                           np.empty(0), {"angular_frame": "rotor absoluto, rotación común libre"})
    return h_system_s, coupling_pu, stiffness_pu_per_rad, point


def eigenvalues(result):
    return np.array([complex(m["lambda"]["real"], m["lambda"]["imag"]) for m in result.modes])


@pytest.mark.parametrize("n,seed", [(2, 271), (3, 542), (5, 813)])
def test_classical_multimachine_properties(n, seed, tmp_path):
    inertia_s, coupling_pu, stiffness, point = classical_fixture(n, seed)
    mass = 2*inertia_s
    omega_nom = 2*np.pi*50.
    # La simetría corresponde a la rigidez de una red recíproca sin pérdidas.
    # A no es simétrica porque las inercias y las unidades de estado difieren.
    np.testing.assert_allclose(stiffness, stiffness.T, atol=1e-14)
    np.testing.assert_allclose(stiffness@np.ones(n), 0., atol=1e-14)
    mass_normalized = omega_nom*stiffness/np.sqrt(mass[:, None]*mass[None, :])
    np.testing.assert_allclose(mass_normalized, mass_normalized.T, atol=1e-14)
    squared_frequencies = np.linalg.eigvalsh(mass_normalized)[1:]
    assert np.all(squared_frequencies > 0.)
    options = ModalOptions()
    undamped = ClassicalMachines(inertia_s, coupling_pu, np.zeros(n), point.x[:n])
    lin = linearize(undamped, point, options)
    expected_a = np.block([[np.zeros((n, n)), omega_nom*np.eye(n)],
                           [-stiffness/mass[:, None], np.zeros((n, n))]])
    np.testing.assert_allclose(lin.a, expected_a, atol=1e-8, rtol=0.)
    assert not np.allclose(lin.a, lin.a.T)
    rotation = np.r_[np.ones(n), np.zeros(n)]
    np.testing.assert_allclose(lin.a@rotation, 0., atol=1e-8)
    assert np.linalg.matrix_rank(lin.a, tol=1e-7) == 2*n-1
    full = solve_modes(lin, point.states, options)
    assert len(full.modes) == 2*n
    # Sin D, la velocidad común añade un cero algebraico y un bloque Jordan.
    exact_full = solve_modes(replace(lin, a=expected_a, diagnostics=()), point.states, options)
    assert np.count_nonzero(abs(eigenvalues(exact_full)) < 1e-5) == 2
    zero_candidates = sorted(full.modes, key=lambda m: abs(complex(m["lambda"]["real"], m["lambda"]["imag"])))[:2]
    for mode in zero_candidates:
        assert abs(complex(mode["lambda"]["real"], mode["lambda"]["imag"])) <= max(
            1e-7, 2*mode["step_sensitivity_per_s"])
    assert full.participation is None
    reduced, states = reduce_angle_reference(lin, point.states, range(n), 0)
    result = solve_modes(reduced, states, options)
    spectrum = eigenvalues(result)
    assert len(result.modes) == 2*n-1
    assert np.count_nonzero(abs(spectrum) < 1e-7) == 1
    expected = np.r_[0j, 1j*np.sqrt(squared_frequencies), -1j*np.sqrt(squared_frequencies)]
    np.testing.assert_allclose(spectrum, expected[match_eigenvalues(spectrum, expected)], atol=1e-7)
    np.testing.assert_allclose(spectrum.real, 0., atol=1e-7)
    for mode in result.modes:
        assert mode["eigenvector_residual"] <= 1e-9
        assert mode["step_sensitivity_per_s"] <= 1e-7
        if mode["kind"] == "oscillatory":
            partner = result.modes[mode["conjugate_index"]]
            assert partner["conjugate_index"] == mode["index"]
            assert partner["lambda"]["imag"] == pytest.approx(-mode["lambda"]["imag"])
    decay_rates = []
    for alpha_per_s in (.1*np.sqrt(squared_frequencies[0]), .4*np.sqrt(squared_frequencies[0])):
        damped = ClassicalMachines(inertia_s, coupling_pu, mass*alpha_per_s, point.x[:n])
        damped_lin = linearize(damped, point, options)
        damped_modes = solve_modes(damped_lin, point.states, options)
        nonzero = eigenvalues(damped_modes)[abs(eigenvalues(damped_modes)) > 1e-7]
        assert len(damped_modes.modes) == 2*n
        assert len(nonzero) == 2*n-1
        assert np.all(nonzero.real < 0.)
        assert all(m["classification"] == "stable" for m in damped_modes.modes
                   if abs(complex(m["lambda"]["real"], m["lambda"]["imag"])) > 1e-7)
        oscillatory = [m for m in damped_modes.modes if m["kind"] == "oscillatory"]
        np.testing.assert_allclose([m["decay_rate_per_s"] for m in oscillatory], -alpha_per_s/2,
                                   atol=1e-7, rtol=0.)
        decay_rates.append(float(np.max(nonzero.real)))
        quotient, quotient_states = reduce_angle_reference(damped_lin, point.states, range(n), 0)
        quotient_modes = solve_modes(quotient, quotient_states, options)
        assert len(quotient_modes.modes) == 2*n-1
        assert np.all(eigenvalues(quotient_modes).real < 0.)
        np.testing.assert_allclose(nonzero,
            eigenvalues(quotient_modes)[match_eigenvalues(nonzero, eigenvalues(quotient_modes))], atol=1e-7)
        assert len(quotient_states) == 2*n-1
        assert sum(s.unit == "p.u." for s in quotient_states) == n
    assert decay_rates[1] < decay_rates[0] < 0.
    (tmp_path/"classical_properties.json").write_text(json.dumps({
        "n_machines": n, "seed": seed, "s_base_mva": 100., "f_nom_hz": 50.,
        "inertia_system_s": inertia_s.tolist(), "stiffness_pu_per_rad": stiffness.tolist(),
        "undamped_modes": list(result.modes), "decay_rates_per_s": decay_rates,
        "ref_comparison": "sin validar contra la referencia externa"}, allow_nan=False), encoding="utf-8")


class SMIB:
    """Barra infinita fija, Pe=Pmax sin δ y swing en base de sistema."""
    def __init__(self, inertia_s, p_max_pu, delta_rad, damping_pu):
        self.inertia_s, self.p_max_pu = inertia_s, p_max_pu
        self.p_mechanical_pu = p_max_pu*np.sin(delta_rad)
        self.damping_pu = damping_pu

    def clone(self):
        return copy.deepcopy(self)

    def evaluate(self, x, z, time_s):
        return np.array([2*np.pi*50.*x[1], (self.p_mechanical_pu-self.p_max_pu*np.sin(x[0])
                         -self.damping_pu*x[1])/(2*self.inertia_s)]), np.empty(0)


@pytest.mark.parametrize("damping_pu", [0., 2.])
def test_smib_frequency_matches_closed_form(damping_pu, tmp_path):
    # H y x'd se convierten desde 200 MVA a 100 MVA en forma explícita.
    s_machine_mva, s_base_mva, h_machine_s = 200., 100., 4.
    h_system_s = h_machine_s*s_machine_mva/s_base_mva
    x_machine_pu, x_line_system_pu = .25, .3
    x_system_pu = x_machine_pu*s_base_mva/s_machine_mva+x_line_system_pu
    p_max_pu = 1.08*1.02/x_system_pu
    delta_rad = .45
    synchronizing_pu_per_rad = p_max_pu*np.cos(delta_rad)
    omega_n_squared = 2*np.pi*50.*synchronizing_pu_per_rad/(2*h_system_s)
    sigma_per_s = -damping_pu/(4*h_system_s)
    omega_d_rad_per_s = np.sqrt(omega_n_squared-sigma_per_s**2)
    expected_frequency_hz = omega_d_rad_per_s/(2*np.pi)
    states = (StateRef(0, "smib_g", "analytical/smib/g", "delta_rad", "rad", 1.),
              StateRef(1, "smib_g", "analytical/smib/g", "speed_deviation_pu", "p.u.", 1.))
    point = OperatingPoint(np.array([delta_rad, 0.]), np.empty(0), 0., states, np.empty(0),
                           {"angular_frame": "barra infinita fija"})
    adapter = SMIB(h_system_s, p_max_pu, delta_rad, damping_pu)
    lin = linearize(adapter, point, ModalOptions())
    result = solve_modes(lin, states, ModalOptions())
    assert result.status == "completed"
    assert len(result.modes) == 2
    for mode in result.modes:
        assert mode["frequency_hz"] == pytest.approx(expected_frequency_hz, rel=1e-8)
        assert mode["decay_rate_per_s"] == pytest.approx(sigma_per_s, abs=1e-9)
        assert mode["damping_ratio"] == pytest.approx(-sigma_per_s/np.sqrt(omega_n_squared), abs=1e-9)
        assert mode["eigenvector_residual"] <= 1e-9
        assert mode["step_sensitivity_per_s"] <= 1e-7
    np.testing.assert_allclose(eigenvalues(result),
        [sigma_per_s+1j*omega_d_rad_per_s, sigma_per_s-1j*omega_d_rad_per_s], atol=1e-7)
    assert sum(m["table_visible"] for m in result.modes) == 1
    (tmp_path/"smib_frequency.json").write_text(json.dumps({
        "frequency_closed_form_hz": float(expected_frequency_hz), "s_base_mva": s_base_mva,
        "h_system_s": h_system_s, "x_system_pu": x_system_pu,
        "damping_system_pu": damping_pu, "modes": list(result.modes),
        "ref_comparison": "sin validar contra la referencia externa"}, allow_nan=False), encoding="utf-8")

