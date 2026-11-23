"""Ensayos unitarios del diagnóstico N-1 por homotopía."""
from __future__ import annotations

import copy

import pytest

from power_machine.contingency.diagnostics import (_classification, diagnose_outage, reduce_internal_positive_loads,
                                               scale_line_admittance, write_diagnosis)


def _item(item_id, item_class, **fields):
    return {"id": item_id, "object_id": item_id, "relative_path": "N1-DIAG/" + item_id,
            "path": "N1-DIAG/" + item_id, "name": item_id, "class": item_class,
            "outserv": 0, **fields}


@pytest.fixture
def parallel_model():
    return {"meta": {"s_base_mva": 100.0, "f_nom_hz": 50.0},
            "buses": [_item("B1", "ElmTerm", un_kv=110.0), _item("B2", "ElmTerm", un_kv=110.0)],
            "branches": [_item("L_OUT", "ElmLne", bus_from="B1", bus_to="B2",
                                params={"R1": 1.0, "X1": 10.0, "G1": 1.0, "B1": 2.0, "C1": 3.0, "nlnum": 1}),
                         _item("L_PAR", "ElmLne", bus_from="B1", bus_to="B2",
                                params={"R1": 1.0, "X1": 10.0, "G1": 0.0, "B1": 0.0, "nlnum": 1})],
            "gens": [_item("G1", "ElmSym", bus="B1", ref=True, mode="constv", v_set_pu=1.0,
                            params={"pgini": 0.0, "qgini": 0.0, "ngnum": 1})],
            "loads": [_item("D_INT", "ElmLod", bus="B2", scope="sistema_interno",
                             params={"plini": 20.0, "qlini": 4.0, "scale0": 1.0}),
                      _item("D_EXT", "ElmLod", bus="B2", scope="equivalente",
                             params={"plini": 5.0, "qlini": 1.0, "scale0": 1.0})],
            "branches3": [], "switches": [], "shunts": [], "controllers": [], "interface": []}


def test_scale_line_admittance_is_copy_and_scales_all_terms(parallel_model):
    scaled = scale_line_admittance(parallel_model, "L_OUT", 0.25)
    params = scaled["branches"][0]["params"]
    assert params["R1"] == 4.0
    assert params["X1"] == 40.0
    assert params["G1"] == 0.25
    assert params["B1"] == 0.5
    assert params["C1"] == 0.75
    assert parallel_model["branches"][0]["params"]["R1"] == 1.0
    assert scale_line_admittance(parallel_model, "L_OUT", 0.0)["branches"][0]["outserv"] == 1


def test_reduce_internal_positive_loads_preserves_external_and_original(parallel_model):
    reduced, info = reduce_internal_positive_loads(parallel_model, 0.2)
    assert info["load_ids"] == ["D_INT"]
    assert info["base_positive_internal_p_mw"] == 20.0
    assert reduced["loads"][0]["params"]["scale0"] == pytest.approx(0.8)
    assert reduced["loads"][1]["params"]["scale0"] == 1.0
    assert parallel_model["loads"][0]["params"]["scale0"] == 1.0


def test_diagnosis_reuses_solution_and_exports(parallel_model, tmp_path):
    original = copy.deepcopy(parallel_model)
    diagnosis = diagnose_outage(parallel_model, "L_OUT", fractions=(1.0, 0.5, 0.0),
                                load_reductions=(0.1, 0.2))
    assert [row["admittance_fraction"] for row in diagnosis["homotopy"]] == [1.0, 0.5, 0.0]
    assert diagnosis["homotopy"][1]["warm_start_fraction"] == 1.0
    assert diagnosis["parallel_branch_ids"] == ["L_PAR"]
    assert diagnosis["homotopy"][0]["outage_branch"]["model_id"] == "L_OUT"
    assert diagnosis["homotopy"][-1]["parallel_branches"][0]["model_id"] == "L_PAR"
    assert all(row["energized_status"]["ok"] for row in diagnosis["homotopy"])
    assert diagnosis["conclusion"]["classification"] == "indeterminado"
    paths = write_diagnosis([diagnosis], tmp_path / "diagnostico")
    assert all(path.is_file() for path in paths)
    with pytest.raises(FileExistsError):
        write_diagnosis([diagnosis], tmp_path / "diagnostico")
    assert parallel_model == original


@pytest.mark.parametrize("fractions", [(1.0, 0.5), (1.0, 0.5, 0.5, 0.0)])
def test_diagnosis_rejects_incomplete_or_repeated_fractions(parallel_model, fractions):
    with pytest.raises(ValueError, match="fractions"):
        diagnose_outage(parallel_model, "L_OUT", fractions=fractions)


def test_relief_alone_is_indeterminate():
    homotopy = [{"admittance_fraction": 1.0, "energized_status": {"ok": True},
                 "minimum_voltage": {"u_pu": 1.0}, "q_limited_generators": []},
                {"admittance_fraction": 0.0, "energized_status": {"ok": False},
                 "minimum_voltage": None}]
    reductions = [{"load_reduction_fraction": 0.01, "energized_status": {"ok": True}}]
    category, _ = _classification(homotopy, reductions)
    assert category == "indeterminado"


def test_convergent_trend_and_cold_repeat_support_physical_hypothesis():
    homotopy = [
        {"admittance_fraction": 1.0, "energized_status": {"ok": True},
         "minimum_voltage": {"u_pu": 1.0}, "q_limited_generators": []},
        {"admittance_fraction": 0.5, "energized_status": {"ok": True},
         "minimum_voltage": {"u_pu": 0.95}, "q_limited_generators": []},
        {"admittance_fraction": 0.1, "energized_status": {"ok": True},
         "minimum_voltage": {"u_pu": 0.75}, "q_limited_generators": [],
         "cold_start_repeat": {"energized_ok": True, "minimum_voltage_pu": 0.75}},
        {"admittance_fraction": 0.0, "energized_status": {"ok": False}, "minimum_voltage": None},
    ]
    reductions = [{"load_reduction_fraction": 0.2, "energized_status": {"ok": True}}]
    category, _ = _classification(homotopy, reductions)
    assert category == "probable_physical_limit"

