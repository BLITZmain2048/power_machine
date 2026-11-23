"""Integración Tk de apertura, persistencia y cálculos con datos explícitos."""
from copy import deepcopy
import json
import time

import pytest

from power_machine.gui.__main__ import create_application
from power_machine.gui.document import Document
from power_machine.gui.case_io import dynamic_directory
from power_machine.gui.benchmark_actions import network_model
from gui_support import fixture

pytestmark = pytest.mark.gui


@pytest.fixture
def app():
    import tkinter as tk
    try:
        window = create_application(case='ieee39')
    except RuntimeError as exc:
        pytest.skip(str(exc))
    assert window.document is not None
    window.maybe_save = lambda: True
    window.update()
    yield window
    window._closed = True
    window.manager.shutdown()
    window.close_parity_panels()
    window.destroy()


def pump(app, done, seconds=30):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.update()
        if done():
            return
        time.sleep(.02)
    pytest.fail('El cálculo no terminó dentro del plazo')


def test_tk_smoke_loadflow_document_and_panels(app, tmp_path):
    assert len(app.model['buses']) == 39
    app.run_loadflow()
    pump(app, lambda: not app.busy)
    assert app.result['max_mismatch_pu'] < 1e-9
    path = tmp_path / 'estudio.pmstudy'
    app.document.save(path)
    restored = Document.open(path, app.api.StudyCase)
    restored.restore_network(app.base_model)
    assert restored.snapshot() == app.document.snapshot()
    for name in ('shortcircuit', 'contingency', 'calc_ccbar', 'calc_pvqv', 'calc_sens', 'network_editor'):
        app.open_parity_panel(name)
        app.update()
        assert name in app.parity_panels


def test_tk_calculations_sc_n1_rms(app, tmp_path):
    from power_machine.gui.panels.shortcircuit import PanelController
    from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions
    from power_machine.gui.panels.contingency import PanelController as N1
    from power_machine.contingency import ContingencyOptions
    model, source = fixture()
    app.activate_network(model, None)
    app.open_parity_panel('shortcircuit')
    path = tmp_path / 'fuente_sc.json'
    path.write_text(json.dumps(source), encoding='utf-8')
    results = []
    PanelController(app.parity_panels['shortcircuit'][1], results.append).submit(
        FaultSpec('B1', '3ph'), ShortCircuitOptions('iec60909', 'maximum', frequency_hz=50), path)
    pump(app, lambda: bool(results) and 'progress' not in results[-1])
    assert results[-1]['payload']['Ik_initial_sym_ka'] > 0
    pump(app, lambda: not app.manager.running)
    app.open_ieee39()
    app.open_parity_panel('contingency')
    results = []
    N1(app.parity_panels['contingency'][1], results.append).submit(
        ContingencyOptions(), selected_ids=[app.model['branches'][0]['id']], filter_reason='Prueba de interfaz')
    pump(app, lambda: any(isinstance(item, dict) and item.get('schema') for item in results))
    result = next(item for item in reversed(results) if isinstance(item, dict) and item.get('schema'))
    assert len(result['payload']['cases']) == 1
    pump(app, lambda: not app.manager.running)
    app.document.study.options.t_end_s = .02
    app.start_simulation()
    pump(app, lambda: not app.busy and not app.manager.running)
    assert len(app.buffer.rows) == 21
    assert list(tmp_path.rglob('signals.csv'))


def test_dynamic_input_and_public_model_copy(tmp_path):
    class PublicNetwork:
        def to_model(self):
            return {'buses': [{'id': 'B1'}]}
    model = network_model(PublicNetwork())
    assert model['buses'][0]['id'] == 'B1'
    assert dynamic_directory(model) is None
    dynamic = tmp_path / 'dynamic'
    dynamic.mkdir()
    (dynamic / 'manifest.json').write_text('{}', encoding='utf-8')
    model['_dynamic_dir'] = str(dynamic)
    assert dynamic_directory(model) == dynamic.resolve()


def test_outputs_follow_configuration(app, tmp_path):
    from power_machine.gui.paths import results_root, run_directory
    assert app.root_path == tmp_path
    assert results_root(app.root_path) == tmp_path
    assert run_directory(app.root_path, 'ensayo').parent == tmp_path


def test_public_copy_of_shared_synthetic_fixture(red_sintetica):
    from power_machine.api import Network
    original = deepcopy(red_sintetica)
    model = network_model(Network(red_sintetica))
    model['buses'][0]['name'] = 'Nombre editado'
    assert red_sintetica == original


def test_user_dynamic_directory_reaches_rms_worker(app, tmp_path, monkeypatch):
    dynamic = tmp_path / 'dynamic'
    dynamic.mkdir()
    (dynamic / 'manifest.json').write_text('{}', encoding='utf-8')
    app.model['meta'].pop('benchmark', None)
    app.model['_dynamic_dir'] = str(dynamic)
    received = []
    monkeypatch.setattr(app.api, 'validate_study', lambda *args, **kwargs: [])
    monkeypatch.setattr(app.manager, 'start', lambda root, study, **kwargs: received.append(kwargs['model']))
    app.start_simulation()
    assert received[0]['_dynamic_dir'] == str(dynamic)
    app.set_busy(False)
