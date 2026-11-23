"""Pruebas analíticas autocontenidas de elementos eléctricos."""

import cmath
import math

import numpy as np
import pytest

from power_machine.elements import MissingParameter, classify_zpu, gen_pq_mw, y_line, y_tr2, y_zpu


def _tr2(**overrides):
    """Transformador de dos devanados con parámetros explícitos de prueba."""
    params = {"strn": 100, "utrn_h": 100, "utrn_l": 10, "uktr": 10, "pcutr": 0,
              "curmg": 0, "pfe": 0, "dutap": 10, "phitr": 0, "nntap0": 0,
              "tap_side": 0, "nt2ag": 0}
    params.update(overrides)
    return {"class": "ElmTr2", "params": {"nntap": 0}, "type_params": params}


def _zpu(**overrides):
    params = {"Sn": 100, "uratio": 1, "nphshift": 0, "ag": 0,
              "r_pu": 0.1, "x_pu": 0.2, "r_pu_ji": 0.1, "x_pu_ji": 0.2,
              "gi_pu": 0, "bi_pu": 0, "gj_pu": 0, "bj_pu": 0}
    params.update(overrides)
    return {"class": "ElmZpu", "params": params}


def test_line_pi_calculada_a_mano():
    line = {"params": {"R1": 10, "X1": 0, "G1": 0, "B1": 1000, "nlnum": 2}}
    expected = np.array([[20 + 0.1j, -20], [-20, 20 + 0.1j]])
    np.testing.assert_allclose(y_line(line, 100, 100), expected)


def test_transformador_con_desfase_de_30_grados():
    matrix = y_tr2(_tr2(nt2ag=1), 100, 10)
    y = -10j
    shift = cmath.exp(1j * math.pi / 6)
    np.testing.assert_allclose(matrix, [[y, -y / shift.conjugate()], [-y / shift, y]])
    np.testing.assert_allclose(matrix @ np.array([shift, 1]), [0, 0], atol=1e-14)


def test_transformador_con_tap_asimetrico():
    branch = _tr2(phitr=30, nt2ag=1, tap_side=1)
    branch["params"].update(nntap=1, ntnum=2)
    tap = 1 + 0.1 * cmath.exp(1j * math.pi / 6)
    shift = cmath.exp(1j * math.pi / 6)
    expected = 2 * np.array([[-10j, 10j / (shift.conjugate() * tap)],
                             [10j / (shift * tap.conjugate()), -10j / abs(tap) ** 2]])
    np.testing.assert_allclose(y_tr2(branch, 100, 10), expected)


def test_zpu_dirigida_y_clasificacion():
    branch = _zpu(r_pu_ji=0.3, x_pu_ji=0.4, uratio=1.05, nphshift=1, ag=5)
    actual = y_zpu(branch, 220, 18)
    tap = 1.05 * cmath.exp(-1j * math.radians(35))
    np.testing.assert_allclose(actual[0, 1], -tap.conjugate() / (0.1 + 0.2j))
    np.testing.assert_allclose(actual[1, 0], -tap / (0.3 + 0.4j))
    assert classify_zpu(_zpu(r_pu=1e-9, x_pu=0, r_pu_ji=1e-9, x_pu_ji=0)) == "near_short"


def test_generador_respeta_cantidad_de_unidades():
    generator = {"class": "ElmSym", "params": {"pgini": 10, "qgini": -2, "ngnum": 3},
                 "type_params": {"sgn": 20}}
    assert gen_pq_mw(generator) == (30, -6)


def test_parametro_ausente_no_se_interpreta_como_cero():
    with pytest.raises(MissingParameter, match="G1"):
        y_line({"params": {"R1": 1, "X1": 1, "B1": 0, "nlnum": 1}}, 10, 10)
