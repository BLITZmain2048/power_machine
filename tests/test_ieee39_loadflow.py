"""Caso IEEE 39 incluido, sin rutas ni exportaciones externas."""

from power_machine.api import load_case, loadflow


def test_ieee39_loadflow_is_converged_and_keeps_the_published_inventory():
    network = load_case("ieee39")
    result = loadflow(network)
    data = result.to_dict()
    assert data["status"] == "completed"
    assert "payload" not in data
    assert result.summary["converged"] is True
    assert len(result.buses.to_records()) == 39
    assert len(result.generators.to_records()) == 10
    assert result.summary["max_mismatch_mva"] < 1e-6
