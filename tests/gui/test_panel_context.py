from gui_support import ieee_model, ieee_root
import pytest
pytestmark = pytest.mark.gui
'Adaptación comprobada del sobre de sensibilidades, sin aceptar huellas ajenas.'
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from power_machine.gui.panel_context import PanelContext, fingerprint

def test_sensitivity_native_scope_preserved_and_wrong_hash_rejected():
    root = Path(__file__).resolve().parents[1]
    study = {'name': 'Caso de integración', 'scenario': {'load_scale': {'load': 1.01}}}
    document = SimpleNamespace(snapshot=lambda : study.copy())
    (callbacks, logs, results) = ([], [], [])
    context = PanelContext(None, root, lambda : document, lambda : {}, log=lambda *args: logs.append(args), select_element=lambda ident: None, on_result=results.append)

    def submit(module, request, callback):
        callbacks.append(callback)
        return SimpleNamespace(finished=Event())
    context.worker = SimpleNamespace(submit=submit, cancel=lambda : None, shutdown=lambda : None)
    request = {'module': 'sensitivities', 'root': str(root), 'snapshot': context.get_snapshot()}
    context.submit_job('sensitivities', request, lambda message: context.publish_result(message[1]))
    native = fingerprint({'scenario': study['scenario']})
    result = {'module': 'sensitivities', 'status': 'completed', 'model_sha256': fingerprint({}), 'study_sha256': native}
    callbacks[0](('result', result))
    assert len(results) == 1
    assert results[0]['study_sha256'] == fingerprint(study)
    assert results[0]['provenance']['engine_study_sha256'] == native
    callbacks[0](('result', {**result, 'study_sha256': '0' * 64}))
    assert len(results) == 1
    assert logs[-1][0] == 'ERROR' and 'study_sha256' in logs[-1][1]
    context.close()

def test_integrated_plot_preserves_units_and_frequency_base():
    import numpy as np
    from power_machine.gui.app_plots import EngineeringPlotPage
    from power_machine.gui.plots import SignalBuffer
    data = SignalBuffer()
    data.reset(['time_s', 'bar|m:u|p.u.', 'gen|m:P:bus1|MW', 'gen|frequency|Hz'])
    data.append([[0, 1.03, 24, 60], [1, 1.02, 26, 59.8]])
    page = SimpleNamespace(_frequency_options=lambda : ('deviation', 60.0))
    for (key, unit, values) in [('bar|m:u|p.u.', 'p.u.', [1.03, 1.02]), ('gen|m:P:bus1|MW', 'MW', [24, 26]), ('gen|frequency|Hz', 'Hz de desviación', [0, -0.2])]:
        (x, y, actual_unit) = EngineeringPlotPage._series(page, data, key)
        assert actual_unit == unit
        assert np.allclose(y, values)
    assert data.rows[1][-1] == 59.8
