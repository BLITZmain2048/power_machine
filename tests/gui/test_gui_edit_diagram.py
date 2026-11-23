"""Integrated drawing, load editing and command-history regressions."""
from types import SimpleNamespace
import tkinter as tk
import pytest
from power_machine.gui.__main__ import create_application
from power_machine.gui.element_dialog import ElementEditor
from power_machine.gui.netedit.integration import get_editor_controller
from power_machine.gui.i18n import language,set_language
from power_machine.gui.theme import theme
from power_machine.loadflow import run_loadflow


@pytest.fixture
def app():
    old_code,old_theme=language(),theme.name
    app=create_application(pause_s=.2)
    app.update()
    yield app
    app._closed=True
    app.manager.shutdown()
    app.close_parity_panels()
    app.destroy()
    set_language(old_code)
    theme.set(old_theme)


def event(x,y):return SimpleNamespace(x=x,y=y,state=0)


def click_bus(view,identity):
    s=view.scene.symbols[identity]
    view._press(event(*view._xy(s.x,s.y)))


def complete(view,values):
    window=view._last_element_dialog
    editor=window.editor
    for key,value in values.items():editor.variables[key].set(str(value))
    assert editor.apply(),editor.message.get()
    identity=editor.object_id
    window.destroy()
    view.app.update()
    return identity


def enable(view):
    view._edit_var.set(True)
    view.toggle_editing()
    view.app.update()


def test_create_bus_line_load_generator_and_flow(app,monkeypatch):
    view=app.diagrams[None]
    enable(view)
    controller=view.editor_controller
    assert controller is get_editor_controller(app)
    view.choose_drawing_tool('bus')
    view._press(event(300,300))
    bid=complete(view,{'name':'Added bus','un_kv':345})
    view.choose_drawing_tool('line')
    click_bus(view,'B01')
    view.drawing_motion(event(400,350))
    assert view.canvas.find_withtag('drawing_preview')
    click_bus(view,bid)
    lid=complete(view,{'name':'Added line','params.R1':1,'params.X1':10,'params.G1':0,'params.B1':0})
    view.choose_drawing_tool('load')
    click_bus(view,bid)
    load=complete(view,{'name':'Added load','p_mw':2,'q_mvar':.3,'scale':1})
    view.choose_drawing_tool('static')
    click_bus(view,bid)
    gen=complete(view,{'name':'Added gen','p_mw':1,'q_mvar':0,'v_set_pu':1,'q_min_mvar':-2,'q_max_mvar':2,'params.sgn':100})
    assert all(controller.session.get_element(key) for key in (bid,lid,load,gen))
    result=run_loadflow(app.model,options={'q_limits':False})
    assert result['converged']
    assert any(row['id']==bid for row in result['buses'])
    monkeypatch.setattr('power_machine.gui.diagram.editing.messagebox.askyesno',lambda *a,**k:True)
    view.select_ids([load])
    view.delete_selection()
    assert controller.session.get_element(load)['outserv']
    assert load not in view.scene.symbols
    view.canvas.focus_force()
    app.update()
    view.canvas.event_generate('<Control-z>')
    app.update()
    assert not controller.session.get_element(load)['outserv']
    assert load in view.scene.symbols
    assert controller.session.get_element(gen)


def test_load_edit_has_actual_flow_effect_and_validation(app):
    controller=get_editor_controller(app)
    item=next(row for row in app.model['loads'] if not row.get('outserv'))
    editor=ElementEditor(app,controller,item['id'])
    old=run_loadflow(app.model,options={'q_limits':False})
    editor.variables['p_mw'].set('25')
    editor.variables['q_mvar'].set('5')
    editor.variables['scale'].set('-1')
    assert not editor.apply()
    editor.variables['scale'].set('1.2')
    assert editor.apply(),editor.message.get()
    changed=controller.session.get_element(item['id'])
    assert changed['params']['plini']==25
    assert changed['params']['scale0']==1.2
    assert app.result is None
    result=run_loadflow(app.model,options={'q_limits':False})
    assert result['converged']
    assert result['totals']['load_p_mw']!=old['totals']['load_p_mw']
    editor.destroy()


def test_edit_mode_double_click_preview_cancel_and_readonly(app):
    view=app.diagrams[None]
    assert not view.editing
    load=app.model['loads'][0]['id']
    symbol=view.scene.symbols[load]
    view._double(event(*view._xy(symbol.x,symbol.y)))
    window=view._last_element_dialog
    assert str(window.editor.apply_button.cget('state'))=='disabled'
    window.destroy()
    enable(view)
    view.choose_drawing_tool('line')
    click_bus(view,'B01')
    view.drawing_motion(event(450,200))
    assert view.canvas.find_withtag('drawing_preview')
    view.canvas.focus_force()
    app.update()
    view.canvas.event_generate('<Escape>')
    app.update()
    assert not view.canvas.find_withtag('drawing_preview')
    assert not view._drawing_buses
    view._double(event(*view._xy(symbol.x,symbol.y)))
    assert str(view._last_element_dialog.editor.apply_button.cget('state'))=='normal'
    view._last_element_dialog.destroy()


def test_variant_panel_uses_no_duplicate_diagram_canvas(app):
    app.open_parity_panel('network_editor')
    panel=app.parity_panels['network_editor'][1].panel
    from power_machine.gui.netedit.portal import DiagramPortal
    assert isinstance(panel.canvas,DiagramPortal)
    for code in ('en','es'):
        set_language(code)
        panel.palette_box.current(2)
        assert panel.palette_key()=='line'
        panel.create()
        assert app.diagrams[None]._drawing_tool=='line'


def test_explicit_templates_and_native_generator_q_limits(app):
    from power_machine.gui.netedit.tools import TOOLS,drawing_template
    from power_machine.elements import gen_q_limits_mvar
    assert TOOLS['external']==('gens','ElmXnet',1)
    template=drawing_template('line')
    assert template['params']['R1'] is None and template['params']['B1'] is None
    three=drawing_template('transformer3')
    assert all(value is None for value in three['type_params'].values())
    controller=get_editor_controller(app)
    gen=next(g for g in app.model['gens'] if g['class']=='ElmSym')
    editor=ElementEditor(app,controller,gen['id'])
    editor.variables['q_min_mvar'].set('-20')
    editor.variables['q_max_mvar'].set('30')
    assert editor.apply(),editor.message.get()
    actual=controller.session.get_element(gen['id'])
    assert gen_q_limits_mvar(actual)==pytest.approx((-20,30))
    editor.destroy()


def test_move_and_undo_restores_original_diagram_position(app):
    view=app.diagrams[None]
    enable(view)
    symbol=view.scene.symbols['B01']
    before=(symbol.x,symbol.y)
    view.select_ids(['B01'])
    view._drag=('B01',100,100,False)
    view._motion(event(120,120))
    view._release(event(120,120))
    assert (view.scene.symbols['B01'].x,view.scene.symbols['B01'].y)!=before
    view._edit_history(False)
    assert (view.scene.symbols['B01'].x,view.scene.symbols['B01'].y)==before
