"""Pruebas migradas del estado de islas de flujo de potencia."""

from power_machine.lf_status import energized_status


def test_isla_resuelta_y_aislada_sin_referencia():
    result = {"converged": False, "islands": [
        {"status": "resuelto", "elements": {"loads": ["L1"]}, "max_mismatch_mva": 1e-9},
        {"status": "sin_referencia", "elements": {"loads": ["L2"]}, "max_mismatch_mva": 1e-9},
    ]}
    status = energized_status(result)
    assert status["ok"]
    assert status["n_solved"] == 1
    assert status["n_deenergized"] == 1


def test_isla_no_convergente_es_fallo():
    result = {"converged": False, "islands": [{"status": "no_convergente", "elements": {}}]}
    assert not energized_status(result)["ok"]
