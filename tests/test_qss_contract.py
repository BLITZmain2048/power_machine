"""Contrato QSS con manifiesto sintético verificable."""

import hashlib
import json

import pytest

from power_machine.qss.series import read_series


def _write_inputs(tmp_path, *, duplicate_time=False):
    model = {"loads": [{"id": "LD1", "name": "Carga A"}, {"id": "LD2", "name": "Carga B"}]}
    model_path = tmp_path / "model.json"
    model_path.write_text(json.dumps(model), encoding="utf-8")
    series = "timestamp_iso;t_s;Carga A\n2026-01-01T00:00:00;0;1.0\n2026-01-01T00:15:00;{};1.1\n".format(0 if duplicate_time else 900)
    series_path = tmp_path / "serie.csv"
    series_path.write_text(series, encoding="utf-8")
    manifest = {"sha256_modelo": hashlib.sha256(model_path.read_bytes()).hexdigest(), "series": {
        "corta": {"archivo": "serie.csv", "sha256": hashlib.sha256(series_path.read_bytes()).hexdigest(), "muestras": 2},
    }}
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return model_path, manifest_path


def test_series_applies_declared_default_only_to_missing_columns(tmp_path):
    model_path, manifest_path = _write_inputs(tmp_path)
    model, series = read_series(model_path, manifest_path, "corta", default_factor=0.95)
    assert model["loads"][0]["id"] == "LD1"
    assert [step.t_s for step in series.steps] == [0.0, 900.0]
    assert series.steps[1].factors == {"LD1": 1.1, "LD2": 0.95}
    assert series.provenance["missing_columns_defaulted"] == ["LD2"]


def test_series_rejects_non_increasing_time(tmp_path):
    model_path, manifest_path = _write_inputs(tmp_path, duplicate_time=True)
    with pytest.raises(ValueError, match="estrictamente creciente"):
        read_series(model_path, manifest_path, "corta", default_factor=1.0)
