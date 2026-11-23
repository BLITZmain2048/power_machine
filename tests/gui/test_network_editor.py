from gui_support import ieee_model, ieee_root
"""Regresión del adaptador público y recorrido reproducible del editor."""
from copy import deepcopy
from pathlib import Path
import pytest
from power_machine.gui.netedit.controller import EditorController
from power_machine.gui.netedit.forms import creation_template
from power_machine.gui.network_session import NetworkSession
from power_machine.netedit import execute_request, model_sha256
from power_machine.gui.netedit.canvas import scene_positions
from power_machine.gui.diagram.layout import build_scene, diagram_groups

from gui_support import ROOT


class Context:
    def __init__(self, model):
        self.root = ROOT
        self.model = deepcopy(model)
        self.network_session = NetworkSession(base=model)
        self.published = []
        self.logs = []
    def get_snapshot(self):
        return dict(model=deepcopy(self.model), study={})
    def select_element(self, iid):
        self.selected = iid
    def log(self, level, message):
        self.logs.append((level, message))
    def publish_result(self, result):
        self.published.append(result)
    def submit_job(self, module, request, callback):
        callback(('result', execute_request(request)))
        return 'job'


def model_for(case):
    from gui_support import analytic_history
    return analytic_history()[1].model


def additions(controller):
    base_bus = next(b for b in controller.model['buses'] if not b.get('outserv') and b.get('un_kv'))
    bus = creation_template('buses')
    bus.update(name='study barra', un_kv=base_bus['un_kv'])
    bid = controller.create('buses', bus)
    line = creation_template('branches', 'ElmLne')
    line.update(name='study línea')
    line['params'].update(R1=1, X1=10)
    lid = controller.create('branches', line, draft=True)
    controller.reconnect(lid, 'bus_from', base_bus['id'])
    controller.reconnect(lid, 'bus_to', bid)
    controller.complete_draft(lid, controller.drafts[lid])
    load = creation_template('loads')
    load.update(name='study carga', p_mw=1, q_mvar=.2)
    did = controller.create('loads', load, draft=True)
    controller.reconnect(did, 'bus', bid)
    controller.complete_draft(did, controller.drafts[did])
    return bid, lid, did


@pytest.mark.parametrize('case', ['power_machine', 'ieee39'])
def test_public_session_roundtrip_and_flow(case, tmp_path):
    context = Context(model_for(case))
    controller = EditorController(context)
    before = model_sha256(context.model)
    ids = additions(controller)
    assert all(controller.session.get_element(i) for i in ids)
    controller.submit(run_loadflow=True)
    assert controller.last_result['payload']['loadflow']
    assert controller.flow_status
    assert model_sha256(context.model) == before
    assert controller.differences()
    path = tmp_path/'session.json'
    controller.create('loads', {'class':'ElmLod', 'name':'Incompleto'}, draft=True)
    controller.save(path)
    controller.open(path)
    assert controller.drafts
    controller.submit(run_loadflow=True)
    assert controller.last_result['payload']['model']['loads'] == controller.model['loads']
    while controller.session.snapshot()['can_undo']:
        controller.undo()
    assert model_sha256(controller.model) == before
    assert not controller.drafts
    controller.redo()
    assert controller.session.get_element(ids[0])
    assert model_sha256(context.model) == before


def test_layout_matches_unifilar_and_move_preserves_flow():
    context = Context(model_for('ieee39'))
    controller = EditorController(context)
    group = diagram_groups(context.model)[0]['id']
    positions = scene_positions(context.model, controller.layout, group)
    assert positions == {i:(s.x,s.y) for i,s in build_scene(context.model, group).symbols.items()}
    bid = context.model['buses'][0]['id']
    controller.submit(run_loadflow=True)
    result = deepcopy(controller.last_result)
    controller.move(bid, 100, 100, group)
    assert controller.last_result == result
    assert not controller.differences()
    controller.select(bid)
    controller.copy()
    clone = controller.paste()
    assert clone in controller.drafts
    assert clone != bid


def test_invalid_preview_draft_and_obsolete_revision():
    controller = EditorController(Context(model_for('ieee39')))
    before = model_sha256(controller.model)
    with pytest.raises(ValueError):
        controller.create('buses', {'class':'ElmTerm', 'un_kv':None})
    assert model_sha256(controller.model) == before
    rev = controller.session.snapshot()['revision']
    controller.create('buses', {'class':'ElmTerm'}, draft=True)
    assert not controller.session.commit([], rev)['accepted']
    iid = next(iter(controller.drafts))
    with pytest.raises(ValueError):
        controller.complete_draft(iid, controller.drafts[iid])
    assert iid in controller.drafts
    controller.undo()
    assert not controller.drafts


def test_changed_base_rejects_late_result():
    context = Context(model_for('ieee39'))
    pending = []
    context.submit_job = lambda module, request, callback: pending.append((request, callback))
    controller = EditorController(context)
    controller.submit(run_loadflow=True)
    request, callback = pending[0]
    result = execute_request(request)
    context.model['buses'][0]['un_kv'] += 1
    callback(('result', result))
    assert controller.last_result is None
    assert not context.published
