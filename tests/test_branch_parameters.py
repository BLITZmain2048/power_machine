"""Transformador y línea de dos terminales con datos sintéticos."""

import pytest

from power_machine.elements import _tap_factor
from power_machine.loadflow import run_loadflow
from power_machine.topology import build_topology


def test_tap_is_clamped_to_the_declared_range():
    branch = {"params": {"nntap": 0}, "type_params": {"dutap": 1.0, "nntap0": 0,
              "phitr": 0, "ntpmn": 1, "ntpmx": 15}}
    assert _tap_factor(branch) == pytest.approx(1.01)
    branch["params"]["nntap"] = 20
    assert _tap_factor(branch) == pytest.approx(1.15)


def test_one_ended_line_keeps_its_charging_at_the_energized_end():
    model = {"meta": {"s_base_mva": 100.0},
             "buses": [{"id": item, "name": item, "un_kv": 100.0, "outserv": 0} for item in ("a", "b")],
             "switches": [{"id": "sw", "class": "StaSwitch", "element_id": "line", "port": "bus2", "closed": False, "outserv": 0}],
             "branches": [{"id": "line", "class": "ElmLne", "name": "line", "bus_from": "a", "bus_to": "b", "outserv": 0,
                            "params": {"R1": 1, "X1": 10, "G1": 0, "B1": 100, "nlnum": 1}}],
             "branches3": [], "shunts": [], "loads": [],
             "gens": [{"id": "ref", "class": "ElmSym", "name": "ref", "bus": "a", "outserv": 0, "ref": True,
                       "mode": "constv", "v_set_pu": 1, "params": {"ngnum": 1, "pgini": 0, "qgini": 0}}], "warnings": []}
    assert build_topology(model)["dangling_branches"] == {"line": 0}
    result = run_loadflow(model)
    line = next(item for item in result["branches"] if item["id"] == "line")
    assert line["status"] == "extremo_abierto"
    assert line["q_from_mvar"] < -0.9
    assert line["p_to_mw"] == pytest.approx(0.0, abs=1e-9)
