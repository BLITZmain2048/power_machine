"""Smoke Tk critical clearing time, se omite solo si el entorno no ofrece servidor gráfico."""
from __future__ import annotations
from gui_support import ieee_model, ieee_root
from copy import deepcopy
import tkinter as tk
import pytest
pytestmark = pytest.mark.gui
from power_machine.gui.panels.calc_ccbar import create_panel
from power_machine.sc_sweep import execute_request
from gui_support import ROOT, fixture, request_for

def test_tk_panel_builds_executes_and_reports_error():
    from test_result_widgets import create_evidence_root
    root = create_evidence_root()
    root.withdraw()
    (model, supplemental) = fixture()
    base = request_for(model, supplemental, selected=('B1',))

    class Context:
        root = ROOT

        def __init__(self):
            (self.results, self.logs) = ([], [])

        def get_snapshot(self):
            return deepcopy(base['snapshot'])

        def submit_job(self, module, request, callback):
            callback(('result', execute_request(request)))
            return 'short_job'

        def publish_result(self, result):
            self.results.append(result)

        def log(self, level, message):
            self.logs.append((level, message))
    context = Context()
    try:
        panel = create_panel(root, context)
        panel.pack(fill='both', expand=True)
        panel.controller.submit({'method': 'iec60909', 'case': 'maximum', 'frequency_hz': 50.0, 'profile': 'iec_radial_v1', 'fault_kind': '3ph'}, selected_bus_ids=['B1'], supplemental=supplemental, supplemental_sha256=base['input']['supplemental_sha256'])
        root.update_idletasks()
        assert context.results and panel.result_tree.get_children()
        panel.controller.on_message(('error', 'error de prueba'))
        assert context.logs[-1][0] == 'error'
        panel.mark_stale('modelo modificado')
    finally:
        root.destroy()
