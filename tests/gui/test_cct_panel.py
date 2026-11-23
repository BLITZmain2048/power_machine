"""Contrato mínimo del panel critical clearing time sin modificar el registro compartido."""
from __future__ import annotations
from gui_support import ieee_model, ieee_root

from copy import deepcopy
from pathlib import Path

from power_machine.gui.panels.calc_ccbar import PANEL_SPEC, PanelController, build_request, export_rows
from gui_support import ROOT, fixture, request_for


def test_build_request_and_controller_messages(tmp_path):
    model, supplemental = fixture()
    base = request_for(model, supplemental)
    class Context:
        root = ROOT
        def __init__(self):
            self.jobs, self.results, self.logs = [], [], []
        def get_snapshot(self):
            return deepcopy(base["snapshot"])
        def submit_job(self, module, request, callback):
            self.jobs.append((module, request, callback))
            return "job"
        def publish_result(self, result):
            self.results.append(result)
        def log(self, level, message):
            self.logs.append((level, message))
    context = Context()
    built = build_request(context, {"method": "iec60909", "case": "maximum"}, selected_bus_ids=["B1"])
    assert built["module"] == "sc_sweep" and built["input"]["selected_bus_ids"] == ["B1"]
    updates = []
    controller = PanelController(context, updates.append)
    submitted = controller.submit({"method": "iec60909", "case": "maximum"}, selected_bus_ids=["B1"])
    assert submitted["module"] == "sc_sweep" and context.jobs[0][0] == "sc_sweep"
    controller.on_message(("progress", {"completed": 1, "total": 1, "bus_id": "B1"}))
    result = {"schema": "power_machine-parity-result-v1", "module": "sc_sweep", "status": "partial", "payload": {"rows": []}}
    controller.on_message(("result", result))
    controller.on_message(("error", "fallo controlado"))
    assert context.results == [result]
    assert context.logs[-1][0] == "error"


def test_export_rows_preserves_empty_values(tmp_path):
    destination = tmp_path / "cc.csv"
    export_rows([{"bus_id": "B1", "object_id": "B1", "name": "Barra", "substation": "S/E",
                  "un_kv": 220.0, "status": "partial", "ik_initial_sym_ka": None}], destination)
    text = destination.read_text(encoding="utf-8")
    assert "ik_initial_sym_ka" in text
    assert "B1" in text


def test_panel_spec_declares_factory_and_stale_contract():
    from power_machine.gui.i18n import tr
    assert {k:PANEL_SPEC[k] for k in ('id','api_version','factory','order')} == {
        'id':'calc_ccbar','api_version':1,'factory':'create_panel','order':11}
    assert PANEL_SPEC['title_key']
    assert tr(PANEL_SPEC['title_key'])
