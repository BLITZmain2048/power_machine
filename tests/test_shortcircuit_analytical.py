"""Casos analíticos de cortocircuito con fuente externa explícita."""

from __future__ import annotations

from dataclasses import asdict
import copy

import pytest

from power_machine.shortcircuit import FaultSpec, ShortCircuitData, ShortCircuitOptions, run_shortcircuit


def datum(value, unit="ohm"):
    """Construye un parámetro de suplemento SC sin recurrir a datos externos."""
    if isinstance(value, complex):
        value = {"real": value.real, "imag": value.imag}
    return {"value": value, "unit": unit, "analytical_fixture": "fuente_sintetica"}


def fixture(two_buses=False):
    """Red de una o dos barras con equivalente externo conocido."""
    model = {
        "meta": {"s_base_mva": 100.0},
        "buses": [{"id": "B1", "path": "fixture/B1", "un_kv": 10.0}],
        "branches": [],
        "gens": [{"id": "S1", "path": "fixture/S1", "class": "ElmXnet", "bus": "B1"}],
    }
    sources = {"S1": {"kind": "external", "parameters": {
        "z1": datum(0.1 + 0.2j), "z2": datum(0.1 + 0.2j), "z0": datum(0.3 + 0.6j)}}}
    elements = {}
    if two_buses:
        model["buses"].append({"id": "B2", "path": "fixture/B2", "un_kv": 10.0})
        model["branches"].append({"id": "L1", "path": "fixture/L1", "class": "ElmLne", "bus_from": "B1", "bus_to": "B2"})
        sources["S1"]["parameters"] = {"z1": datum(0.05 + 0.1j), "z2": datum(0.05 + 0.1j), "z0": datum(0.15 + 0.3j)}
        elements["L1"] = {"parameters": {"z1": datum(0.05 + 0.1j), "z2": datum(0.05 + 0.1j), "z0": datum(0.15 + 0.3j)}}
    return model, ShortCircuitData("power_machine-sc-data-v1", elements, sources)


def run(model, data, kind="3ph", bus_id="B1", **extra):
    return run_shortcircuit(model, FaultSpec(bus_id, kind), ShortCircuitOptions("iec60909", "maximum", **extra), data)


def test_fuente_externa_trifasica_es_repetible():
    model, data = fixture()
    before = copy.deepcopy((model, asdict(data)))
    result = run(model, data)
    assert result.status in {"completed", "partial"}, result.diagnostics
    assert result.quantities["Ik_initial_sym_ka"] == pytest.approx(28.4018778722, rel=1e-9)
    assert result.quantities["ip_peak_ka"] == pytest.approx(49.7527185605, rel=1e-9)
    assert (model, asdict(data)) == before


@pytest.mark.parametrize("kind", ["lg", "ll", "llg"])
def test_fallas_asimetricas_no_inventan_datos(kind):
    model, data = fixture()
    result = run(model, data, kind)
    assert result.status in {"completed", "partial"}
    assert result.quantities["phase_currents_ka"]


def test_rama_sintetica_conserva_corriente_en_falla_remota():
    model, data = fixture(two_buses=True)
    result = run(model, data, bus_id="B2")
    assert result.status in {"completed", "partial"}
    branch = next(item for item in result.contributions if item["kind"] == "branch")
    assert branch["terminals"]["from"]["phase_currents_ka"]["a"] == pytest.approx(
        result.quantities["phase_currents_ka"]["a"], rel=1e-10)
