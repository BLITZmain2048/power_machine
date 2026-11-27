"""Network v3 interaction regressions against the real shared document."""
import csv
from pathlib import Path
import pytest
from test_gui_shell_windows import shell
from power_machine.gui.i18n import set_language,tr
from power_machine.gui.theme import theme
from power_machine.gui.results_tables import ResultsTable,display_value,format_value
from power_machine.gui.properties_dock import editor_controller


def test_explorer_sections_subtitles_and_preserved_ids(shell):
    app=shell
    set_language('es')
    manager=app.data_manager
    tree=manager.tree
    assert [tree.item(key,'text') for key in tree.get_children()]==[
        tr('v3.red.section.network'),tr('v3.red.section.cases'),tr('v3.red.section.results'),tr('v3.red.section.library')]
    load=app.model['loads'][0]
    assert tree.exists(load['id'])
    assert load['bus'] in tree.item(load['id'],'values')[0]
    assert 'MW' in tree.item(load['id'],'values')[0]
    assert ',' in tree.item(load['id'],'values')[0]
    assert not manager.heading.winfo_manager()
    assert manager.explorer_painter.elide('Interruptores / acopladores',60).endswith('…')
    app.select(load['id'])
    app.update()
    assert tree.selection()==(load['id'],)
    assert manager.explorer_painter.full_text[load['id']].splitlines()[1]


def test_inspector_inline_validation_apply_discard_and_results(shell):
    app=shell
    set_language('es')
    load=app.model['loads'][0]
    app.select(load['id'])
    dock=app.properties_dock
    assert dock.editor.item['id']==load['id']
    assert not dock.editor.winfo_manager()
    assert not dock.footer.winfo_manager()
    app.result={'loads':[{'id':load['id'],'p_mw':12.25,'q_mvar':3.0,'loading_pct':71.2}]}
    app.bus.publish('results.updated',study='loadflow')
    assert dock.result_values['p_mw']==12.25
    entry=dock.begin_edit('p_mw')
    entry.delete(0,'end')
    entry.insert(0,'24,75')
    entry.focus_force()
    app.update()
    entry.event_generate('<Return>')
    app.update()
    assert dock.dirty
    assert dock.footer.winfo_manager()=='pack'
    assert '•' in dock.rows['p_mw'][1].cget('text')
    dock.stage_field('scale','-1')
    assert not dock.apply_changes()
    assert dock.message.get()
    dock.stage_field('scale','1')
    assert dock.apply_changes()
    actual=editor_controller(app).session.get_element(load['id'])
    assert actual['params']['plini']==24.75
    assert not dock.dirty
    assert app.result is None
    dock.stage_field('p_mw','88,5')
    set_language('en')
    assert dock.heading.cget('text')==tr('v3.red.kind.ElmLod')
    assert dock.editor.variables['p_mw'].get()=='88.5'
    dock.discard_changes()
    assert float(dock.editor.variables['p_mw'].get())==24.75
    assert not dock.footer.winfo_manager()


def test_equipment_filter_sort_inline_edit_undo_and_export(shell,tmp_path):
    app=shell
    set_language('es')
    red=app.windows.window.workspace
    red.documents.select(red.equipment_host)
    app.update()
    assert red.mode.get()==tr('v3.equipment')
    table=red.equipment
    table.set_kind('ElmLod')
    assert table.tree.heading('p_mw','text')=='P [MW]'
    assert table.tree.heading('q_mvar','text')=='Q [Mvar]'
    assert len(table.rows)==len(app.model['loads'])
    table.sort('p_mw')
    values=[float(row['p_mw']) for row in table.rows]
    assert values==sorted(values)
    item=table.rows[0]
    identity=item['id']
    original=editor_controller(app).session.get_element(identity)['p_mw']
    table.query.set(identity)
    assert table.tree.get_children()==(identity,)
    assert table.commit_cell(identity,'p_mw','100,25')
    assert editor_controller(app).session.get_element(identity)['params']['plini']==100.25
    table.history(False)
    assert editor_controller(app).session.get_element(identity)['p_mw']==original
    table.history(True)
    assert editor_controller(app).session.get_element(identity)['p_mw']==100.25
    output=tmp_path/'equipment.csv'
    assert table.export_csv(output)==output
    records=list(csv.reader(output.open(encoding='utf-8-sig')))
    assert '100.25' in records[1]
    assert table.tree.item(identity,'values')[table.columns.index('p_mw')]=='100,25'
    assert 'nominal' not in table.columns  # P and Q already have editable columns.


def test_selection_shared_by_equipment_results_explorer_and_diagram(shell):
    app=shell
    table=app.windows.window.workspace.equipment
    table.refresh()
    identity=app.model['buses'][2]['id']
    table.tree.selection_set(identity)
    table._select()
    app.update()
    assert app.selected_id==identity
    assert app.data_manager.tree.selection()==(identity,)
    assert app.properties_dock.current_id==identity
    assert app.diagrams[None].selected_ids=={identity}
    result=ResultsTable(app,'buses',[{'id':'B01','u_pu':1.0},{'id':identity,'u_pu':1.02}])
    table.set_kind('ElmLod')
    result.tree.selection_set('B01')
    result._publish_selection()
    app.update()
    assert table.tree.selection()==('B01',)
    assert table.kind is None
    assert app.properties_dock.current_id=='B01'
    result.destroy()


@pytest.mark.parametrize('name',['light','dark'])
def test_time_cursor_explicit_coverage_and_clear(shell,name):
    app=shell
    theme.set(name)
    set_language('es')
    view=app.diagrams[None]
    branch=app.model['branches'][0]['id']
    before={key:view.element_color(app.index[key],{},345) for key in ('B01','B02',branch)}
    original_results=dict(view.results)
    app.bus.publish('time.cursor',t=.320,bus_state={'B01':{'u_pu':.9},'B02':{'u_pu':float('nan')}},
                    branch_state={branch:{'loading_pct':125.0}},source='analysis')
    assert view.element_color(app.index['B01'],{},345)==theme.color('v_low')
    bus_lines=[handle for handle in view.canvas.find_withtag('element:B01') if 'symbol' in view.canvas.gettags(handle) and view.canvas.type(handle)=='line']
    assert bus_lines and all(view.canvas.itemcget(handle,'fill')==theme.color('v_low') for handle in bus_lines)
    assert view.element_color(app.index['B02'],{},345)==theme.color('text_muted')
    assert view.element_color(app.index[branch],{},345)==theme.color('load_120')
    assert view.results==original_results
    texts=[view.canvas.itemcget(item,'text') for item in view.canvas.find_withtag('time_cursor') if view.canvas.type(item)=='text']
    assert len(texts)==1
    assert tr('v3.fix.temporal_layer',value='0,320',count=1,total=len(app.model['buses'])) in texts[0]
    app.bus.publish('time.cursor',t=None,source='analysis')
    assert not view.canvas.find_withtag('time_cursor')
    assert not view.cursor_buses and not view.cursor_branches
    assert view.element_color(app.index['B01'],{},345)==before['B01']


def test_localized_diagram_labels_and_zoom_status(shell):
    from power_machine.gui.diagram.view import result_text
    app=shell
    view=app.diagrams[None]
    set_language('es')
    symbol=view.scene.symbols['B01']
    text=result_text(symbol,{'u_pu':1.0123,'u_kv':349.24,'angle_deg':-2.5},layers=view.layers)
    assert '1,012' in text and '-2,5' in text
    assert display_value(1.2345,'u_pu')=='1,2345'
    assert format_value(1.2345,'u_pu')=='1.2345'
    view.fit()
    view._zoom(200,150,1.7)
    assert view.zoom_text.get().strip() in view.status.get()
    view.scale=.2
    view.draw()
    assert not view.result_visible(view.scene.symbols['B02'])
    assert app.windows.window.workspace.statusbar.winfo_manager()=='pack'
    set_language('en')
    text=result_text(symbol,{'u_pu':1.0123},layers=view.layers)
    assert '1.012' in text


def test_opening_inline_field_preserves_source_precision(shell):
    app=shell
    set_language('es')
    identity=app.model['loads'][0]['id']
    controller=editor_controller(app)
    precise=97.612345678901
    controller.edit(identity,{'p_mw':precise})
    app.select(identity)
    dock=app.properties_dock
    assert not dock.dirty
    entry=dock.begin_edit('p_mw')
    assert entry.get()==str(precise).replace('.',',')
    entry.focus_force()
    app.update()
    entry.event_generate('<Return>')
    app.update()
    assert controller.session.get_element(identity)['p_mw']==precise
    assert not dock.dirty
