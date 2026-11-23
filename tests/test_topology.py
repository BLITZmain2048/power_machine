"""Pruebas migradas de conectividad con una red sintética."""

from power_machine.topology import build_topology


def test_acoplador_cerrado_y_puerto_abierto():
    model = {
        "buses": [{"id": key, "outserv": 0} for key in "abc"],
        "switches": [
            {"id": "coupler", "class": "ElmCoup", "bus1": "a", "bus2": "b", "closed": True, "outserv": 0},
            {"id": "port", "class": "StaSwitch", "element_id": "line", "port": "bus1", "closed": False, "outserv": 0},
        ],
        "branches": [{"id": "line", "class": "ElmLne", "bus_from": "b", "bus_to": "c", "outserv": 0}],
        "branches3": [], "shunts": [], "loads": [],
        "gens": [{"id": "slack", "bus": "a", "ref": True, "outserv": 0}],
    }
    topology = build_topology(model)
    assert topology["bus_to_node"]["a"] == topology["bus_to_node"]["b"]
    assert topology["bus_to_node"]["b"] != topology["bus_to_node"]["c"]
    assert "line" not in topology["active"]["branches"]


def test_barra_fuera_de_servicio_excluye_rama():
    model = {"buses": [{"id": "a", "outserv": 0}, {"id": "b", "outserv": 1}],
             "switches": [], "branches": [{"id": "ab", "class": "ElmLne", "bus_from": "a", "bus_to": "b", "outserv": 0}],
             "branches3": [], "shunts": [], "loads": [], "gens": []}
    topology = build_topology(model)
    assert "b" not in topology["bus_to_node"]
    assert "ab" not in topology["active"]["branches"]
