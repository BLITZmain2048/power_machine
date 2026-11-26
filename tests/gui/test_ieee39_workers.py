from gui_support import ieee_model, ieee_root
"""Carga por ruta, geometría y trabajos de benchmark sin ventana real."""
from copy import deepcopy
import json
from pathlib import Path
from queue import Queue
from threading import Event
import threading
import time
from types import SimpleNamespace
import pytest

pytest.importorskip('tkinter')

from power_machine.gui.app import benchmark_scene, benchmark_study
from power_machine.gui.worker import simulate_benchmark
from power_machine import study

from gui_support import ROOT


def test_path_load_without_implicit_overrides_and_explicit_units(tmp_path):
    model = study.load_model(ROOT, 'cases/ieee39/model.json')
    assert len(model['buses']) == 39
    assert len(model['branches']) == 46
    assert model['meta']['f_nom_hz'] == 60
    case = benchmark_study(study, model, ROOT / 'cases/ieee39/model.json', ROOT)
    assert not case.ldf_options['q_limits']
    assert case.ldf_options['gui_model_path'] == 'cases/ieee39/model.json'
    clone = tmp_path / 'other_model.json'
    clone.write_text(json.dumps(model), encoding='utf-8')
    assert study.load_model(ROOT, clone) == model
    result = study.run_loadflow(ROOT, case, model)
    assert result['converged']
    for item in study.element_index(model).values():
        assert item['object_id'] == item['id'] and item['relative_path'] == item['path']


def test_layout_has_all_equipment_deterministic_without_mutation():
    model = study.load_model(ROOT, 'cases/ieee39/model.json')
    before = deepcopy(model)
    a, b = benchmark_scene(model), benchmark_scene(model)
    assert a == b
    assert set(study.element_index(model)) <= set(a.symbols)
    assert len(a.wires) >= 46
    assert len({(a.symbols[x['id']].x, a.symbols[x['id']].y) for x in model['buses']}) == 39
    assert model == before




@pytest.mark.parametrize('event', ['E39-DIN-01', 'E39-DIN-02', 'E39-DIN-03'])
def test_dynamic_worker_real_engine_outputs_in_workspace(tmp_path, monkeypatch, event):
    import power_machine.gui.worker as worker
    monkeypatch.setattr(worker, 'run_directory', lambda root, name: tmp_path)
    q = Queue()
    model = study.load_model(ROOT, 'cases/ieee39/model.json')
    simulate_benchmark(ROOT, model, ROOT / 'cases/ieee39/model.json', event, 1.12, .01, q, Event())
    messages = []
    while not q.empty():
        messages.append(q.get_nowait())
    assert not any(m[0] == 'error' for m in messages), messages
    assert messages[-1][0] == 'done'
    assert messages[-1][1]['event'] == event
    assert messages[-1][1]['completed']
    assert any(m[0] == 'header' for m in messages)
    from power_machine.gui.plots import SignalBuffer
    from power_machine.gui.history import read_signals
    buffer = SignalBuffer()
    for message in messages:
        if message[0] == 'header':
            buffer.reset(message[1])
        elif message[0] == 'samples':
            buffer.append(message[1])
    assert buffer.rows
    assert any(key.endswith('|Hz') for key in buffer.keys)
    assert any('|m:u|' in key for key in buffer.keys)
    saved = read_signals(tmp_path / 'signals.csv')
    assert len(saved.rows) == len(buffer.rows)
    assert len(saved.keys) == len(buffer.keys)
    assert (tmp_path / 'signals.csv').exists()
    assert json.loads((tmp_path / 'model_snapshot.json').read_text(encoding='utf-8')) == model


def test_dynamic_worker_cancelled_before_export(tmp_path, monkeypatch):
    import power_machine.gui.worker as worker
    monkeypatch.setattr(worker, 'run_directory', lambda *args: pytest.fail('No debe exportar'))
    q, cancel = Queue(), Event()
    cancel.set()
    simulate_benchmark(ROOT, {}, ROOT / 'cases/ieee39/model.json', 'E39-DIN-01', 1.2, .01, q, cancel)
    assert q.get_nowait() == ('done', {'completed': False, 'cancelled': True,
                                      'wall_s': pytest.approx(0, abs=2), 'steps': 0})


def test_saved_classical_csv_keeps_event_sides_and_missing_values():
    import csv
    import math
    from power_machine.gui.history import read_signals
    from power_machine.gui.plots import SignalBuffer
    path = ROOT / 'cases/ieee39/results/E39-DIN-02/signals.csv'
    with path.open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream)
        header, rows = next(reader), list(reader)
    raw = SignalBuffer()
    raw.reset(header)
    with pytest.raises(ValueError):
        raw.append(rows)
    adapted = read_signals(path)
    assert len(adapted.rows) == len(rows)
    assert sum(row[0] == 1. for row in adapted.rows) == sum(float(row[0]) == 1. for row in rows)
    assert any(math.isnan(value) for row in adapted.rows for value in row)


def test_spawn_manager_delivers_in_main_thread_and_closes(tmp_path, monkeypatch):
    from power_machine.gui.worker import SimulationManager
    from test_panel_registry import Scheduler
    monkeypatch.setenv('POWER_MACHINE_OUTPUT_DIR', str(tmp_path))
    scheduler, messages = Scheduler(), []
    main = threading.get_ident()
    manager = SimulationManager(scheduler, lambda msg: messages.append((threading.get_ident(), msg)))
    model = study.load_model(ROOT, 'cases/ieee39/model.json')
    try:
        manager.start_benchmark(ROOT, model, ROOT / 'cases/ieee39/model.json',
                                 'E39-DIN-03', end_s=1.02, step_s=.01)
        deadline = time.monotonic() + 25
        while manager.process is not None and time.monotonic() < deadline:
            pending = list(scheduler.pending.items())
            scheduler.pending.clear()
            for _, callback in pending:
                callback()
            time.sleep(.01)
        assert manager.process is None
        assert all(ident == main for ident, _ in messages)
        assert any(msg[0] == 'done' and msg[1]['completed'] for _, msg in messages), messages
        assert not any(msg[0] == 'error' for _, msg in messages), messages
    finally:
        manager.shutdown()
    assert not scheduler.pending


def test_dynamic_cancellation_during_rk4(tmp_path, monkeypatch):
    from power_machine.benchmarks import ieee39_dynamics as engine
    import power_machine.gui.worker as worker
    cancel, queue = Event(), Queue()
    constructor = engine.ClassicalSystem
    def construct(*args):
        system = constructor(*args)
        original = system.rhs
        def rhs(state):
            value = original(state)
            cancel.set()
            return value
        system.rhs = rhs
        return system
    monkeypatch.setattr(engine, 'ClassicalSystem', construct)
    monkeypatch.setattr(worker, 'run_directory', lambda *args: pytest.fail('No debe exportar'))
    simulate_benchmark(ROOT, study.load_model(ROOT, 'cases/ieee39/model.json'),
                       ROOT / 'cases/ieee39/model.json', 'E39-DIN-01', 1.2, .01, queue, cancel)
    messages = []
    while not queue.empty():
        messages.append(queue.get_nowait())
    assert messages[-1][0] == 'done' and messages[-1][1]['cancelled'], messages


def test_rms_facade_uses_supplied_model_and_solves_its_initial_point(monkeypatch):
    import numpy as np
    model = study.load_model(ROOT, 'cases/ieee39/model.json')
    original = deepcopy(model)
    seen = {}
    def runner(root, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(state=SimpleNamespace(x=np.array([0.])), network=SimpleNamespace(node_ids=['B01']),
            initial_network_residual=np.array([0.]), initial_derivative=np.array([0.]),
            override_warnings=[], dsl_coverage=[], coverage=[])
    monkeypatch.setattr(study, 'RMSRunner', runner)
    monkeypatch.setattr(study, 'load_model', lambda *args: pytest.fail('Must load the explicitly selected IEEE 39 case'))
    case = study.StudyCase(ldf_options={'q_limits': False})
    simulation = study.Simulation(ROOT, case, model=model)
    model['loads'][0]['p_mw'] += 99
    report = simulation.initial_conditions()
    assert seen['model'] == original
    assert seen['operating_point']['bus_voltages']
    assert seen['operating_point']['generator_powers']
    assert not report['validated_operating_point']




def test_loadflow_freezes_model_and_discards_changed_result():
    from power_machine.gui.app import Application
    from power_machine.gui.document import Document
    model = study.load_model(ROOT, 'cases/ieee39/model.json')
    jobs, delivered = [], []
    app = SimpleNamespace(root_path=ROOT, api=study, model=model,
        document=Document(study.StudyCase(ldf_options={'q_limits': False})), available=lambda: True,
        output=SimpleNamespace(log=lambda *args: None), loadflow_done=delivered.append,
        start_task=lambda name, fn, callback: jobs.append((fn, callback)))
    Application.run_loadflow(app)
    app.model['loads'][0]['p_mw'] += 1000.
    result = jobs[0][0]()
    assert result['converged']
    jobs[0][1](result)
    assert not delivered


def test_benchmark_cc_action_passes_local_supplement_and_60_hz(monkeypatch):
    from power_machine.gui import app as app_module
    from power_machine.gui.document import Document
    from power_machine.gui.panel_context import PanelContext
    from test_panel_registry import Scheduler, finish
    model = study.load_model(ROOT, 'cases/ieee39/model.json')
    doc = Document(benchmark_study(study, model, ROOT / 'cases/ieee39/model.json', ROOT))
    scheduler, results = Scheduler(), []
    context = PanelContext(scheduler, ROOT, lambda: doc, lambda: model,
                           log=lambda *args: None, select_element=lambda iid: None)
    requests, resolver = [], context.worker.resolver
    def resolve(module):
        execute = resolver(module)
        def run(request, **kwargs):
            requests.append(deepcopy(request))
            return execute(request, **kwargs)
        return run
    context.worker.resolver = resolve
    app = SimpleNamespace(available=lambda: True, root_path=ROOT, model=model,
        model_path=ROOT / 'cases/ieee39/model.json', parity_panels={'shortcircuit': (None, context)},
        open_parity_panel=lambda ident: None, show_benchmark_cc=results.append,
        show_error=lambda exc: pytest.fail(str(exc)))
    monkeypatch.setattr(app_module.simpledialog, 'askstring', lambda *args, **kwargs: 'B04')
    try:
        app_module.Application.benchmark_cc(app)
        assert len(context.worker.jobs) == 1
        finish(next(iter(context.worker.jobs)), scheduler)
        completed = [result for result in results if 'progress' not in result]
        assert completed and completed[-1]['status'] == 'partial', results
        assert completed[-1]['payload']['Ik_initial_sym_ka'] > 0
        assert requests[-1]['options']['frequency_hz'] == 60.
    finally:
        context.close()


def test_cc_n1_controls_inherit_case_units_and_options(monkeypatch):
    from power_machine.gui import app as app_module
    received=[]
    panel=SimpleNamespace(configure_case=lambda config:received.append(config))
    app=SimpleNamespace(model={'meta':{'f_nom_hz':60.}},model_path=ROOT/'cases/ieee39/model.json',
                        document=SimpleNamespace(study=study.StudyCase(ldf_options={'q_limits':False})))
    app_module.Application.initialize_benchmark_controls(app,'shortcircuit',panel)
    app_module.Application.initialize_benchmark_controls(app,'contingency',panel)
    assert len(received)==2
    for config in received:
        assert config['frequency_hz']==60.
        assert config['q_limits'] is False
        assert Path(config['supplemental_path']) == ROOT/'cases/ieee39/shortcircuit_data.json'
    app_module.Application.initialize_benchmark_controls(app,'modal',SimpleNamespace())


