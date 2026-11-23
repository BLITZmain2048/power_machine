"""Cobertura de red SC basada solo en el fixture analítico local."""

from __future__ import annotations

from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions, run_shortcircuit
from power_machine.shortcircuit.sequences import prepare_network

from test_shortcircuit_analytical import datum, fixture


def test_zpu_dirigida_conserva_matriz_no_simetrica():
    model, data = fixture(two_buses=True)
    model["branches"][0]["class"] = "ElmZpu"
    data.elements["L1"]["parameters"].update(
        ratio_pu=datum(1.2 + 0.1j, "pu"), z1_reverse=datum(0.1 + 0.3j),
        y1_from=datum(0.01 + 0.02j, "S"), y1_to=datum(0.02 + 0.03j, "S"),
    )
    network = prepare_network(model, data, ShortCircuitOptions("iec60909", "maximum"), "3ph")
    assert not [item for item in network["diagnostics"] if item["level"] == "error"]


def test_falla_en_barra_desconocida_es_entrada_invalida():
    model, data = fixture()
    result = run_shortcircuit(model, FaultSpec("B9", "3ph"), ShortCircuitOptions("iec60909", "maximum"), data)
    assert result.status == "invalid_input"
    assert any(item["code"] == "invalid_fault_bus" for item in result.diagnostics)
