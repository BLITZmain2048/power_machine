"""Polish checks against the shared document and actual Tk layout."""
from test_gui_shell_windows import shell
from power_machine.gui.i18n import set_language,tr
from power_machine.gui.events_editor import input_number,screen_number
from power_machine.gui.diagram.data_cards import element_data

def test_event_numbers_cards_and_inputs(shell):
    app=shell
    set_language('es')
    e=app.analysis.show_events()
    e.time.set('1,025');e.params['r_ohm'].set('0,1');e.params['x_ohm'].set('0.2')
    e.target.set(e.target_labels['B16'])
    assert e.store(False)
    assert e.rows[-1]['time_s']==1.025
    assert '0,100' in e.tree.item(str(len(e.rows)-1),'values')[-1]
    e.tree.selection_set(str(len(e.rows)-1));e.load_selection()
    assert e.time.get()=='1,025'
    set_language('en')
    assert e.time.get()=='1.025'
    assert input_number('0,25')==input_number('0.25')
    assert input_number(screen_number(.00025))==.00025
    for card in app.analysis.event_cards.widgets:
        assert all(child.winfo_class()=='TLabel' for child in card.winfo_children())

def test_selection_empty_inspector_and_toolbar(shell):
    app=shell;red=app.windows.window.workspace
    app.select(None)
    assert app.properties_dock.code.cget('text')=='—'
    app.select('LD16')
    for panel in (red.equipment_host,red.single_line,red.equipment_host):
        red.documents.select(panel);app.update()
        assert app.selected_id=='LD16'
        assert app.properties_dock.current_id=='LD16'
        assert app.diagrams[None].selected_ids=={'LD16'}
    view=app.diagrams[None]
    assert not view._edit_bar.winfo_manager()
    assert len(view._toolbar.winfo_children())>5

def test_nominal_voltage_derived_only_with_cursor(shell):
    app=shell;view=app.diagrams[None]
    view.cursor_t=1.05;view.cursor_buses={'B16':{'u_pu':.5}}
    data=element_data(view,'B16')
    nominal=app.index['B16']['un_kv']
    assert any(row.value.endswith('kV') and '—' not in row.value for row in data.rows)
    assert data.rows[2].value.startswith('—')
    assert view.cursor_buses['B16']=={'u_pu':.5}
    view.cursor_buses={'B16':{}}
    assert element_data(view,'B16').rows[1].value.startswith('—')

def test_configuration_hint_has_separate_row(shell):
    app=shell;app.geometry('1264x680');app.analysis.select_study('rms',open_panel=False)
    app.update()
    form=app.analysis.basic_options
    assert form.canvas.grid_info()['row']==0
    if form.hint.winfo_ismapped():
        assert form.hint.winfo_rooty()>=form.canvas.winfo_rooty()+form.canvas.winfo_height()


def test_modified_case_hover_explains_asterisk(shell,monkeypatch):
    from types import SimpleNamespace
    app=shell;set_language('es')
    app.document.edit(lambda study:setattr(study.options,'t_end_s',2.25))
    app.update_document()
    node='case:'+app.document.active_case_id
    tree=app.data_manager.tree;tree.see(node);app.update()
    assert tree.item(node,'text').rstrip().endswith('*')
    painter=app.data_manager.explorer_painter
    found=[]
    def record(text,x,y):
        painter.tip_job=None
        found.append(text)
    monkeypatch.setattr(painter,'show_tip',record)
    painter.hovered=None
    box=tree.bbox(node)
    painter.hover(SimpleNamespace(y=box[1]+box[3]//2,x_root=100,y_root=100))
    app.after(800,app.quit);app.mainloop()
    assert found and tr('v3.polish.unsaved') in found[0]
