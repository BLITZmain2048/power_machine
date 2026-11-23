from gui_support import ieee_model, ieee_root
import copy
from pathlib import Path


import pytest
from test_result_widgets import create_evidence_root
from power_machine.gui.panels.calc_met import PANEL_SPEC, PanelController, build_request
from power_machine.dyn_metrics import execute_request


from gui_support import ROOT


class _Job:
    def __init__(self):
        self.cancelled = False

    def cancel(self):
        self.cancelled = True


class _Context:
    root = ROOT

    def __init__(self):
        self.logs = []
        self.published = []
        self.job = _Job()

    def get_snapshot(self):
        return {"model": {"schema": "power_machine-model-v1"}, "study": {}, "fingerprints": {}}

    def submit_job(self, module, request, callback):
        self.submitted = (module, request, callback)
        return self.job

    def log(self, level, message):
        self.logs.append((level, message))

    def publish_result(self, result):
        self.published.append(copy.deepcopy(result))


def test_controller_builds_request_publishes_result_and_cancels():
    context = _Context()
    controller = PanelController(context)
    request = controller.submit(run_directory="cases/ieee39/results/BASE", event_time_s=0.0)
    assert PANEL_SPEC["api_version"] == 1
    assert request["module"] == "dyn_metrics"
    result = execute_request(request)
    assert result["status"] in ("partial", "invalid_input")
    assert result.get("diagnostics")
    context.submitted[2](result)
    assert controller.result == result
    assert context.published == [result]
    controller.cancel()
    assert context.job.cancelled


def test_build_request_and_tk_panel_smoke():
    context = _Context()
    request = build_request(context, "cases/ieee39/results/BASE", event_time_s=0.0)
    assert request["input"]["run_directory"] == "cases/ieee39/results/BASE"
    tkinter = pytest.importorskip("tkinter")
    try:
        root = create_evidence_root()
    except tkinter.TclError:
        pytest.skip("Tk sin pantalla disponible")
    try:
        from power_machine.gui.panels.calc_met import create_panel
        panel = create_panel(root, context)
        panel.mark_stale("Cambio de escenario")
        root.update_idletasks()
        assert panel.controller is not None
    finally:
        root.destroy()
