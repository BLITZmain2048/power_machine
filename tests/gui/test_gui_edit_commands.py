"""Command atomicity and explicit unsupported creation coverage."""
from copy import deepcopy
import pytest
from gui_support import ieee_model
from test_network_editor import Context
from power_machine.gui.netedit.controller import EditorController
from power_machine.gui.netedit.tools import drawing_template
from power_machine.netedit import model_sha256


def test_disconnect_and_retire_selection_undo_once():
    controller=EditorController(Context(ieee_model()))
    branch=controller.model['branches'][0]
    original=deepcopy(branch)
    controller.disconnect(branch['id'])
    changed=controller.session.get_element(branch['id'])
    assert changed['bus_from'] is None and changed['bus_to'] is None
    controller.undo()
    assert controller.session.get_element(branch['id'])['bus_from']==original['bus_from']
    assert controller.session.get_element(branch['id'])['bus_to']==original['bus_to']
    before=model_sha256(controller.model)
    ids=[item['id'] for item in controller.model['loads'][:2]]
    controller.retire_many(ids)
    assert all(controller.session.get_element(key)['outserv'] for key in ids)
    controller.undo()
    assert model_sha256(controller.model)==before


def test_draft_parameter_updates_preserve_counts_and_creation_position():
    controller=EditorController(Context(ieee_model()))
    data=drawing_template('static')
    data.update(bus='B01',editor_position=[400,250])
    identity=controller.create('gens',data,draft=True)
    controller.edit(identity,dict(name='Explicit generator',p_mw=1,q_mvar=0,v_set_pu=1,params={'sgn':100}))
    assert controller.drafts[identity]['params']['scale0']==1
    assert controller.drafts[identity]['params']['ngnum']==1
    controller.complete_draft(identity,controller.drafts[identity])
    assert controller.session.get_element(identity)['params']['pgini']==1
    assert controller.layout['groups']['__editor__'][identity]==[400,250]
    controller.undo()
    assert identity in controller.drafts
    assert controller.session.get_element(identity) is None


def test_three_winding_creation_validates_and_can_undo():
    controller=EditorController(Context(ieee_model()))
    data=drawing_template('transformer3')
    data.update(name='Explicit 3W draft',bus_hv='B01',bus_mv='B02',bus_lv='B03')
    for key in data['type_params']:
        data['type_params'][key]=100 if key.startswith('strn') else 345 if key.startswith('utrn') else 10 if key.startswith('uktr') else 0
    identity=controller.create('branches3',data,draft=True)
    before=model_sha256(controller.model)
    data['type_params'].update(nt3ag_h=0,nt3ag_m=0,nt3ag_l=0,curm3=0,pfe=0)
    controller.edit(identity,data)
    controller.complete_draft(identity,controller.drafts[identity])
    assert identity not in controller.drafts
    assert controller.session.get_element(identity)['class']=='ElmTr3'
    controller.undo()
    assert model_sha256(controller.model)==before


def test_open_element_editor_changes_language_without_losing_values(tk_root):
    from power_machine.gui.element_dialog import ElementEditor
    from power_machine.gui.i18n import language,set_language
    controller=EditorController(Context(ieee_model()))
    old=language()
    set_language('es')
    editor=ElementEditor(tk_root,controller,controller.model['loads'][0]['id'])
    try:
        editor.variables['p_mw'].set('42.5')
        assert editor._field_labels['p_mw'].cget('text')=='Potencia activa P [MW] *'
        set_language('en')
        assert editor._field_labels['p_mw'].cget('text')=='Active power P [MW] *'
        assert editor._field_labels['scale'].cget('text')=='Scale [1] *'
        assert editor.apply_button.cget('text')=='Apply'
        assert editor.variables['p_mw'].get()=='42.5'
    finally:
        editor.destroy()
        set_language(old)
