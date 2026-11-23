"""Contrato mínimo del controlador sensitivities sin abrir tkinter."""
from __future__ import annotations
from gui_support import ieee_model, ieee_root

import copy
import json
from pathlib import Path
import tkinter as tk


import power_machine.gui.panels.calc_sens as panel
import pytest
from test_result_widgets import create_evidence_root
from power_machine.gui.panels.calc_sens import PANEL_SPEC, PanelController, build_request
from power_machine.sensitivities import SensitivityOptions, execute_request
from power_machine.sensitivities.results import fingerprint


from gui_support import ROOT


def test_controller_submits_and_publishes_invalid_case(tmp_path):
    model, study = {"meta": {}, "buses": [], "branches": []}, {"scenario": {}}
    snapshot = {"model": model, "study": study, "fingerprints": {"model_sha256": fingerprint(model), "study_sha256": fingerprint(study)}}
    class Context:
        root = tmp_path
        def __init__(self):
            self.jobs, self.logs, self.results = [], [], []
        def get_snapshot(self):
            return copy.deepcopy(snapshot)
        def submit_job(self, module, request, callback):
            self.jobs.append((module, request))
            callback(("result", execute_request(request)))
            return "job"
        def log(self, level, text):
            self.logs.append((level, text))
        def publish_result(self, result):
            self.results.append(result)
    context = Context()
    controller = PanelController(context)
    options = SensitivityOptions("missing", "also_missing")
    assert build_request(context, options)["module"] == "sensitivities"
    controller.submit(options)
    assert context.jobs[0][0] == "sensitivities" and context.results[0]["status"] == "invalid_input"
    assert "PowerFactory" in context.logs[0][1]
    assert PANEL_SPEC["id"] == "calc_sens" and PANEL_SPEC["api_version"] == 1


def test_factory_builds_without_display(monkeypatch, tmp_path):
    buttons = []
    class Variable:
        def __init__(self, _master, value=False):
            self.value = value
        def get(self):
            return self.value
        def set(self, value):
            self.value = value
    class Widget:
        def __init__(self, *args, **kwargs):
            self.command = kwargs.get("command")
            if self.command:
                buttons.append(self)
        def grid(self, *args, **kwargs):
            pass
        def pack(self, *args, **kwargs):
            pass
        def bind(self, *args, **kwargs):
            pass
        def configure(self, *args, **kwargs):
            pass
        def columnconfigure(self, *args, **kwargs):
            pass
        rowconfigure = columnconfigure
        def create_window(self, *args, **kwargs):
            return 1
        def bbox(self, *args, **kwargs):
            return (0, 0, 1, 1)
        def itemconfigure(self, *args, **kwargs):
            pass
        def yview(self, *args, **kwargs):
            pass
        def set(self, *args, **kwargs):
            return ""
        def heading(self, *args, **kwargs):
            pass
        def column(self, *args, **kwargs):
            pass
        def get_children(self, *args):
            return ()
    class Context:
        root = tmp_path
        def get_snapshot(self):
            return {"model": {"buses": [], "branches": []}}
    monkeypatch.setattr(panel.tk, "Canvas", Widget)
    monkeypatch.setattr(panel.tk, "StringVar", Variable)
    monkeypatch.setattr(panel.tk, "BooleanVar", Variable)
    for name in ("Frame", "Scrollbar", "Label", "Combobox", "Entry", "Checkbutton", "Button", "Treeview"):
        monkeypatch.setattr(panel.ttk, name, Widget)
    frame = panel.create_panel(None, Context())
    frame.mark_stale("escenario modificado")
    assert isinstance(frame, Widget)
    assert hasattr(frame, "controller") and len(buttons) >= 3


def _tk_available():
    try:
        root = create_evidence_root()
        root.withdraw()
        root.destroy()
    except tk.TclError:
        return False
    return True


@pytest.mark.skipif(not _tk_available(), reason="Tk no está disponible en el intérprete de prueba")
def test_tk_real_power_machine_and_ieee39_execute_show_result():
    class Context:
        root = ROOT
        def __init__(self, model):
            study = {"schema": "power_machine-study-v1", "scenario": {}}
            self.snapshot = {"model": model, "study": study, "fingerprints": {
                "model_sha256": fingerprint(model), "study_sha256": fingerprint(study)}}
            self.results = []
        def get_snapshot(self):
            return copy.deepcopy(self.snapshot)
        def submit_job(self, _module, request, callback):
            callback(("result", execute_request(request)))
            return None
        def log(self, _level, _text):
            pass
        def publish_result(self, result):
            self.results.append(result)
    root = create_evidence_root()
    root.withdraw()
    cases = (("cases/ieee39/model.json", SensitivityOptions("B01", "L01-02", q_limits=False)),
             ("cases/ieee39/model.json", SensitivityOptions("B01", "L01-02", step_p_mw=.5, step_q_mvar=.5, q_limits=False)))
    try:
        for path, options in cases:
            context = Context(json.loads((ROOT / path).read_text(encoding="utf-8")))
            frame = panel.create_panel(root, context)
            frame.controller.submit(options)
            root.update_idletasks()
            if frame.capability_gate.last["evaluable"]:
                assert context.results[0]["status"] == "completed"
                assert frame.controller.result["payload"]["branches"]
            else:
                assert not context.results
                assert any(row.get("code") in ("SENS-01", "SENS-02") for row in frame.capability_gate.last.get("blockers", []))
            frame.destroy()
    finally:
        root.destroy()
