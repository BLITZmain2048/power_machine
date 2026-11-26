"""Governors estándar aislados, sin declarar equivalencia RMS/modal con PF."""
from __future__ import annotations

from types import SimpleNamespace

import numpy as np
import pytest
import json
from power_machine.resources import pf_data_root, pf_data_available
_private = json.loads((pf_data_root() / "governor_regression.json").read_text()) if pf_data_available() else {}
pytestmark = pytest.mark.pfdata

from power_machine.dynamics.controllers import ControlledSynchronousMachine
from power_machine.dynamics.governors_ieeeg import (
    IEEEG1Governor, IEEEG3Governor, IEEEG1_NAMES, IEEEG3_NAMES,
)
from power_machine.dynamics.interfaces import finite_difference_jacobian


PF_G1 = _private.get("PF_G1")
PF_G3 = _private.get("PF_G3")

# la potencia LF es total. Rstr=dpu=0 en estas nueve máquinas.
PF_LF = _private.get("PF_LF")


def _governor(family, *, pgn_mw=500, turbine="HP", **changes):
    names, vector = (IEEEG1_NAMES, PF_G1) if family == "G1" else (IEEEG3_NAMES, PF_G3)
    p = dict(zip(names, vector))
    p.update(changes)
    if family == "G1":
        return IEEEG1Governor("pf_gov", "PF_relative_path", p, pgn_mw=pgn_mw, turbine=turbine)
    return IEEEG3Governor("pf_gov", "PF_relative_path", p, pgn_mw=pgn_mw)


def _trapezoid(gov, x0, inputs, step_s, end_s):
    """Trapezoidal implícito local con Newton, sin proyección en el caso lineal."""
    x = x0.copy()
    states = [x.copy()]
    identity = np.eye(gov.n_states)
    for index in range(round(end_s/step_s)):
        previous = x.copy()
        dx0 = gov.evaluate(index*step_s, previous, inputs)[1]
        for _ in range(12):
            dx1 = gov.evaluate((index+1)*step_s, x, inputs)[1]
            residual = x-previous-step_s/2*(dx0+dx1)
            if np.max(np.abs(residual)) < 1e-13:
                break
            df = gov.jacobian(0, x, inputs)[0][:, :gov.n_states]
            x -= np.linalg.solve(identity-step_s/2*df, residual)
        else:
            raise AssertionError("Newton local sin convergencia")
        states.append(x.copy())
    return np.array(states)


def _linear_solution(family, params, x0, delta_pref, delta_w, times_s):
    """Solución espectral independiente desde ecuaciones físicas sin jacobian."""
    p = params
    if family == "G1":
        # Tiempos distintos en el ensayo evitan Jordan repetido en eig.
        a = np.zeros((6, 6))
        a[0, 0] = -1/p["T1"]
        a[1, :2] = [-p["K"]*(1-p["T2"]/p["T1"])/p["T3"], -1/p["T3"]]
        for i, name in enumerate(("T4", "T5", "T6", "T7"), 2):
            a[i, i-1:i+1] = [1/p[name], -1/p[name]]
        b = np.array([delta_w/p["T1"],
                      (delta_pref-p["K"]*p["T2"]/p["T1"]*delta_w)/p["T3"], 0, 0, 0, 0])
    else:
        a = np.array([
            [0, 0, 1, 0],
            [1/(p["a11"]*p["Tw"]), -1/(p["a11"]*p["Tw"]), 0, 0],
            [-(p["Sigma"]+p["Delta"])/(p["Tg"]*p["Tp"]), 0,
             -1/p["Tp"], p["Delta"]/(p["Tg"]*p["Tp"])],
            [1/p["Tr"], 0, 0, -1/p["Tr"]],
        ])
        b = np.array([0, 0, (delta_pref-delta_w)/(p["Tg"]*p["Tp"]), 0])
    poles, vectors = np.linalg.eig(a)
    coefficients = np.linalg.solve(vectors, b)
    response = []
    for time_s in times_s:
        increment = vectors@(np.expm1(poles*time_s)/poles*coefficients)
        assert np.max(np.abs(increment.imag)) < 1e-11
        response.append(x0+increment.real)
    return np.array(response)


@pytest.mark.parametrize("family", ["G1", "G3"])
@pytest.mark.parametrize("port,increment", [("Pref", .001), ("w", .001), ("wref", -.001)])
def test_reference_and_speed_steps_against_analytic_solution_and_h2(family, port, increment):
    gov = _governor(family, **({"T3": .4} if family == "G1" else {}))
    x0, base = gov.initialize({"pm": .4})
    inputs = dict(base)
    inputs[port] += increment
    dp = inputs["Pref"]-base["Pref"]
    dw = inputs["w"]-inputs["wref"]-(base["w"]-base["wref"])
    coarse = _trapezoid(gov, x0, inputs, .001, .3)
    fine = _trapezoid(gov, x0, inputs, .0005, .3)
    finer = _trapezoid(gov, x0, inputs, .00025, .3)
    exact = _linear_solution(family, gov.parameters, x0, dp, dw, np.arange(len(coarse))*.001)
    np.testing.assert_allclose(coarse, exact, rtol=0, atol=2e-6)
    error = np.max(np.abs(coarse-fine[::2]))
    refined_error = np.max(np.abs(fine-finer[::2]))
    assert error > 1e-12
    assert error/refined_error == pytest.approx(4, rel=.02)
    assert refined_error < 2e-6


@pytest.mark.parametrize("index", range(2, 11))
def test_nine_pf_vectors_equilibrium_traceability_and_state_count(index):
    family = "G1" if index < 10 else "G3"
    cls, names, vector = ((IEEEG1Governor, IEEEG1_NAMES, PF_G1) if family == "G1"
                          else (IEEEG3Governor, IEEEG3_NAMES, PF_G3))
    gov = cls(f"pf_gov_{index}", f"GOV {index:02d}.ElmDsl", vector, pgn_mw=600)
    assert tuple(gov.parameters) == names
    assert gov.parameters["Uc"] == vector[-4]
    assert gov.parameters["Pmin"] == vector[-3]
    assert gov.parameters["Uo"] == vector[-2]
    assert gov.parameters["Pmax"] == vector[-1]
    assert gov.object_id == f"pf_gov_{index}"
    assert gov.relative_path == f"GOV {index:02d}.ElmDsl"
    assert gov.n_states == len(set(gov.state_names)) == (6 if index < 10 else 4)
    assert all(base == 600 for base in gov.effective_bases_mw.values())
    assert gov.base_assumptions
    assert all(gov.parameters[name] == 0 for name in gov.effective_bases_mw)
    x, u = gov.initialize({"pm": .73, "dw": .002})
    initial = x.copy()
    y, dx = gov.evaluate(0, x, u)
    assert y["pm"] == pytest.approx(.73, abs=1e-14)
    assert np.max(np.abs(dx)) < 1e-12
    assert u["w"] == 1.002 and u["wref"] == 1
    accepted, derivative = gov.accept_step(.1, x, u)
    assert accepted == y
    np.testing.assert_array_equal(derivative, dx)
    np.testing.assert_array_equal(x, initial)
    assert gov.next_event_time(0) is None
    expected_pref = .73+5*.002 if family == "G1" else .002+.04*.73
    assert u["Pref"] == pytest.approx(expected_pref)
    assert 8*IEEEG1Governor.n_states+IEEEG3Governor.n_states == 52


@pytest.mark.parametrize("turbine,weights", [
    ("HP", (.12, .24, .36, .48)), ("LP", (.3, .4, .6, .7)),
    ("COMBINED", (.42, .64, .96, 1.18)),
])
def test_hp_lp_stage_allocation_and_explicit_power_bases(turbine, weights):
    gov = _governor("G1", pgn_mw=500, turbine=turbine, PNhp=600, PNlp=1000,
                    K1=.1, K3=.2, K5=.3, K7=.4, K2=.15, K4=.2, K6=.3, K8=.35)
    np.testing.assert_allclose(gov.weights, weights, atol=1e-14)
    assert gov.base_assumptions == ()
    stages = [.2, .4, .6, .8]
    y, _ = gov.evaluate(0, np.array([0, .5, *stages]), {"Pref": .5, "w": 1, "wref": 1})
    assert y["pm"] == pytest.approx(sum(k*z for k, z in zip(weights, stages)))
    x, u = gov.initialize({"pm": .7})
    assert x[1] == pytest.approx(.7/sum(weights))
    assert gov.evaluate(0, x, u)[0]["pm"]*gov.pgn_mw == pytest.approx(350)


def test_hydro_base_and_water_column_inverse_response():
    gov = _governor("G3", pgn_mw=500, PN=1000)
    x, u = gov.initialize({"pm": .8})
    np.testing.assert_allclose(x, [.4, .4, 0, .4], atol=1e-14)
    assert gov.power_base_ratio == 2
    assert gov.base_assumptions == ()
    assert gov.direct_gain == -2 and gov.water_gain == 3
    # Escalón impuesto de compuerta, solución de la turbina aislada:
    # pm(t)=2*(.6-3*.2*exp(-t/(a11*Tw))). No usa evaluate para el estado z.
    for time_s in (0, .1, .5, 2):
        water_pu = .6-.2*np.exp(-time_s/.375)
        state = np.array([.6, water_pu, 0, .6])
        output = gov.evaluate(time_s, state, u)[0]["pm"]
        assert output == pytest.approx(2*(.6-.6*np.exp(-time_s/.375)))
    assert gov.evaluate(0, np.array([.6, .4, 0, .6]), u)[0]["pm"] < .8


@pytest.mark.parametrize("family", ["G1", "G3"])
def test_jacobian_against_numerical_differences(family):
    gov = _governor(family)
    x, u = gov.initialize({"pm": .5})
    x += (.003 if family == "G1" else .00003)*np.arange(1, gov.n_states+1)
    point = np.r_[x, [u[key] for key in gov.ir.inputs]]
    def both(values):
        y, dx = gov.evaluate(0, values[:gov.n_states], dict(zip(gov.ir.inputs, values[gov.n_states:])))
        return np.r_[dx, y["pm"]]
    expected = finite_difference_jacobian(both, point)
    df, dy = gov.jacobian(0, x, u)
    assert df.shape == (gov.n_states, gov.n_states+3)
    assert dy.shape == (1, gov.n_states+3)
    np.testing.assert_allclose(np.vstack((df, dy)), expected, rtol=2e-8, atol=2e-7)


@pytest.mark.parametrize("family", ["G1", "G3"])
@pytest.mark.parametrize("sign", [-1, 1])
def test_position_rate_limits_release_and_non_mutating_projection(family, sign):
    gov = _governor(family)
    x, base = gov.initialize({"pm": .5})
    p = gov.parameters
    gate = gov.gate_index
    bound = p["Pmax"] if sign == 1 else p["Pmin"]
    x[gate] = bound
    outward = dict(base, Pref=sign*10)
    if family == "G3":
        x[2] = p["Uo"] if sign == 1 else p["Uc"]
        assert gov.evaluate(0, x, outward)[1][2] == 0
        assert np.all(gov.jacobian(0, x, outward)[0][2] == 0)
    assert gov.evaluate(0, x, outward)[1][gate] == 0
    assert np.all(gov.jacobian(0, x, outward)[0][gate] == 0)
    inward = dict(base, Pref=-sign*10)
    if family == "G3":
        # Inercia Tp conserva la velocidad al invertir el comando.
        assert sign*gov.evaluate(0, x, inward)[1][2] < 0
        x[2] = -sign*.05
    assert sign*gov.evaluate(0, x, inward)[1][gate] < 0
    overshot = x.copy()
    overshot[gate] = bound+sign*20
    if family == "G3":
        overshot[2] = sign*20
    original = overshot.copy()
    projected = gov.project_state(overshot)
    np.testing.assert_array_equal(overshot, original)
    assert projected[gate] == bound
    if family == "G3":
        assert projected[2] == (p["Uo"] if sign == 1 else p["Uc"])
        assert projected[1] == overshot[1]
    x, base = gov.initialize({"pm": .95 if sign == 1 else .05})
    for _ in range(800):
        def rhs(state):
            return gov.evaluate(0, state, outward)[1]
        h_s = .001
        previous = x.copy()
        k1 = rhs(x)
        k2 = rhs(x+h_s/2*k1)
        k3 = rhs(x+h_s/2*k2)
        k4 = rhs(x+h_s*k3)
        x = gov.project_state(x+h_s/6*(k1+2*k2+2*k3+k4))
        assert p["Pmin"] <= x[gate] <= p["Pmax"]
        rate_per_s = (x[gate]-previous[gate])/h_s
        assert p["Uc"]-1e-8 <= rate_per_s <= p["Uo"]+1e-8
        if family == "G3":
            assert p["Uc"] <= x[2] <= p["Uo"]
    assert x[gate] == pytest.approx(bound, abs=1e-12)


def test_steam_rate_clip_and_dormant_stage_keeps_physical_pole():
    gov = _governor("G1", K7=0)
    x, u = gov.initialize({"pm": .4})
    for pref, rate in ((10, .3), (-10, -.3)):
        assert gov.evaluate(0, x, dict(u, Pref=pref))[1][1] == rate
        assert np.all(gov.jacobian(0, x, dict(u, Pref=pref))[0][1] == 0)
    df, dy = gov.jacobian(0, x, u)
    assert df[5, 5] == -1/gov.parameters["T7"]
    assert dy[0, 5] == 0
    assert gov.n_states == 6


@pytest.mark.parametrize("family", ["G1", "G3"])
def test_initialization_limits_and_incompatible_inputs(family):
    gov = _governor(family)
    for pm in (-.1, 1.1):
        with pytest.raises(ValueError, match='outside limits'):
            gov.initialize({"pm": pm})
    with pytest.raises(ValueError, match='requires load-flow pm'):
        gov.initialize({})
    for pm in (0, 1):
        x, u = gov.initialize({"pm": pm, "w": 1.01, "wref": 1})
        assert np.max(np.abs(gov.evaluate(0, x, u)[1])) < 1e-12
        with pytest.raises(ValueError, match='numerical equilibrium'):
            gov.initialize({"pm": pm, "Pref": 10})
    with pytest.raises(ValueError, match="inconsistent"):
        gov.initialize({"pm": .5, "dw": .1, "w": 1, "wref": 1})


@pytest.mark.parametrize("family", ["G1", "G3"])
def test_existing_controlled_machine_consumer_contract(family):
    gov = _governor(family)
    x, u = gov.initialize({"pm": .5})
    machine = SimpleNamespace(n_states=2, pt=0,
                              outputs=lambda x, v: {"xspeed": u["w"]})
    device = ControlledSynchronousMachine.__new__(ControlledSynchronousMachine)
    device.machine, device.avr, device.governor = machine, None, gov
    device.n_states = 2+gov.n_states
    device.pref_pu, device.wref_pu = u["Pref"], u["wref"]
    avr_dx, gov_dx = device._controls(0, np.r_[np.zeros(2), x], 1+0j)
    assert avr_dx.size == 0
    assert np.max(np.abs(gov_dx)) < 1e-12
    assert machine.pt == pytest.approx(.5)


@pytest.mark.parametrize("family,changes", [
    ("G1", {"T1": 0}), ("G1", {"T3": 0}), ("G1", {"T7": 0}),
    ("G1", {"K1": -1}), ("G1", {"T2": -1}), ("G1", {"PNhp": -1}),
    ("G3", {"Tg": 0}), ("G3", {"Tp": 0}), ("G3", {"Tw": 0}),
    ("G3", {"a11": 0}), ("G3", {"a23": 0}), ("G3", {"Sigma": -1}),
    ("G3", {"PN": -1}), ("G3", {"Tr": None}), ("G1", {"K": ""}),
    ("G1", {"Uo": -.3}), ("G3", {"Pmin": 1}), ("G1", {"K": np.nan}),
])
def test_invalid_parameters_are_explicit_errors(family, changes):
    with pytest.raises(ValueError):
        _governor(family, **changes)


@pytest.mark.parametrize("family", ["G1", "G3"])
def test_missing_parameters_invalid_bases_states_and_inputs(family):
    cls, vector = (IEEEG1Governor, PF_G1) if family == "G1" else (IEEEG3Governor, PF_G3)
    with pytest.raises(ValueError, match="values"):
        cls("id", "path", vector[:-4], pgn_mw=500)
    with pytest.raises(ValueError, match="Missing"):
        cls("id", "path", {}, pgn_mw=500)
    for pgn in (0, -1, None, np.inf, "", True):
        with pytest.raises(ValueError):
            cls("id", "path", vector, pgn_mw=pgn)
    gov = _governor(family)
    x, u = gov.initialize({"pm": .5})
    with pytest.raises(ValueError, match="states"):
        gov.evaluate(0, x[:-1], u)
    with pytest.raises(ValueError, match="states"):
        gov.evaluate(0, x*np.nan, u)
    with pytest.raises(ValueError, match='Missing inputs'):
        gov.evaluate(0, x, {"Pref": .5})
    for bad in (None, "", np.inf):
        with pytest.raises(ValueError):
            gov.evaluate(0, x, dict(u, w=bad))
    if family == "G1":
        with pytest.raises(ValueError, match='gain'):
            _governor("G1", turbine="LP")
        with pytest.raises(ValueError, match="turbine"):
            _governor("G1", turbine="otro")


def test_unknown_parameter_cannot_silently_hide_base_typo():
    with pytest.raises(ValueError, match="Unknown"):
        _governor("G1", PNhap=600)


def test_exported_native_local_blocks_and_similarity_coordinates():
    # Entradas de Amat.csv MOD-02, solo bloques locales leídos del export.
    # Esta evidencia no verifica cableado, límites, bases o red completa.
    g1 = _governor("G1")
    x, u = g1.initialize({"pm": .5})
    df = g1.jacobian(0, x, u)[0]
    # La coordenada PF x0 es -K*speed_lag, las demás conservan escala.
    scale = np.diag(_private["coordinate_scale"])
    native_g1 = np.array(_private["native_g1"])
    np.testing.assert_allclose(scale@df[:, :6]@np.linalg.inv(scale), native_g1, atol=1e-11)
    np.testing.assert_allclose(scale@df[:, 7], _private["input_column_g1"], atol=1e-11)
    g3 = _governor("G3")
    x, u = g3.initialize({"pm": .5})
    df = g3.jacobian(0, x, u)[0]
    native_g3 = np.array(_private["native_g3"])
    np.testing.assert_allclose(df[:, :4], native_g3, atol=1e-11)
    np.testing.assert_allclose(df[:, 5], _private["input_column_g3"], atol=1e-11)


def test_backward_initialization_at_all_nine_exported_loadflow_powers():
    for index, sgn_mva, n_units, p_total_mw in PF_LF:
        pgn_mw = sgn_mva*_private["cosn"]
        family = "G1" if index < 10 else "G3"
        gov = _governor(family, pgn_mw=pgn_mw)
        pm_machine_pu = p_total_mw/(n_units*pgn_mw)
        x, u = gov.initialize({"pm": pm_machine_pu, "dw": 0})
        y, dx = gov.evaluate(0, x, u)
        assert np.max(np.abs(dx)) < 1e-8
        assert y["pm"]*n_units*pgn_mw == pytest.approx(p_total_mw, abs=1e-10)
        assert gov.parameters["Pmin"] <= x[gov.gate_index] <= gov.parameters["Pmax"]
        assert u["w"] == u["wref"] == 1


def test_zero_optional_gains_do_not_create_auxiliary_poles():
    g1 = _governor("G1", K=0, T2=0)
    x, u = g1.initialize({"pm": .5})
    dx = g1.evaluate(0, x, dict(u, w=1.01))[1]
    assert dx[0] == pytest.approx(.01/.2)
    np.testing.assert_array_equal(dx[1:], np.zeros(5))
    assert g1.n_states == 6
    g3 = _governor("G3", Delta=0)
    x, u = g3.initialize({"pm": .5})
    df = g3.jacobian(0, x, u)[0]
    assert df[2, 3] == 0 and df[3, 3] == -.1
    assert g3.n_states == 4

