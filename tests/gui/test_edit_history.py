from power_machine.gui.i18n import tr
from gui_support import ieee_model, ieee_root
"""Integración de órdenes de red y documento, sin ventana real."""
from copy import deepcopy
from types import SimpleNamespace, MethodType
import pytest

pytest.importorskip('tkinter')

from power_machine.gui.document import Document
from power_machine.gui.netedit.controller import EditorController
from power_machine.gui.netedit.forms import creation_template
from power_machine.study import StudyCase
from gui_support import analytic_history
from gui_support import FakeContext


def capture(controller):
    state=controller.session.snapshot()
    return {key:deepcopy(state[key]) for key in ('model','variant','layout','drafts')}


@pytest.fixture
def editor(tmp_path):
    _, history, ids = analytic_history()
    document = Document(StudyCase(name='Original'))
    from power_machine.netedit import new_variant
    from power_machine.netedit.schema import variant_to_dict
    document.restore_network(history.model)
    document.network.load_data(history.model,{'schema':'power_machine-gui-network-v1',
        'variant':variant_to_dict(new_variant(history.model)),'drafts':{}})
    context=FakeContext(history.model)
    context.network_session=document.network
    controller=EditorController(context)
    changed=[]
    document.network.subscribe(lambda event:changed.append(document.network.model))
    document.save(tmp_path/'initial.power_machine')
    return document, controller, ids, changed


def test_document_redo_and_branching(editor):
    doc, c, ids, _ = editor
    doc.edit(lambda study: setattr(study, 'name', 'Segundo'))
    c.edit(ids['l1'], {'p_mw': 60.})
    assert doc.undo() and doc.study.name == 'Original'
    assert next(x for x in c.model['loads'] if x['id'] == ids['l1'])['p_mw'] == 60
    assert doc.redo() and doc.study.name == 'Segundo'
    assert doc.network.undo()
    assert next(x for x in c.model['loads'] if x['id'] == ids['l1'])['p_mw'] == 50
    assert doc.network.redo()
    assert doc.undo()
    doc.edit(lambda study: setattr(study, 'name', 'Rama'))
    assert not doc.redo()


@pytest.mark.parametrize('operation', ['create', 'move', 'retire', 'edit', 'disconnect'])
def test_every_network_edit_exact_undo_redo(editor, operation):
    doc, c, ids, changed = editor
    before = capture(c)
    if operation == 'create':
        data = creation_template('buses')
        data['un_kv'] = 10.
        c.create('buses', data)
    elif operation == 'move':
        c.move(ids['b2'], 451., -78.)
    elif operation == 'retire':
        c.retire(ids['z1'])
    elif operation == 'edit':
        c.edit(ids['l1'], {'p_mw': 60.})
    else:
        c.reconnect(ids['l1'], 'bus')
    after = capture(c)
    assert before != after and len(changed) == 1
    # Ejercita los métodos que capturaron los botones originales del panel.
    c.undo()
    assert capture(c) == before
    c.redo()
    assert capture(c) == after
    assert doc.snapshot()['name'] == 'Original'


def test_variant_switch_is_reversible(editor, tmp_path):
    doc, c, ids, _ = editor
    other = EditorController(FakeContext(c.base))
    other.edit(ids['l1'], {'p_mw': 60.})
    path = tmp_path / 'variant.json'
    other.save(path)
    c.edit(ids['l1'], {'p_mw': 70.})
    before = capture(c)
    c.open(path)
    after = capture(c)
    assert before != after
    assert c.variant.variant_id == other.variant.variant_id
    c.undo()
    assert capture(c) == before
    c.redo()
    assert capture(c) == after


def test_rejected_and_pending_edit_do_not_enter_history(editor):
    doc, c, ids, _ = editor
    before = capture(c)
    with pytest.raises(ValueError):
        c.edit(ids['l1'], {'p_mw': None})
    assert capture(c) == before
    assert not doc.network.undo()
    rejected=doc.network.undo(doc.network.snapshot()['revision']+1)
    assert not rejected['accepted'] and tr('integration.stale_revision') in str(rejected['diagnostics'])
    assert capture(c)==before


def test_closed_panel_session_reattaches_and_redo_branches(editor):
    doc, c, ids, _ = editor
    c.move(ids['b2'], 1., 2.)
    state = capture(c)
    c.close()
    context=FakeContext(c.base)
    context.network_session=doc.network
    replacement=EditorController(context)
    assert capture(replacement) == state
    assert doc.network.undo()
    replacement.move(ids['b2'], 20., 30.)
    assert not doc.network.redo()


def test_application_routes_shortcuts_by_active_tab(editor):
    from power_machine.gui.app import Application
    doc, c, ids, _ = editor
    doc.edit(lambda s: setattr(s, 'name', 'Editado'))
    c.edit(ids['l1'], {'p_mw': 60.})
    tab = ['net']
    updates = []
    app = SimpleNamespace(document=doc, parity_panels={'network_editor': ('net', None)},
        book=SimpleNamespace(select=lambda: tab[0]), available=lambda: True,
        update_document=lambda: updates.append(True), rebuild_plots=lambda: None,
        output=SimpleNamespace(log=lambda *args: None))
    app.history_step = MethodType(Application.history_step, app)
    Application.undo(app)
    assert doc.study.name == 'Editado'
    assert not updates
    Application.redo(app)
    tab[0] = 'other'
    Application.undo(app)
    assert doc.study.name == 'Original' and updates


def test_drafts_and_completed_drafts_reversible(editor):
    doc, c, ids, _ = editor
    data = creation_template('loads')
    data.update(bus=ids['b2'], p_mw=4., q_mvar=0.)
    iid = c.create('loads', data, draft=True)
    draft = capture(c)
    c.complete_draft(iid, data)
    completed = capture(c)
    c.undo()
    assert capture(c) == draft
    c.redo()
    assert capture(c) == completed


def test_save_open_network_sidecar_and_clean_state(editor, tmp_path):
    doc, c, ids, _ = editor
    assert not doc.modified
    c.edit(ids['l1'], {'p_mw': 61.})
    assert doc.modified
    path = tmp_path / 'case.power_machine'
    doc.save(path)
    assert not doc.modified
    loaded = Document.open(path, StudyCase)
    loaded.restore_network(c.base)
    assert not loaded.modified
    assert loaded.network.model == c.model
    c.undo()
    assert doc.modified
    c.redo()
    assert not doc.modified


def test_large_proposals_record_only_accepted_revisions():
    from power_machine.study import load_model
    from power_machine.netedit import new_variant,edit_command
    from power_machine.netedit.schema import variant_to_dict
    from pathlib import Path
    base=ieee_model()
    document=Document(StudyCase())
    document.restore_network(base)
    document.network.load_data(base,{'schema':'power_machine-gui-network-v1','variant':variant_to_dict(new_variant(base)),'drafts':{}})
    context=FakeContext(base)
    context.network_session=document.network
    c=EditorController(context)
    item=next(item for item in c.model['loads'] if item.get('p_mw') is not None and not item.get('outserv'))
    before=capture(c)
    revision=c.session.snapshot()['revision']
    command=edit_command(c.model,'update','loads',item['id'],{'p_mw':item['p_mw']+1})
    assert c.session.preview([command],revision)['accepted']
    assert capture(c)==before
    assert not c.session.commit([command],revision+1,'Revisión incorrecta')['accepted']
    assert capture(c)==before and not document.network.undo()
    assert c.session.commit([command],revision,'Edición aceptada')['accepted']
    assert capture(c)!=before and document.network.undo()
    assert capture(c)==before
    bad=edit_command(c.model,'update','loads',item['id'],{'p_mw':None})
    assert not c.session.commit([bad],c.session.snapshot()['revision'],'Edición inválida')['accepted']
    assert capture(c)==before and not document.network.undo()
