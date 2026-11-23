from gui_support import ieee_model, ieee_root
"""Contratos de vigencia, identidad y precisión de los componentes INT-1B."""
import csv
import json
import os
from pathlib import Path
import time
import tkinter as tk
import pytest
from power_machine.gui.paths import configure_tk_libraries
configure_tk_libraries()
from power_machine.gui.panels.panel_widgets import StudyPanel, ResultTable, ElementSelector


ROOT_ATTEMPTS = []


def create_evidence_root():
    configure_tk_libraries()
    for attempt in range(3):
        try:
            return tk.Tk()
        except tk.TclError as exc:
            if attempt == 2:
                pytest.skip(f'Entorno Tk no disponible tras tres intentos: {exc}')
            time.sleep(.2)


@pytest.fixture
def window():
    root = create_evidence_root()
    root.geometry("1000x600")
    try:
        yield root
    finally:
        root.destroy()


def test_status_keeps_freshness_independent(window):
    panel = StudyPanel(window)
    panel.pack(fill="both", expand=True)
    window.update()
    panel.show_state({"status": "completed"})
    assert panel.freshness == "sin resultado"
    assert panel.coverage == "completo"
    panel.set_status("partial", freshness="vigente", validation_label="sin validar contra PF")
    panel.mark_stale("Cambió el suplemento")
    panel.show_state({"status": "completed"})
    assert panel.freshness == "obsoleto"
    assert panel.coverage == "completo"
    assert panel.validation_label == "sin validar contra PF"
    assert 330 <= panel.form.winfo_width() <= 350
    assert panel.results.winfo_height() >= 150 and panel.plot_area.winfo_height() >= 150


def test_raw_sort_filter_specs_and_csv(window, tmp_path):
    table = ResultTable(window, [("value", "Corriente")], root=tmp_path)
    table.set_column_specs([{"key": "value", "label": "Corriente", "unit": "kA", "dtype": "float", "significant_digits": 4, "missing_text": "Ausente"}])
    table.set_rows([{"value": 1.000049}, {"value": 1.000041}, {"value": None}, {"value": 0}])
    table.sort("value")
    assert [r["value"] for r in table.rows] == [0, 1.000041, 1.000049, None]
    table.sort("value")
    assert [r["value"] for r in table.rows] == [1.000049, 1.000041, 0, None]
    table.set_filter(lambda r: r["value"] is None or r["value"] > 1)
    table.export_csv(tmp_path / "visible.csv")
    table.export_csv(tmp_path / "all.csv", scope="all")
    with (tmp_path / "visible.csv").open(encoding="utf-8-sig", newline="") as stream:
        visible = list(csv.reader(stream))
    assert visible == [["Corriente [kA]"], ["1.000049"], ["1.000041"], [""]]
    with (tmp_path / "all.csv").open(encoding="utf-8-sig", newline="") as stream:
        assert len(list(csv.reader(stream))) == 5


def test_selector_refresh_and_ambiguous_typing(window):
    items = [{"id": key, "name": "Barra", "substation": "S/E", "un_kv": 220} for key in ("a", "b")]
    variable = tk.StringVar(window, "a")
    selector = ElementSelector(window, items, {"buses": items}, variable)
    assert len(selector.choices) == 2 and selector.get_id() == "a"
    selector.set_items(list(reversed(items)), {"buses": items})
    assert selector.get_id() == "a"
    selector.set("Barra")
    selector.search()
    assert selector.get_id() is None
    selector.set_id("b")
    assert selector.get_id() == "b"
    selector.set_items(items[:1], {"buses": items})
    assert selector.get_id() is None
