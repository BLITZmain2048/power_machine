from gui_support import ieee_model, ieee_root
"""panel integration-1, registro, contexto y trabajos sin crear una ventana real."""
from copy import deepcopy
from dataclasses import asdict
import importlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace, MethodType

import pytest

pytest.importorskip("tkinter")

from power_machine.gui.panels.registry import PanelRegistry, PANEL_MODULES, JOB_MODULES, job_entrypoint
from power_machine.gui.panel_context import PanelContext, fingerprint, cancel_panel_timers
from power_machine.gui.parity_worker import ParityWorker
from power_machine.gui.document import Document
from power_machine.study import StudyCase
from gui_support import parallel_model


class Scheduler:
    """Simula after en el mismo hilo que la prueba, sin Tcl ni pantalla."""
    def __init__(self):
        self.pending = {}
        self.sequence = 0

    def after(self, delay_ms, callback):
        self.sequence += 1
        self.pending[self.sequence] = callback
        return self.sequence

    def after_cancel(self, ident):
        self.pending.pop(ident, None)

    def drain(self):
        for _ in range(100):
            if not self.pending:
                return
            ident = next(iter(self.pending))
            self.pending.pop(ident)()
        raise AssertionError("El sondeo no terminó")


def finish(job, scheduler):
    job.thread.join(timeout=10)
    assert not job.thread.is_alive()
    scheduler.drain()


@pytest.fixture
def active(tmp_path, parallel_model):
    document = Document(StudyCase(name="Integración áé", scenario={}, ldf_options={"q_limits": False}))
    state = {"document": document, "model": parallel_model}
    scheduler, logs, results, selections = Scheduler(), [], [], []
    context = PanelContext(scheduler, tmp_path, lambda: state["document"], lambda: state["model"],
                           log=lambda *args: logs.append(args), select_element=selections.append,
                           on_result=results.append)
    yield context, state, scheduler, logs, results, selections
    context.close()


def test_thirteen_unique_descriptors_and_entrypoints():
    registry = PanelRegistry()
    assert not registry.errors
    expected = {'shortcircuit','contingency','modal','network_editor','protection','dsl_editor',
                'calc_pvqv','calc_sens','calc_ccbar','calc_cct','calc_met','calc_qds','calc_lfctl'}
    assert set(registry.entries) == expected
    assert len(registry.entries) == len(PANEL_MODULES) == 13
    assert registry.ordered() == sorted(registry.entries.values(), key=lambda entry:(entry.order,entry.id))
    assert all(callable(job_entrypoint(name)) for name in JOB_MODULES)
    for entry in registry.ordered():
        module = importlib.import_module("power_machine.gui.panels." + entry.id)
        assert entry.factory is module.create_panel


@pytest.mark.parametrize("failure", ["import", "api", "duplicate", "factory", "order"])
def test_isolated_descriptor_failure(failure):
    def module(ident):
        return SimpleNamespace(PANEL_SPEC={"id": ident, "title": ident, "order": 1,
                                          "api_version": 1, "factory": "create_panel"},
                               create_panel=lambda parent, context: (parent, context))
    good, broken, last = module("good"), module("broken"), module("last")
    if failure == "api":
        broken.PANEL_SPEC["api_version"] = 2
    elif failure == "duplicate":
        broken.PANEL_SPEC["id"] = "good"
    elif failure == "factory":
        broken.create_panel = None
    elif failure == "order":
        broken.PANEL_SPEC["order"] = "1"
    def load(name):
        if name == "broken" and failure == "import":
            raise ImportError("Panel no disponible")
        return {"good": good, "broken": broken, "last": last}[name]
    registry = PanelRegistry(("good", "broken", "last"), importer=load)
    assert set(registry.entries) == {"good", "last"}
    assert set(registry.errors) == {"broken"}
    assert registry.create("last", "parent", "context") == ("parent", "context")


def test_snapshot_is_independent_and_hashes_match_modules(active):
    context, state, _, _, _, _ = active
    snapshot = context.get_snapshot()
    for module in ("shortcircuit.results", "contingency.results", "modal.results", "protection.results", "dsl_editor.schema"):
        external = importlib.import_module("power_machine." + module).fingerprint
        assert external(snapshot["model"]) == snapshot["fingerprints"]["model_sha256"]
        assert external(snapshot["study"]) == snapshot["fingerprints"]["study_sha256"]
    snapshot["model"]["buses"][0]["name"] = "Alterada"
    snapshot["study"]["name"] = "Alterado"
    assert state["model"]["buses"][0]["name"] != "Alterada"
    assert state["document"].study.name == "Integración áé"
    assert context.get_snapshot()["fingerprints"]["variant_sha256"] is None


@pytest.mark.parametrize("panel_id", list(PanelRegistry().entries))
def test_real_panel_controller_dispatch(active, tmp_path, panel_id, tk_root):
    context, state, scheduler, logs, results, selections = active
    snapshot = context.get_snapshot()
    assert snapshot['model']['buses']
    assert snapshot['fingerprints']['model_sha256'] == fingerprint(state['model'])
    widget = PanelRegistry().create(panel_id, tk_root, context)
    widget.pack(fill='both', expand=True)
    tk_root.update()
    assert widget.winfo_width() > 1
    widget.destroy()


def test_worker_is_async_and_copies_request():
    scheduler, messages, errors = Scheduler(), [], []
    started, release = threading.Event(), threading.Event()
    identities = []
    def execute(request, *, cancel, progress):
        identities.append(threading.get_ident())
        started.set()
        assert release.wait(2)
        progress({"unit": "s"})
        return request
    worker = ParityWorker(scheduler, resolver=lambda _: execute, on_error=errors.append)
    request = {"input": {"value": None}}
    main = threading.get_ident()
    job = worker.submit("modal", request, lambda message: messages.append((threading.get_ident(), message)))
    assert started.wait(2)
    request["input"]["value"] = 5
    assert not messages and worker.running
    release.set()
    finish(job, scheduler)
    assert identities != [main]
    assert [ident for ident, _ in messages] == [main, main]
    assert messages[-1][1] == ("result", {"input": {"value": None}})
    assert not errors and not worker.running


def test_cancel_handle_and_shutdown_suppress_late_callbacks():
    scheduler, messages = Scheduler(), []
    started = threading.Event()
    def execute(request, *, cancel, progress):
        started.set()
        assert cancel.wait(2)
        return {"status": "cancelled"}
    worker = ParityWorker(scheduler, resolver=lambda _: execute)
    job = worker.submit("modal", {}, messages.append)
    assert started.wait(2)
    job.cancel()
    finish(job, scheduler)
    assert messages == [("result", {"status": "cancelled"})]
    started.clear()
    job = worker.submit("modal", {}, messages.append)
    assert started.wait(2)
    worker.shutdown()
    finish(job, scheduler)
    assert len(messages) == 1 and not scheduler.pending
    with pytest.raises(RuntimeError):
        worker.submit("modal", {}, messages.append)


def test_error_does_not_break_other_jobs():
    scheduler, messages = Scheduler(), []
    def execute(request, *, cancel, progress):
        if request.get("fail"):
            raise ValueError("Fallo controlado")
        return {"ok": True}
    worker = ParityWorker(scheduler, resolver=lambda _: execute)
    jobs = [worker.submit("modal", {"fail": failure}, messages.append) for failure in (True, False)]
    for job in jobs:
        job.thread.join(2)
        assert not job.thread.is_alive()
    scheduler.drain()
    assert any(message[0] == "error" and "Fallo controlado" in message[1] for message in messages)
    assert ("result", {"ok": True}) in messages
    assert not worker.jobs


@pytest.mark.parametrize("change", ["study", "model", "document"])
def test_changed_active_state_discards_results(active, change):
    context, state, scheduler, logs, results, _ = active
    started, release = threading.Event(), threading.Event()
    messages = []
    def execute(request, *, cancel, progress):
        started.set()
        assert release.wait(2)
        progress({"step": 1})
        return {"schema": "power_machine-parity-result-v1", "module": "modal"}
    context.worker.resolver = lambda _: execute
    request = {"module": "modal", "root": str(context.root), "snapshot": context.get_snapshot()}
    job = context.submit_job("modal", request, messages.append)
    assert started.wait(2)
    if change == "study":
        state["document"].study.name = "Cambio"
    elif change == "model":
        state["model"]["buses"][0]["name"] = "Cambio"
    else:
        state["document"] = Document(StudyCase.from_json(state["document"].snapshot()))
    release.set()
    finish(job, scheduler)
    assert len(messages) == 1 and messages[0][0] == "error"
    assert not results and logs[-1][0] == "AVISO"


def test_module_root_and_json_gates(active, tmp_path):
    context, _, _, _, _, _ = active
    request = {"module": "modal", "root": str(tmp_path)}
    with pytest.raises(ValueError):
        context.submit_job("unregistered", request, lambda _: None)
    with pytest.raises(ValueError):
        context.submit_job("shortcircuit", request, lambda _: None)
    with pytest.raises(ValueError):
        context.submit_job("modal", {**request, "root": str(tmp_path.parent)}, lambda _: None)
    with pytest.raises(ValueError):
        context.submit_job("modal", {**request, "value": float("nan")}, lambda _: None)
    context.close()
    with pytest.raises(RuntimeError):
        context.get_snapshot()


class FakeWidget:
    def __init__(self, *args, **kwargs):
        self.destroyed = False
        self.tk = SimpleNamespace(call=lambda *args: ())
    def winfo_children(self):
        return []
    def pack(self, **kwargs):
        pass
    def destroy(self):
        self.destroyed = True


def test_application_open_reuse_close_and_failed_factory(active, monkeypatch):
    from power_machine.gui import app as app_module
    context, state, scheduler, logs, _, _ = active
    registry = PanelRegistry(("good", "bad"), importer=lambda name: SimpleNamespace(
        PANEL_SPEC={"id": name, "title": name, "order": 1, "api_version": 1, "factory": "create_panel"},
        create_panel=(lambda parent, ctx: FakeWidget()) if name == "good" else
        (lambda parent, ctx: (_ for _ in ()).throw(ValueError("Fábrica defectuosa")))))
    for name in ("Frame", "Button", "Label"):
        monkeypatch.setattr(app_module.ttk, name, FakeWidget)
    contexts = []
    def make_context(*args, **kwargs):
        new = PanelContext(scheduler, context.root, lambda: state["document"], lambda: state["model"],
                           log=context.log, select_element=context.select_element)
        contexts.append(new)
        return new
    monkeypatch.setattr(app_module, "PanelContext", make_context)
    tabs, selections = [], []
    application = SimpleNamespace(panel_registry=registry, parity_panels={}, root_path=context.root,
        _closed=False, capture_presentation=lambda: None,
        document=state["document"], model=state["model"], output=SimpleNamespace(log=context.log),
        select=context.select_element, publish_parity_result=lambda _: None, available=lambda: True,
        book=SimpleNamespace(add=lambda frame, **kwargs: tabs.append(frame), select=selections.append))
    cls = app_module.Application
    application.close_parity_panel = MethodType(cls.close_parity_panel, application)
    cls.open_parity_panel(application, "good")
    cls.open_parity_panel(application, "good")
    assert len(tabs) == 1 and len(contexts) == 1 and len(selections) == 2
    cls.open_parity_panel(application, "bad")
    assert set(application.parity_panels) == {"good"}
    assert contexts[-1].closed and "Fábrica defectuosa" in logs[-1][1]
    application.current_tab = lambda: tabs[0]
    application.available = lambda: False
    cls.close_tab(application)
    assert not application.parity_panels and tabs[0].destroyed and contexts[0].closed


def test_panel_timers_are_cancelled_without_affecting_application():
    cancelled = []
    scripts = {"panel_timer": ("panel_poll", "timer"), "child_timer": ("child_poll arg", "timer"),
               "app_timer": ("app_refresh", "timer")}
    def call(*args):
        return tuple(scripts) if len(args) == 2 else scripts[args[2]]
    child = SimpleNamespace(_tclCommands=["child_poll"], winfo_children=lambda: [],
                            after_cancel=cancelled.append)
    widget = SimpleNamespace(_tclCommands=["panel_poll"], winfo_children=lambda: [child],
                             tk=SimpleNamespace(call=call, splitlist=lambda text: text.split()),
                             after_cancel=cancelled.append)
    cancel_panel_timers(widget)
    assert cancelled == ["panel_timer", "child_timer"]


def test_lf_controls_registration_and_menu_command(monkeypatch):
    from power_machine.gui.panels.registry import job_entrypoint
    from power_machine.gui.coordination import PanelCoordination
    import power_machine.gui.coordination as coordination
    from power_machine.lf_controls import execute_request
    registry = PanelRegistry()
    assert 'calc_lfctl' in registry.entries
    assert job_entrypoint('lf_controls') is execute_request
    class Menu:
        def __init__(self, *args, **kwargs):
            self.items = []
        def add_command(self, **kwargs):
            self.items.append(kwargs)
        def add_cascade(self, **kwargs):
            self.items.append(kwargs)
        def index(self, *args):
            return len(self.items) - 1
    monkeypatch.setattr(coordination.tk, 'Menu', Menu)
    opened = []
    app = SimpleNamespace(menus={'calculation': Menu(), 'edit': Menu()},
                          panel_registry=registry, open_parity_panel=opened.append,
                          show_calculation_details=lambda key: None)
    PanelCoordination.install_calculation_menu(app)
    menu, index, title, module = app.calculation_items['calc_lfctl']
    assert module == 'lf_controls'
    assert app.menus['calculation'].items[index]['label'].startswith(title)
    menu.items[0]['command']()
    assert opened == ['calc_lfctl']


def test_script_console_calls_delivered_window(monkeypatch):
    from power_machine.gui.app_workspace import WorkspaceActions
    from power_machine.gui import scripting
    calls = []
    expected = object()
    def opened(parent, root):
        calls.append((parent, root))
        return expected
    monkeypatch.setattr(scripting, 'open_scripting_window', opened)
    app = SimpleNamespace(root_path=Path.cwd(), operational={})
    assert WorkspaceActions.open_script_console(app) is expected
    assert calls == [(app, app.operational)]


