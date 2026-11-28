"""Public API contract tests for the typed 0.1.0 facade."""
from __future__ import annotations
import pytest

import inspect
import json
from pathlib import Path



def _api():
    import power_machine.api as api
    return api


def _strict(value):
    payload = value.to_dict() if hasattr(value, "to_dict") else value
    json.dumps(payload, allow_nan=False)
    return payload


def _sc_data() -> Path:
    return Path(__file__).parents[1] / "power_machine" / "cases" / "ieee39" / "shortcircuit_data.json"


def test_public_import_is_lazy_for_gui_modules():
    import subprocess, sys
    subprocess.run([sys.executable, "-c", "import sys, power_machine; assert power_machine.__version__ == '0.1.0'; assert 'tkinter' not in sys.modules; assert 'matplotlib' not in sys.modules"], check=True)



def test_typed_loadflow_tables_are_json_safe(ieee39):
    api = _api()
    result = api.loadflow(ieee39, api.LoadflowOptions())
    assert isinstance(result, api.LoadflowResult)
    assert result.status in {"completed", "partial"}
    assert result.buses.name == "buses"
    assert result.buses.units["u_pu"] == "p.u."
    assert result.buses.select("id", "u_pu").to_records()
    _strict(result)


def test_table_csv_is_confined_to_configured_output(monkeypatch, tmp_path, ieee39):
    monkeypatch.setenv("POWER_MACHINE_OUTPUT_DIR", str(tmp_path))
    table = _api().loadflow(ieee39).buses
    path = table.to_csv("voltages.csv")
    assert path == tmp_path / "voltages.csv"
    assert path.is_file()


def test_network_copy_and_model_are_independent(ieee39):
    clone = ieee39.copy()
    model = clone.to_model()
    model["buses"][0]["name"] = "local only"
    assert clone.get(model["buses"][0]["id"])["name"] != "local only"


def test_public_errors_have_code_field_and_action(ieee39):
    api = _api()
    with pytest.raises(api.UnknownElementError) as caught:
        ieee39.get("does-not-exist")
    assert caught.value.code == "unknown_element"
    assert caught.value.to_dict()["action"]
    with pytest.raises(api.MissingStudyDataError):
        api.shortcircuit(ieee39)


def test_shortcircuit_uses_explicit_sc_data(ieee39):
    result = _api().shortcircuit(ieee39, bus="B01", sc_data=_sc_data())
    assert result.faults.name == "faults"
    _strict(result)


def test_shortcircuit_sweep_preserves_the_supplied_source(ieee39):
    result = _api().shortcircuit(ieee39, sc_data=_sc_data(), selected_bus_ids=["B01"])
    assert result.faults.name == "faults"
    assert result.coverage["evaluated"] == 1
    _strict(result)


def test_contingency_exposes_stable_severity_columns(ieee39):
    api = _api()
    result = api.contingency(ieee39, elements=["L01-02"], options=api.ContingencyOptions(max_cases=1))
    row = result.cases.to_records()[0]
    assert {"unserved_mw", "max_loading_pct", "min_voltage_pu", "severity",
            "disconnected_generation_mw", "alert_priority", "outage_ids", "label"} <= set(row)
    assert row["outage_ids"] == ["L01-02"]
    assert row["label"] == "L01-02"
    assert result.cases.units["disconnected_generation_mw"] == "MW"
    assert result.cases.units["alert_priority"] == "rank"
    ranked = result.rank()
    assert ranked.name == "cases"
    assert ranked.to_records()[0]["label"] == "L01-02"


def test_contingency_combined_outages_have_a_readable_label(ieee39):
    api = _api()
    result = api.contingency(ieee39, elements=["L01-02", "G33"],
                             options=api.ContingencyOptions(orders=(2,), max_cases=10))
    row = result.cases.to_records()[0]
    assert set(row["outage_ids"]) == {"L01-02", "G33"}
    assert row["label"] == " + ".join(row["outage_ids"])


def test_options_and_unit_parameter_names_are_discoverable():
    api = _api()
    assert "t_end_s" in inspect.signature(api.rms).parameters
    assert "step_s" in inspect.signature(api.rms).parameters
    assert "sc_data" in inspect.signature(api.shortcircuit).parameters
    assert "c" not in inspect.signature(api.shortcircuit).parameters
    assert "t_end" not in inspect.signature(api.rms).parameters
    assert api.RmsOptions().linear_solver == "compatibility"
    assert api.ContingencyOptions(island_reference="none").island_reference == "none"
    with pytest.raises(api.InvalidModelError):
        api.RmsOptions(linear_solver="unknown")
    with pytest.raises(api.InvalidModelError):
        api.ContingencyOptions(island_reference="unknown")


@pytest.mark.refdata
def test_compare_reference_loadflow_reference(ieee39):
    reference = __import__("power_machine.resources", fromlist=["ref_data_root"]).ref_data_root()
    result = _api().compare_reference(ieee39, reference, "loadflow")
    assert result.status == "completed"
    _strict(result)


@pytest.mark.refdata
def test_compare_reference_network_reference(ieee39):
    reference = __import__("power_machine.resources", fromlist=["ref_data_root"]).ref_data_root()
    result = _api().compare_reference(ieee39, reference, "network")
    assert result.status == "completed"
    _strict(result)


@pytest.mark.refdata
def test_compare_reference_shortcircuit_reference(ieee39):
    reference = __import__("power_machine.resources", fromlist=["ref_data_root"]).ref_data_root()
    result = _api().compare_reference(ieee39, reference, "shortcircuit")
    assert result.status == "completed"
    assert len(result.tables["comparison"].to_records()) == 39


def test_unsupported_studies_are_public_errors(ieee39):
    api = _api()
    with pytest.raises(api.UnsupportedStudyError):
        api.cct(ieee39, {"engine": "pm_rms", "fault_bus_id": "B16"})
    with pytest.raises(api.MissingStudyDataError):
        api.quasi_dynamic(ieee39, [])


def test_cct_classical_ieee39_is_typed(ieee39):
    api = _api()
    options = api.CctOptions(lower_clearing_s=0.02, upper_clearing_s=0.30, resolution_ms=20.0,
                             end_s=3.0, step_ms=5.0, recovery_min_voltage_pu=0.5, recovery_window_s=0.1)
    result = api.cct(ieee39, {"engine": "ieee39_classical", "fault_bus_id": "B16"}, options=options)
    assert isinstance(result, api.CctResult)
    assert result.trials.name == "trials"
    _strict(result)


def test_quasi_dynamic_user_series_is_typed(tmp_path, ieee39):
    from power_machine.qss.series import Series, SeriesStep
    loads = [row["id"] for row in ieee39.to_model()["loads"]]
    series = Series("user_load_profile", (
        SeriesStep("2026-11-18T00:00:00", 0.0, {identifier: 1.0 for identifier in loads}),
        SeriesStep("2026-11-18T00:15:00", 900.0, {identifier: 1.01 for identifier in loads}),
    ), {"source": "user_supplied"})
    api = _api()
    result = api.quasi_dynamic(ieee39, series, options=api.QuasiDynamicOptions(output=tmp_path / "qss"))
    assert isinstance(result, api.QuasiDynamicResult)
    assert result.records.name == "records"
    assert (tmp_path / "qss" / "run.json").is_file()
    _strict(result)


def test_batch_is_reexported_and_uses_output_directory(monkeypatch, tmp_path, ieee39):
    import power_machine
    monkeypatch.setenv("POWER_MACHINE_OUTPUT_DIR", str(tmp_path))
    result = power_machine.run_batch(ieee39, "loadflow", [power_machine.Scenario("base")],
                                     options=power_machine.BatchOptions(workers=1), output=tmp_path / "batch")
    assert result.to_records()[0]["id"] == "base"
    assert (tmp_path / "batch" / "summary.csv").is_file()
