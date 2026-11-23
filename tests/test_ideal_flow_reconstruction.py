"""Regresiones para líneas ideales, KCL y 3W provisional."""

import pytest

from power_machine.loadflow import run_loadflow
from power_machine.results import reconstruct_ideal_flows
from power_machine.topology import build_topology, ideal_line_candidate


def _line():
    return {"id": "ab", "name": "cero", "class": "ElmLne", "bus_from": "a",
            "bus_to": "b", "outserv": 0, "params": {"R1": 0, "X1": 0, "B1": 0}}


def test_zero_line_fuses_only_without_electrical_type_or_open_port():
    model = {"buses": [{"id": x, "outserv": 0} for x in "ab"],
             "switches": [], "branches": [_line()], "branches3": [],
             "shunts": [], "loads": [], "gens": []}
    assert ideal_line_candidate(model["branches"][0])
    topology = build_topology(model)
    assert topology["bus_to_node"]["a"] == topology["bus_to_node"]["b"]
    assert topology["ideal_edges"][0]["id"] == "ab"
    assert "ab" not in topology["active"]["branches"]
    model["branches"][0]["type_class"] = "TypLne"
    assert not ideal_line_candidate(model["branches"][0])
    assert build_topology(model)["bus_to_node"]["a"] != build_topology(model)["bus_to_node"]["b"]
    model["branches"][0].pop("type_class")
    model["switches"] = [{"id": "sw", "class": "StaSwitch", "element_id": "ab",
                          "port": "bus1", "closed": False, "outserv": 0}]
    assert not build_topology(model)["ideal_edges"]
    assert build_topology(model)["ideal_disconnected"] == ["ab"]
    model["meta"] = {"s_base_mva": 100}
    model["buses"] = [{"id": x, "name": x, "un_kv": 100, "outserv": 0} for x in "ab"]
    model["gens"] = [{"id": "g", "name": "g", "class": "ElmSym", "bus": "a",
                      "ref": True, "mode": "constv", "v_set_pu": 1, "outserv": 0,
                      "params": {"pgini": 0, "qgini": 0, "ngnum": 1}}]
    result = run_loadflow(model)
    line = next(row for row in result["branches"] if row["id"] == "ab")
    assert line["status"] == "puerto_abierto_ideal"
    assert line["p_from_mw"] == line["p_to_mw"] == 0


def _recover(edges, generation, loads, passive_nodes=()):
    buses = {x: {"un_kv": 100} for x in "abc"}
    return reconstruct_ideal_flows(edges, {x: "a" for x in "abc"}, {"a": 0},
                                   {"a": 1+0j} if not passive_nodes else {},
                                   buses, [], generation, loads, {}, passive_nodes)


def test_kcl_tree_recovers_unique_flows():
    edges = [{"id": "ab", "class": "ElmCoup", "bus_from": "a", "bus_to": "b"},
             {"id": "bc", "class": "ElmLne", "bus_from": "b", "bus_to": "c"}]
    rows, warnings = _recover(edges, [{"bus": "a", "p_mw": 10, "q_mvar": 5}],
                              [{"bus": "b", "p_mw": 4, "q_mvar": 2},
                               {"bus": "c", "p_mw": 6, "q_mvar": 3}])
    by_id = {row["id"]: row for row in rows}
    assert by_id["ab"]["p_from_mw"] == pytest.approx(10)
    assert by_id["ab"]["q_from_mvar"] == pytest.approx(5)
    assert by_id["bc"]["p_from_mw"] == pytest.approx(6)
    assert by_id["bc"]["q_to_mvar"] == pytest.approx(-3)
    assert not warnings


def test_kcl_mesh_uses_minimum_norm_and_flags_nonunique():
    edges = [{"id": key, "class": "ElmCoup", "bus_from": a, "bus_to": b}
             for key, a, b in (("ab", "a", "b"), ("bc", "b", "c"), ("ac", "a", "c"))]
    rows, warnings = _recover(edges, [{"bus": "a", "p_mw": 10, "q_mvar": 0}],
                              [{"bus": "b", "p_mw": 4, "q_mvar": 0},
                               {"bus": "c", "p_mw": 6, "q_mvar": 0}])
    flows = {r["id"]: r["p_from_mw"] for r in rows}
    assert flows["ab"] + flows["ac"] == pytest.approx(10)
    assert -flows["ab"] + flows["bc"] == pytest.approx(-4)
    assert flows["ab"] + flows["bc"] - flows["ac"] == pytest.approx(0)
    assert all(r["status"] == "malla_no_unica" for r in rows)
    assert any("not unique" in warning for warning in warnings)


def test_passive_unsupplied_ideal_group_has_zero_flow():
    rows, _ = _recover([{"id": "ab", "class": "ElmCoup", "bus_from": "a",
                         "bus_to": "b"}], [], [], {"a"})
    assert rows[0]["status"] == "sin_energia_pasiva"
    assert rows[0]["p_from_mw"] == 0


def test_tr3_missing_losses_is_provisional_and_phase_matches_pf_sign():
    buses = [{"id": x, "name": x, "un_kv": 100, "outserv": 0} for x in "abc"]
    typ = {**{f"strn3_{k}": 100 for k in "hml"},
           **{f"utrn3_{k}": 100 for k in "hml"},
           "uktr3_h": 10, "uktr3_m": 10, "uktr3_l": 10,
           "nt3ag_h": 0, "nt3ag_m": 1, "nt3ag_l": 1}
    model = {"meta": {"s_base_mva": 100}, "buses": buses, "switches": [],
             "branches": [], "branches3": [{"id": "tr", "name": "tr",
                "class": "ElmTr3", "bus_hv": "a", "bus_mv": "b", "bus_lv": "c",
                "outserv": 0, "type_params": typ}], "shunts": [], "loads": [],
             "gens": [{"id": "g", "name": "g", "class": "ElmSym", "bus": "a",
                       "ref": True, "mode": "constv", "v_set_pu": 1,
                       "outserv": 0, "params": {"pgini": 0, "qgini": 0, "ngnum": 1}}],
             "warnings": []}
    result = run_loadflow(model, options={"provisional_tr3": True})
    assert result["converged"]
    assert result["omitted_branches"] == []
    assert any("3W provisional" in text for text in result["warnings"])
    assert next(r for r in result["buses"] if r["id"] == "b")["angle_deg"] == pytest.approx(-30, abs=1e-6)
    assert result["branches"][0]["p_loss_mw"] == pytest.approx(0, abs=1e-9)



