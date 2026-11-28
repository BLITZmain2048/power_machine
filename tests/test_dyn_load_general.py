"""General Load según TechRef, sin afirmar equivalencia numérica con REF."""

from __future__ import annotations

import numpy as np
import pytest

from power_machine.dynamics.interfaces import (
    DynamicDevice, LocalJacobianDevice, finite_difference_jacobian,
)
from power_machine.dynamics.load_general import GeneralLoad


# Proyección de modelo/types.json del IEEE 39 reference export, sin dependencia
# del paquete externo. La carga REF usa P lineal y Q cuadrática, no ambas Z.
REF_PARAMETERS = {
    "loddy": 100, "i_nln": 1,
    "aP": 0.0, "bP": 1.0, "cP": 0.0,
    "aQ": 0.0, "bQ": 0.0, "cQ": 1.0,
    "kpu0": 0.0, "kpu1": 1.0, "kpu": 2.0,
    "kqu0": 0.0, "kqu1": 1.0, "kqu": 2.0,
    "t1": 0.10000000149011612,
    "tpu": 0.0, "tqu": 0.0, "tpf": 0.0, "tqf": 0.0,
    "kpf": 0.0, "kqf": 0.0, "udmin": 0.8, "udmax": 1.2,
}


def _load(**changes):
    params = dict(REF_PARAMETERS, **changes)
    return GeneralLoad("ref_load", r"Grid.ElmNet\Load.ElmLod", "ref_bus", params)


def _trapezoid(load, initial, v, step_s, end_s):
    """Newton local sobre f de producción, sin usar la solución del filtro."""
    x = initial.copy()
    states = [x.copy()]
    empty = np.empty(0)
    for i in range(round(end_s/step_s)):
        previous = x.copy()
        before = load.f(i*step_s, previous, empty, v)
        for _ in range(4):
            after = load.f((i+1)*step_s, x, empty, v)
            residual = x-previous-step_s/2*(before+after)
            if abs(residual[0]) < 1e-13:
                break
            jac = load.jacobian_local((i+1)*step_s, x, empty, v)[0, 0]
            x -= residual/(1-step_s/2*jac)
        else:
            raise AssertionError("Newton de carga no convergió")
        states.append(x.copy())
    return np.array(states)[:, 0]


@pytest.mark.parametrize("v0", [0.8+0j, 0.9*np.exp(.7j), 1.07*np.exp(-.3j), 1.2+0j])
@pytest.mark.parametrize("s0", [500+184j, 308.6-92.2j, 0j, 40j])
def test_exact_lf_initialization_equilibrium_and_contract(v0, s0):
    load = _load()
    x = load.init(v0, s0)
    original = x.copy()
    assert isinstance(load, DynamicDevice)
    assert isinstance(load, LocalJacobianDevice)
    assert (load.n_states, load.n_alg) == (1, 0)
    assert load.object_id == "ref_load"
    assert load.relative_path == r"Grid.ElmNet\Load.ElmLod"
    assert x[0] == abs(v0)
    assert load.consumed_power_mva(v0, x) == pytest.approx(s0, abs=1e-12)
    assert abs(load.current_injection(x, v0)) < 1e-13
    np.testing.assert_array_equal(load.f(0, x, np.empty(0), v0), [0.0])
    assert load.g(0, x, np.empty(0), v0).size == 0
    assert load.events() == ()
    np.testing.assert_array_equal(x, original)


def test_from_model_and_nineteen_independent_states():
    items = [{"class": "ElmLod", "id": f"load_{i}", "path": f"grid/load_{i}",
              "bus": f"bus_{i}", "type_params": dict(REF_PARAMETERS)} for i in range(19)]
    devices = [GeneralLoad.from_model(item, s_base_mva=50) for item in items]
    states = [device.init(1.0, 500+184j) for device in devices]
    assert sum(device.n_states for device in devices) == 19
    devices[0].set_step_percent(dp_percent=10)
    assert devices[0].consumed_power_mva(1.0, states[0]) == pytest.approx(550+184j)
    assert devices[1].consumed_power_mva(1.0, states[1]) == pytest.approx(500+184j)
    assert devices[0].object_id == items[0]["id"]
    assert devices[0].relative_path == items[0]["path"]
    assert items[0]["type_params"] == REF_PARAMETERS
    with pytest.raises(ValueError, match="ElmLod"):
        GeneralLoad.from_model(dict(items[0], **{"class": "ElmSym"}))


@pytest.mark.parametrize("target_u", [.2, .6, .94, 1.1, 1.4])
def test_voltage_step_against_analytic_filter_and_power(target_u):
    load = _load()
    initial = load.init(1.03*np.exp(.4j), 500+184j)
    step_s, end_s = .001, .4
    v = target_u*np.exp(-.2j)
    actual = _trapezoid(load, initial, v, step_s, end_s)
    times = np.arange(actual.size)*step_s
    expected = target_u+(initial[0]-target_u)*np.exp(-times/REF_PARAMETERS["t1"])
    np.testing.assert_allclose(actual, expected, atol=3e-6, rtol=0)
    # Multiplicador (47) independiente de la implementación.
    if target_u < .4:
        k = 2*(target_u/.8)**2
    elif target_u <= .8:
        k = 1-2*((target_u-.8)/.8)**2
    elif target_u < 1.2:
        k = 1
    else:
        k = 1+(target_u-1.2)**2
    expected_s = k*(500*expected/initial[0]+1j*184*(expected/initial[0])**2)
    actual_s = np.array([load.consumed_power_mva(v, np.array([xu])) for xu in actual])
    np.testing.assert_allclose(actual_s, expected_s, atol=.003, rtol=0)
    # La tensión medida permanece continua, la corrección de banda es inmediata.
    assert load.consumed_power_mva(v, initial) == pytest.approx(k*(500+184j))


def test_h_half_second_order_for_state_and_pq():
    load = _load()
    x0 = load.init(1.03, 500+184j)
    coarse = _trapezoid(load, x0, .94, .002, .4)
    fine = _trapezoid(load, x0, .94, .001, .4)
    times = np.arange(coarse.size)*.002
    exact = .94+(1.03-.94)*np.exp(-times/REF_PARAMETERS["t1"])
    error_coarse = np.max(abs(coarse-exact))
    error_fine = np.max(abs(fine[::2]-exact))
    assert error_coarse/error_fine == pytest.approx(4, rel=.002)
    exact_s = 500*exact/1.03+1j*184*(exact/1.03)**2
    coarse_s = np.array([load.consumed_power_mva(.94, np.array([u])) for u in coarse])
    fine_s = np.array([load.consumed_power_mva(.94, np.array([u])) for u in fine[::2]])
    assert np.max(abs(coarse_s-exact_s))/np.max(abs(fine_s-exact_s)) == pytest.approx(4, rel=.002)
    assert np.max(abs(coarse_s-fine_s)) < .01
    assert np.max(abs(coarse-fine[::2])) < 1e-5


@pytest.mark.parametrize("u,k,dk", [
    (0, 0, 0), (.2, .125, 1.25), (.4, .5, 2.5),
    (.6, .875, 1.25), (.8, 1, 0), (1., 1, 0),
    (1.2, 1, 0), (1.4, 1.04, .4),
])
def test_exact_band_multiplier(u, k, dk):
    load = _load()
    assert load.voltage_multiplier(u) == pytest.approx((k, dk), abs=1e-14)
    load.init(1., 500+184j)
    # Congela xu deliberadamente para distinguir U de xu y U/U0.
    assert load.consumed_power_mva(u, np.array([1.])) == pytest.approx(k*(500+184j))


@pytest.mark.parametrize("threshold", [.4, .8, 1.2])
def test_band_continuity_and_slope(threshold):
    load = _load()
    epsilon = 1e-7
    left = load.voltage_multiplier(threshold-epsilon)
    at = load.voltage_multiplier(threshold)
    right = load.voltage_multiplier(threshold+epsilon)
    assert max(abs(left[0]-at[0]), abs(right[0]-at[0])) < 3e-7
    assert max(abs(left[1]-at[1]), abs(right[1]-at[1])) < 7e-7


def test_native_steady_state_is_linear_p_quadratic_q_and_not_both_z():
    load = _load()
    load.init(1.04, 500+184j)
    v = .93*np.exp(.5j)
    expected = 500*(.93/1.04)+1j*184*(.93/1.04)**2
    assert load.consumed_power_mva(v) == pytest.approx(expected)
    assert abs(load.consumed_power_mva(v)-(500+184j)*(.93/1.04)**2) > 40
    # Los límites están sobre U absoluto, no sobre el cociente U/U0.
    assert load.voltage_multiplier(.81)[0] == 1
    np.testing.assert_array_equal(load.f(0, np.array([.93]), [], v), [0.0])


@pytest.mark.parametrize("u", [.8, .91, 1.08, 1.2])
def test_impedance_equivalence_in_band_when_both_exponents_are_two(u):
    load = _load(bP=0, cP=1)
    v0 = 1.04*np.exp(.1j)
    s0 = 500-92.2j
    load.init(v0, s0)
    v = u*np.exp(-.5j)
    x = np.array([u])
    assert load.consumed_power_mva(v, x) == pytest.approx(s0*(u/abs(v0))**2)
    assert abs(load.current_injection(x, v)) < 1e-12
    # Fuera de banda se aplica además k(U), incluso para exponentes dos.
    assert load.consumed_power_mva(.6) == pytest.approx(.875*s0*(.6/abs(v0))**2)


def test_static_dynamic_split_events_and_frequency_disabled():
    load = _load(loddy=70)
    load.init(1, 500+184j)
    v, x = .6*np.exp(.4j), np.array([.9])
    expected = 500*(.3*.6**2+.7*.875*.9)+1j*184*(.3*.6**2+.7*.875*.9**2)
    assert load.consumed_power_mva(v, x) == pytest.approx(expected)
    load.set_frequency_pu(1.02)
    assert load.consumed_power_mva(v, x) == pytest.approx(expected)
    load.set_step_percent(dp_percent=10, dq_percent=-5)
    assert load.consumed_power_mva(v, x) == pytest.approx(1.1*expected.real+.95j*expected.imag)
    # Reconstruye consumo desde la corriente TOTAL, incluida la estampa LF.
    full_injection = load.current_injection(x, v)-load._y_baseline*v
    assert -100*v*np.conj(full_injection) == pytest.approx(expected.real*1.1+expected.imag*.95j)
    reinitialized = load.init(1, 500+184j)
    assert load.consumed_power_mva(1, reinitialized) == pytest.approx(500+184j)


@pytest.mark.parametrize("u", [.2, .4, .6, .8, 1, 1.2, 1.4])
@pytest.mark.parametrize("share", [70, 100])
def test_analytic_jacobian_against_nonlinear_residuals(u, share):
    load = _load(loddy=share)
    load.init(1.04*np.exp(.1j), 500-92.2j)
    load.set_step_percent(dp_percent=10, dq_percent=-5)
    v = u*np.exp(.4j)
    point = np.array([.97, v.real, v.imag])

    def function(p):
        voltage = complex(p[1], p[2])
        current = load.current_injection(p[:1], voltage)
        return np.r_[load.f(0, p[:1], [], voltage), current.real, current.imag]

    numeric = finite_difference_jacobian(function, point, rel_step=1e-7)
    actual = load.jacobian_local(0, point[:1], [], v)
    assert actual.shape == (3, 3)
    np.testing.assert_allclose(actual, numeric, rtol=3e-6, atol=6e-6)


def test_zero_voltage_finite_current_and_jacobian_convention():
    load = _load()
    x = load.init(1.03, 500+184j)
    assert load.current_injection(x, 0j) == 0j
    assert load.consumed_power_mva(0j, x) == 0j
    assert load.f(0, x, [], 0j)[0] == pytest.approx(-1.03/REF_PARAMETERS["t1"])
    jac = load.jacobian_local(0, x, [], 0j)
    np.testing.assert_array_equal(jac[0, 1:], [0., 0.])
    for column, v in ((1, 1e-8+0j), (2, 1e-8j)):
        current = load.current_injection(x, v)
        np.testing.assert_allclose([current.real/1e-8, current.imag/1e-8], jac[1:, column], atol=1e-12)
    assert np.isfinite(load.current_injection(x, 1e-200+1e-200j))
    assert np.isfinite(load.jacobian_local(0, x, [], 1e-200+1e-200j)).all()
    # En falla sostenida el filtro llega a cero sin recortarlo a udmin.
    np.testing.assert_array_equal(load.f(0, np.array([0.]), [], 0j), [0.])


def test_canonical_exponent_aliases_and_polynomial_mixture():
    params = dict(REF_PARAMETERS, aP=.2, bP=.3, cP=.5)
    aliases = {"kpu0": "e_aP", "kpu1": "e_bP", "kpu": "e_cP",
               "kqu0": "e_aQ", "kqu1": "e_bQ", "kqu": "e_cQ"}
    for alias, canonical in aliases.items():
        params[canonical] = params.pop(alias)
    load = GeneralLoad("id", "path", "bus", params)
    load.init(1., 500+184j)
    assert load.consumed_power_mva(.9) == pytest.approx(500*(.2+.3*.9+.5*.9**2)+184j*.9**2)
    without_c = dict(REF_PARAMETERS)
    del without_c["cP"]
    assert GeneralLoad("id", "path", "bus", without_c)._polynomials[0] == ((1., 1.),)


@pytest.mark.parametrize("key,value,exception", [
    ("i_nln", 2, NotImplementedError), ("t1", 0, NotImplementedError),
    ("tpu", .1, NotImplementedError), ("tqu", .1, NotImplementedError),
    ("tpf", .1, NotImplementedError), ("tqf", .1, NotImplementedError),
    ("kpf", 1, NotImplementedError), ("kqf", -1, NotImplementedError),
    ("loddy", 101, ValueError), ("loddy", 0, ValueError),
    ("t1", None, ValueError), ("t1", "", ValueError),
    ("udmin", 0, ValueError), ("udmax", .7, ValueError),
    ("udmax", float("nan"), ValueError), ("kpu1", None, ValueError),
    ("cP", .2, ValueError), ("e_bP", 2, ValueError),
])
def test_reject_missing_and_unsupported_profiles(key, value, exception):
    with pytest.raises(exception):
        _load(**{key: value})


def test_reject_invalid_initial_state_voltage_and_base():
    load = _load()
    with pytest.raises(ValueError, match="init"):
        load.current_injection(np.array([1.]), 1.)
    for voltage, power in ((0, 1), (.7, 1), (1.3, 1), (1, complex(float("nan"), 0))):
        with pytest.raises(ValueError):
            load.init(voltage, power)
    load.init(1, 500+184j)
    for state in ([], [1, 2], [-1], [float("nan")]):
        with pytest.raises(ValueError):
            load.f(0, np.array(state), [], 1)
    with pytest.raises(ValueError):
        load.current_injection(np.array([1.]), complex(float("inf"), 0))
    with pytest.raises(ValueError):
        load.jacobian_local(0, np.array([1.]), [1.], 1.)
    with pytest.raises(ValueError):
        GeneralLoad("id", "path", "bus", REF_PARAMETERS, s_base_mva=0)

