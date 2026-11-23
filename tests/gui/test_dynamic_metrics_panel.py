"""Contrato mínimo del panel dynamic metrics sin abrir procesos RMS ni ventanas persistentes."""
from __future__ import annotations
from gui_support import ieee_model, ieee_root

import copy
import json
from pathlib import Path
import tkinter as tk


import pytest
from test_result_widgets import create_evidence_root
from power_machine.gui.panels.calc_cct import PANEL_SPEC, PanelController, build_request, bus_labels, create_panel
from power_machine.cct import CCTInput, CCTOptions


from gui_support import ROOT


class Context:
    def __init__(self):
        self.root = ROOT
        self.model = json.loads((ROOT / "cases/ieee39/model.json").read_text(encoding="utf-8"))
        self.study = {"schema": "power_machine-study-v1", "scenario": {}, "ldf_options": {"q_limits": True}}
        self.published, self.logs, self.request = [], [], None

    def get_snapshot(self):
        from power_machine.cct.results import fingerprint
        return {"model": copy.deepcopy(self.model), "study": copy.deepcopy(self.study),
                "fingerprints": {"model_sha256": fingerprint(self.model), "study_sha256": fingerprint(self.study),
                                 "variant_sha256": None}}

    def submit_job(self, module, request, callback):
        self.request = request
        return None

    def publish_result(self, result):
        self.published.append(result)

    def log(self, *args):
        self.logs.append(args)


def test_panel_request_and_result_contract():
    context, shown = Context(), []
    labels = bus_labels(context.model)
    assert any(value == "B16" for value in labels.values())
    request = build_request(context, CCTOptions(end_s=3, upper_clearing_s=.3, step_ms=5),
                            CCTInput(engine="ieee39_classical", fault_bus_id="B16"))
    assert request["module"] == "cct"
    controller = PanelController(context, shown.append)
    controller.submit(CCTOptions(end_s=3, upper_clearing_s=.3, step_ms=5),
                      CCTInput(engine="ieee39_classical", fault_bus_id="B16"))
    controller.on_message(("result", {"schema": "power_machine-parity-result-v1", "module": "cct",
                                       "status": "completed", "payload": {"simulation_count": 2}}))
    assert context.published and shown[-1]["status"] == "completed"


def test_panel_tk_builds_and_marks_stale():
    try:
        root = create_evidence_root()
    except tk.TclError:
        pytest.skip("Tk no dispone de pantalla en este entorno")
    try:
        panel = create_panel(root, Context())
        panel.mark_stale("cambio de escenario")
        assert PANEL_SPEC["api_version"] == 1
        assert hasattr(panel, "controller")
    finally:
        root.destroy()
