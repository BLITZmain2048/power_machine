"""Shared fixtures for engine and public facade tests."""

from __future__ import annotations

import copy

import pytest


@pytest.fixture(scope="session")
def ieee39():
    """Return the IEEE 39 case through the public API."""
    from power_machine.api import load_case

    return load_case("ieee39")


@pytest.fixture
def red_sintetica():
    """Return a three-bus test network with a 100 MVA system base."""
    model = {
        "schema": "power-machine-model-v1",
        "meta": {"s_base_mva": 100.0, "f_nom_hz": 50.0},
        "buses": [
            {"id": "B1", "name": "Barra 1", "un_kv": 110.0, "type": "slack"},
            {"id": "B2", "name": "Barra 2", "un_kv": 110.0, "type": "pq"},
            {"id": "B3", "name": "Barra 3", "un_kv": 110.0, "type": "pq"},
        ],
        "branches": [
            {"id": "L12", "class": "ElmLne", "bus_from": "B1", "bus_to": "B2",
             "r_pu": 0.01, "x_pu": 0.10, "b_pu": 0.0, "rate_a_mva": 100.0},
            {"id": "L23", "class": "ElmLne", "bus_from": "B2", "bus_to": "B3",
             "r_pu": 0.015, "x_pu": 0.12, "b_pu": 0.0, "rate_a_mva": 100.0},
        ],
        "loads": [
            {"id": "LD2", "class": "ElmLod", "bus": "B2", "p_mw": 30.0, "q_mvar": 10.0},
            {"id": "LD3", "class": "ElmLod", "bus": "B3", "p_mw": 20.0, "q_mvar": 8.0},
        ],
        "gens": [
            {"id": "G1", "class": "ElmSym", "bus": "B1", "p_mw": 55.0, "q_mvar": 20.0,
             "ref": True, "v_set_pu": 1.0},
        ],
        "switches": [],
        "shunts": [],
    }
    return copy.deepcopy(model)


def pytest_collection_modifyitems(items):
    import os
    reason = "External reference data is not distributed; set POWER_MACHINE_REF_IEEE39 to your own export"
    for item in items:
        if item.get_closest_marker("refdata") and not os.environ.get("POWER_MACHINE_REF_IEEE39"):
            item.add_marker(pytest.mark.skip(reason=reason))
