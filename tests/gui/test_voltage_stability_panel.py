from gui_support import ieee_model, ieee_root
"""Contrato del panel voltage stability sin depender del registro global."""
import json
from pathlib import Path

import pytest
from test_result_widgets import create_evidence_root
from power_machine.gui.panels import calc_qds
from power_machine.qds_panel.adapter import MODULE, execute_request, fingerprint


from gui_support import ROOT


class FakeJob:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class FakeContext:
    def __init__(self):
        self.root = ROOT
        model = json.loads((ROOT / "cases/ieee39_pf/model.json").read_text(encoding="utf-8"))
        study = {"scenario": {}, "ldf_options": {}}
        self.snapshot = {"model": model, "study": study, "fingerprints": {
            "model_sha256": fingerprint(model), "study_sha256": fingerprint(study), "variant_sha256": None}}
        self.jobs, self.published, self.logs = [], [], []

    def get_snapshot(self):
        return self.snapshot

    def submit_job(self, module, request, callback):
        job = FakeJob()
        self.jobs.append((module, request, callback, job))
        return job

    def publish_result(self, result):
        self.published.append(result)

    def log(self, level, message):
        self.logs.append((level, message))


def test_build_submit_cancel_and_stale_contract():
    context = FakeContext()
    updates = []
    controller = calc_qds.PanelController(context, updates.append)
    request = controller.submit(step_s=900.0, horizon_s=900.0)
    assert context.jobs[0][0] == MODULE
    assert request["input"]["profile"] == {"kind": "builtin_ieee39", "name": "S1_diaria_15min"}
    assert request["input"]["configuration"]["step_s"] == 900.0
    controller.on_message(("progress", {"completed": 1, "total": 2}))
    assert updates[-1]["progress"]["completed"] == 1
    controller.cancel()
    assert context.jobs[0][3].cancelled
    controller.mark_stale("El modelo cambió")
    assert updates[-1] == {"stale": "El modelo cambió"}


def test_panel_factory_minimal_tk_when_display_is_available():
    import tkinter as tk
    try:
        root = create_evidence_root()
    except tk.TclError:
        pytest.skip("Tk no dispone de pantalla en este entorno")
    try:
        root.withdraw()
        context = FakeContext()
        panel = calc_qds.create_panel(root, context)
        assert panel.controller.context.root == ROOT
        request = panel.controller.submit(step_s=900.0, horizon_s=900.0,
                                          output_dir="results/quasi_dynamic")
        result = execute_request(request)
        panel.controller.on_message(("result", result))
        root.update_idletasks()
        assert panel.controller.result["status"] == "unsupported"
        assert panel.controller.result.get("diagnostics")
        panel.controller.on_message(("error", "Error QDS de ensayo"))
        root.update_idletasks()
        panel.mark_stale("Prueba de vigencia")
    finally:
        root.destroy()
