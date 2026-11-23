"""Contratos visibles de los paneles P3 de GUI v2."""
from __future__ import annotations

import json
from pathlib import Path

from power_machine.gui.panels.calc_cct import _result_rows
from power_machine.gui.panels.calc_met import metric_detail_rows
from power_machine.gui.panels.calc_qds import qds_columns


ROOT = Path(__file__).resolve().parents[2]
PANELS = ("cct", "metrics", "qds", "modal", "dsl")


def test_p3_catalogue_sections_exist_in_both_languages():
    catalogues = [json.loads((ROOT / "power_machine/gui/locale" / f"{code}.json").read_text(encoding="utf-8"))
                  for code in ("en", "es")]
    for catalogue in catalogues:
        for panel in PANELS:
            assert any(key.startswith(f"panel.{panel}.") for key in catalogue)
    assert set(catalogues[0]) == set(catalogues[1])


def test_cct_frontiers_keep_result_interval_information():
    rows = _result_rows({"payload": {"stable_frontier": {"clearing_s": .12, "stable": True},
                                      "unstable_frontier": {"clearing_s": .13, "stable": False}}})
    assert [row[0] for row in rows] == ["Estable", "Inestable"]
    assert [row[1] for row in rows] == [.12, .13]


def test_metrics_and_qds_keep_declared_units():
    details = metric_detail_rows({"payload": {"signals": [{"signal_id": "f", "metrics": {"frequency_hz": 50.0}}]}})
    assert details[0]["unit"] == "Hz"
    demand = next(columns for key, _, columns in qds_columns() if key == "demand")
    assert any("[s]" in label for _, label in demand)
