from gui_support import ieee_model, ieee_root
import pytest
pytestmark = pytest.mark.gui
'Contratos de registro, generaciones y obsolescencia de study.'
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from threading import Event
from power_machine.gui.app import loadflow_status
from power_machine.gui.coordination import PanelCoordination
from power_machine.gui.panel_context import PanelContext
from power_machine.gui.panels.registry import PanelRegistry, PANEL_MODULES, JOB_MODULES

def test_optional_panels_and_real_import_failure():
    name = 'power_machine.gui.panels.calc_cct'

    def absent(module):
        raise ModuleNotFoundError(name=module)
    registry = PanelRegistry((name,), importer=absent)
    assert registry.unavailable and (not registry.errors)

    def broken(module):
        raise ModuleNotFoundError(name='missing_dependency')
    assert PanelRegistry((name,), importer=broken).errors
    assert len(PANEL_MODULES) == 13
    assert 'power_machine.gui.panels.calc_lfctl' in PANEL_MODULES
    assert {'voltage_stability', 'sensitivities', 'sc_sweep', 'cct', 'dyn_metrics', 'qds_panel', 'lf_controls'} <= JOB_MODULES.keys()

def test_loadflow_only_passive_islands_are_called_deenergized():
    passive = {'status': 'sin_referencia', 'elements': dict(loads=[], gens=[], shunts=[])}
    result = {'islands': [{'status': 'resuelto'}, passive]}
    from power_machine.gui.i18n import tr
    assert tr('shell.lf.deenergized_islands', n=1) in loadflow_status(result)[1]
    passive['elements']['loads'] = ['load']
    assert tr('shell.lf.unsupplied_injections', n=1) in loadflow_status(result)[1]

def test_finished_result_marked_and_presentation_edit_preserved():
    state = {'scenario': {}, 'options': {'dt_s': 0.001}, 'plots': []}
    document = SimpleNamespace(snapshot=lambda : deepcopy(state))
    (titles, reasons, invalidations) = ({}, [], [])
    context = SimpleNamespace(title='Contingencias', panel=SimpleNamespace(mark_stale=reasons.append), invalidate=invalidations.append)
    app = SimpleNamespace(document=document, model={}, parity_results={'contingency': {'status': 'partial'}}, parity_panels={'contingency': ('frame', context)}, book=SimpleNamespace(tab=lambda frame, **kw: titles.update(kw)), refresh_tab_navigation=lambda : None)
    PanelCoordination.invalidate_panel_results(app)
    state['plots'].append({'axes': 1})
    PanelCoordination.invalidate_panel_results(app)
    assert not reasons
    state['scenario']['load_scale'] = {'load': 1.01}
    PanelCoordination.invalidate_panel_results(app)
    assert app.parity_results['contingency']['stale']
    assert titles['text'].endswith('(obsoleto)') and reasons == invalidations

def test_generation_discards_late_message_even_after_undo():
    root = Path(__file__).resolve().parents[1]
    document = SimpleNamespace(snapshot=lambda : {'scenario': {}})
    (callbacks, received) = ([], [])
    context = PanelContext(None, root, lambda : document, lambda : {}, log=lambda *args: None, select_element=lambda ident: None)
    handle = SimpleNamespace(finished=Event())

    def submit(module, request, callback):
        callbacks.append(callback)
        return handle
    context.worker = SimpleNamespace(submit=submit, cancel=lambda : None, shutdown=lambda : None)
    context.submit_job('modal', {'module': 'modal', 'root': str(root)}, received.append)
    callbacks[0](('result', {'module': 'modal', 'status': 'completed'}))
    accepted = received.pop()[1]
    context.publish_result(accepted)
    context.invalidate('Cambió la carga')
    context.publish_result(accepted)
    callbacks[0](('result', {'module': 'modal', 'status': 'completed'}))
    assert not received and context.results['modal']['stale']
    context.close()

def test_document_gate_keeps_cancelled_job_until_actual_finish():
    from power_machine.gui.app_jobs import document_gate
    document = SimpleNamespace()
    gate = document_gate(document)
    gate.reserve()
    handle = SimpleNamespace(finished=Event())
    gate.attach(handle)
    import pytest
    with pytest.raises(ValueError, match='cancelado'):
        document_gate(document).reserve()
    handle.finished.set()
    assert not gate.running

def test_worker_error_logged_once_and_publication_requires_ticket():
    root = Path(__file__).resolve().parents[1]
    document = SimpleNamespace(snapshot=lambda : {'scenario': {}})
    (callbacks, logs, requests) = ([], [], [])
    context = PanelContext(None, root, lambda : document, lambda : {}, log=lambda *args: logs.append(args), select_element=lambda ident: None)

    def submit(module, request, callback):
        callbacks.append(callback)
        requests.append(request)
        return SimpleNamespace(finished=Event())
    context.worker = SimpleNamespace(submit=submit, cancel=lambda : None, shutdown=lambda : None)
    request = {'module': 'modal', 'root': str(root)}
    context.submit_job('modal', request, lambda msg: context.log('ERROR', msg[1]))
    assert requests[0]['job_id'] and requests[0]['generation'] == 0
    callbacks[0](('error', 'Fallo analítico provocado'))
    assert logs == [('ERROR', 'Fallo analítico provocado')]
    context.publish_result({'module': 'modal', 'status': 'completed'})
    assert not context.results
    context.close()

def test_rms_and_panel_share_exclusion():
    import pytest
    from power_machine.gui.app import Application
    root = Path(__file__).resolve().parents[1]
    document = SimpleNamespace(snapshot=lambda : {'scenario': {}})
    logs = []
    context = PanelContext(None, root, lambda : document, lambda : {}, log=lambda *args: logs.append(args), select_element=lambda ident: None)
    context.external_busy = lambda : True
    request = {'module': 'modal', 'root': str(root)}
    with pytest.raises(ValueError, match='RMS'):
        context.submit_job('modal', request, lambda msg: None)
    context.external_busy = lambda : False
    handle = SimpleNamespace(finished=Event())
    context.worker = SimpleNamespace(submit=lambda *args: handle, cancel=lambda : None, shutdown=lambda : None)
    context.submit_job('modal', request, lambda msg: None)
    app = SimpleNamespace(document=document, busy=False, manager=SimpleNamespace(running=False), output=SimpleNamespace(log=lambda *args: logs.append(args)))
    assert not Application.available(app)
    context.close()
    assert not Application.available(app)
    handle.finished.set()
    assert Application.available(app)
