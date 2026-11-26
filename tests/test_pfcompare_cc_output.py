"""Regression coverage for caller-selected PF short-circuit output roots."""
from pathlib import Path
import pytest
from power_machine.pfcompare import cc


def test_writer_accepts_explicit_root_and_rejects_parent(tmp_path, monkeypatch):
    monkeypatch.delenv("POWER_MACHINE_OUTPUT_DIR", raising=False)
    root = tmp_path / "caller"
    cc.write_table(root / "nested" / "table.csv", [{"value": 1}], output_dir=root)
    assert cc.read_csv(root / "nested" / "table.csv") == [{"value": "1"}]
    with pytest.raises(ValueError, match="working directory"):
        cc.write_table(root / ".." / "escape.csv", [{"value": 1}], output_dir=root)
    assert not (tmp_path / "escape.csv").exists()


@pytest.mark.pfdata
def test_environment_is_default_and_caller_can_choose_root(tmp_path, monkeypatch):
    root = tmp_path / "configured"
    monkeypatch.setenv("POWER_MACHINE_OUTPUT_DIR", str(root))
    cc.write_table(root / "table.csv", [{"value": 2}])
    outside = tmp_path / "caller"
    with pytest.raises(ValueError, match="working directory"):
        cc.write_table(outside / "table.csv", [{"value": 2}])
    assert not outside.exists()
    cc.write_table(outside / "table.csv", [{"value": 2}], output_dir=outside)
    assert (outside / "table.csv").is_file()
    with pytest.raises(ValueError, match="working directory"):
        cc.write_table(outside / ".." / "escape.csv", [{"value": 2}], output_dir=outside)
    result = cc.run_comparisons(output_dir=outside / "comparison")
    assert len(result["buses"]) == 39


@pytest.mark.pfdata
def test_run_accepts_caller_root_without_environment(tmp_path, monkeypatch):
    monkeypatch.delenv("POWER_MACHINE_OUTPUT_DIR", raising=False)
    root = tmp_path / "caller"
    result = cc.run_comparisons(output_dir=root)
    assert len(result["buses"]) == 39
    assert len(cc.read_csv(root / "E39-CC-01" / "buses.csv")) == 39
