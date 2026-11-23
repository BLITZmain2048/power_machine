from test_gui_edit_diagram import app
from power_machine.gui.load_tools import open_load_tools
from power_machine.gui.netedit.integration import get_editor_controller
from power_machine.gui.netedit.tools import drawing_template
from power_machine.gui.element_dialog import ElementEditor,element_title
from power_machine.gui.i18n import tr


def test_scaling_scope_and_joint_q(app):
    win=open_load_tools(app)
    try:
        assert win.scope_combo.get()==tr('tools.scope.all')
        assert win.value_combo.get()==tr('tools.scope.all')
        assert str(win.q_entry['state'])=='disabled'
        win.p_factor.set('1.25')
        assert win.q_factor.get()=='1.25'
        win.together.set(False)
        assert str(win.q_entry['state'])=='normal'
    finally:win.destroy()


def test_external_draft_can_complete_in_shared_editor(app):
    c=get_editor_controller(app);data=drawing_template('external')
    data.update(name='Synthetic grid',bus='B01')
    data['params'].update(usetp=1,phiini=0,snss=1000,snssmin=500,rntxn=.1,rntxnmin=.2)
    ident=c.create('gens',data,draft=True)
    editor=ElementEditor(app,c,ident)
    try:
        assert 'params.bustp' in editor.variables
        assert editor.apply(),editor.message.get()
        assert c.session.get_element(ident)['class']=='ElmXnet'
        assert 'Synthetic grid' in element_title(editor.item,c.model)
        c.undo()
        assert c.session.get_element(ident) is None
    finally:editor.destroy()
