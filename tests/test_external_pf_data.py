"""External references are optional; public inputs remain available."""
import json
import pytest
from power_machine.resources import pf_data_root, PFReferenceDataError, readonly_path

def test_missing_external_data_has_stable_code(monkeypatch):
    monkeypatch.delenv("POWER_MACHINE_PF_IEEE39", raising=False)
    with pytest.raises(PFReferenceDataError) as caught:
        pf_data_root()
    assert caught.value.code == "pf_reference_data_not_included"
    assert "not distributed" in str(caught.value)

def test_explicit_export_overrides_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("POWER_MACHINE_PF_IEEE39", str(tmp_path / "other"))
    assert pf_data_root(tmp_path) == tmp_path.resolve()

def test_api_missing_data(monkeypatch):
    from power_machine.api import load_case, compare_powerfactory
    monkeypatch.delenv("POWER_MACHINE_PF_IEEE39", raising=False)
    with pytest.raises(PFReferenceDataError) as caught:
        compare_powerfactory(load_case("ieee39"))
    assert caught.value.code == "pf_reference_data_not_included"

@pytest.mark.gui
def test_gui_message_is_localized(monkeypatch):
    from power_machine.gui.display import public_text
    from power_machine.gui.i18n import tr
    assert public_text(PFReferenceDataError()) == tr("pf_reference_data_not_included")

def test_matpower_default_is_included():
    from power_machine.api import load_case
    assert len(load_case("ieee39").to_model()["buses"]) == 39


def test_validation_without_external_data(monkeypatch, tmp_path):
    import os, subprocess, sys
    monkeypatch.delenv("POWER_MACHINE_PF_IEEE39", raising=False)
    destination = tmp_path / "validation"
    process = subprocess.run([sys.executable, "-m", "validation", "--output", str(destination)],
                             check=True, capture_output=True, text=True)
    result = json.loads((destination / "summary.json").read_text())
    assert result["pass"]
    assert result["mode"] == "internal"
    assert result["powerfactory"]["status"] == "not included"
    assert len(result["analytical"]) == 7
    assert result["convergence"]
    assert "not included" in process.stdout
