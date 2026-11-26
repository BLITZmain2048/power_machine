from power_machine.gui.i18n import tr
from gui_support import ieee_model, ieee_root
"""Pruebas dirigidas study con Tk real y errores de transporte."""
from copy import deepcopy
import importlib
from pathlib import Path
import tkinter as tk
from tkinter import ttk
import pytest
from test_result_widgets import create_evidence_root
from power_machine.gui.panels.panel_widgets import ElementSelector, ResultTable, significant, AdvancedOptions

from gui_support import ROOT
TASK = ROOT / "results/panel_tests"

class Context:
    root = ROOT
    def __init__(self, model=None):
        self.model = model or {"meta": {"s_base_mva": 100, "f_nom_hz": 50}, "buses": [{"id": "b", "name": "Barra", "un_kv": 220}], "gens": []}
        self.logs, self.jobs, self.results = [], [], []
    def get_snapshot(self):
        return deepcopy({"model": self.model, "study": {}, "fingerprints": {}})
    def log(self, *args):
        self.logs.append(args)
    def submit_job(self, module, request, callback):
        self.jobs.append((module, request, callback))
    def publish_result(self, result):
        self.results.append(result)
    def select_element(self, object_id):
        pass

@pytest.fixture
def window():
    from power_machine.gui.paths import configure_tk_libraries
    configure_tk_libraries()
    root = create_evidence_root()
    root.geometry("1000x600")
    try:
        yield root
    finally:
        root.destroy()


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


@pytest.mark.parametrize("name", ["shortcircuit", "contingency", "modal", "protection", "dsl_editor"])
def test_geometry_and_stale(window, name):
    panel = importlib.import_module("power_machine.gui.panels." + name).create_panel(window, Context())
    panel.pack(fill="both", expand=True)
    window.update()
    assert panel.results.winfo_height() >= 160
    for advanced in [w for w in descendants(panel.options) if isinstance(w, AdvancedOptions)]:
        advanced.open.set(True)
        advanced.toggle()
    window.update()
    for button in [w for w in descendants(panel.options) if isinstance(w, ttk.Button)]:
        panel.form.reveal(button)
        window.update()
        y = button.winfo_rooty() - panel.form.canvas.winfo_rooty()
        assert y >= 0 and y + button.winfo_height() <= panel.form.canvas.winfo_height() + 2
    panel.mark_stale("Carga modificada")
    from power_machine.gui.panels.panel_widgets import panel_status_text
    assert panel_status_text('obsoleto').upper() in panel.state_text.get() and "Carga modificada" in panel.status_reason


def test_worker_errors_visible(window):
    for name in ("contingency", "dsl_editor", "shortcircuit", "modal", "protection"):
        context = Context()
        panel = importlib.import_module("power_machine.gui.panels." + name).create_panel(window, context)
        panel.pack(fill="both", expand=True)
        controller = getattr(panel, "controller", None) or getattr(panel, "modal_controller", None) or panel.protection_controller
        if name == "dsl_editor":
            controller.submit("compile")
            context.jobs[-1][2](("error", "fallo inyectado"))
        else:
            controller.on_message(("error", "fallo inyectado"))
        assert "error" in panel.state_text.get().lower()
        assert context.logs[-1][1] == "fallo inyectado"
        assert not context.results
        panel.destroy()


def test_empty_cc_and_directory_rejected_before_read():
    from power_machine.gui.panels.shortcircuit import PanelController
    from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions
    context = Context()
    for path in ("", " ", str(ROOT)):
        with pytest.raises(ValueError):
            PanelController(context).submit(FaultSpec("b", "3ph"), ShortCircuitOptions("iec60909", "maximum"), path)
    assert not context.jobs


def test_table_selector_csv(window, tmp_path):
    context = Context()
    var = tk.StringVar(window, "b")
    selector = ElementSelector(window, context.model["buses"], context.model, var)
    assert selector.get() == "b · 220 kV" and var.get() == "b"
    selector.set("Bar")
    selector.search()
    assert len(selector.cget("values")) == 1 and var.get() == ""
    selector.set(next(iter(selector.choices)))
    selector.choose()
    assert var.get() == "b"
    table = ResultTable(window, [("i", "I [kA]")], tmp_path)
    table.set_rows([{"i": 12.34567}, {"i": None}, {"i": 0}])
    table.sort("i")
    assert [r["i"] for r in table.rows] == [0, 12.34567, None]
    path = tmp_path / "tabla.csv"
    table.export_csv(path)
    assert "12.34567" in path.read_text(encoding="utf-8-sig")
    assert significant(0) == "0" and significant(None) == tr("panel.common.no_data")


def test_modal_summary():
    from power_machine.gui.panels.modal import modal_compatibility
    text = modal_compatibility({"meta": {"s_base_mva": 100, "f_nom_hz": 60}}, ROOT)
    assert tr("panel.modal.compatible_bases") in text and "machine_admittance" in text
    assert tr("panel.modal.blocked_bases") not in text




@pytest.fixture(params=('en','es'))
def result_locale(request):
    from power_machine.gui.i18n import language,set_language
    old=language()
    set_language(request.param)
    yield request.param
    set_language(old)


def test_n1_values_filter_and_tcc_bounds(window,result_locale):
    from power_machine.gui.panels import contingency as contingency, protection as protection
    context = Context()
    panel = contingency.create_panel(window, context)
    panel.pack(fill="both", expand=True)
    window.update()
    cases = [{"case_id": str(i), "status": status, "metrics": {"min_u_pu": .987654321, "max_loading_pct": 120.12345, "unsupplied_p_mw": 0}, "outages": [{"object_id": "branch"}]} for i, status in enumerate(("completed", "limit_violation"))]
    panel.controller.on_message(("result", {"schema": "power_machine-parity-result-v1", "status": "partial", "payload": {"cases": cases}}))
    table = panel.ranking.ranked_table
    first = table.tree.get_children()[0]
    from power_machine.gui.i18n import translate_text
    assert table.tree.set(first, "state") == translate_text(contingency.STATUS_TEXT['completed'])
    assert table.tree.set(first, "u_pu") == ("0,9877" if result_locale=='es' else "0.9877")
    assert table.tree.set(first, "loading_pct") == ("120,12" if result_locale=='es' else "120.12")
    assert table.rows[0]['u_pu']==.987654321
    button = next(w for w in descendants(panel.options) if isinstance(w, ttk.Checkbutton) and w.cget("text") == tr('results.violations_only'))
    button.invoke()
    assert len(table.tree.get_children()) == 1
    panel.destroy()
    panel = protection.create_panel(window, context)
    panel.pack(fill="both", expand=True)
    window.update()
    canvas = next(w for w in descendants(panel.plot_area) if isinstance(w, tk.Canvas))
    series = [{"object_id": "analytical", "label": "Perfil analítico", "points": [(100, 10), (1000, .1)], "current_unit": "A_primary"}]
    protection.draw_tcc(canvas, series)
    for item in canvas.find_all():
        if canvas.type(item) == "text":
            x1, y1, x2, y2 = canvas.bbox(item)
            assert 0 <= x1 and x2 <= canvas.winfo_width()
            assert 0 <= y1 and y2 <= canvas.winfo_height()
