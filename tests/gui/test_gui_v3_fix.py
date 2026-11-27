"""Astra regressions for provenance, transport, study identity and keyboard edits."""
from types import SimpleNamespace
import pytest
from test_gui_shell_windows import shell
from power_machine.gui.plots import SignalBuffer,TIME_KEY
from power_machine.gui.time_cursor import TemporalResult
from power_machine.gui.theme import theme
from power_machine.gui.i18n import tr

@pytest.mark.parametrize('start,end',[(5.,10.),(-1.,2.)])
def test_cursor_real_interval_and_invalid_input(shell,start,end):
    cursor=shell.analysis.time_cursor
    samples=SignalBuffer();samples.reset([TIME_KEY,'B01|m:u|p.u.'])
    samples.append([[start,1.],[end,.9]])
    cursor.set_data(TemporalResult(samples,shell.model))
    assert cursor.t==start
    assert float(cursor.scale.cget('from'))==start
    cursor.seek(end);cursor.stop();assert cursor.t==start
    for invalid in ('nan','inf','oops',str(end+1)):
        cursor.time_text.set(invalid);cursor._entered()
        assert cursor.t==start and cursor.entry.instate(['invalid'])
        assert cursor.note.cget('text')==tr('v3.fix.cursor_range',start=__import__('power_machine.gui.time_cursor',fromlist=['number']).number(start),end=__import__('power_machine.gui.time_cursor',fromlist=['number']).number(end))
    cursor.time_text.set(str((start+end)/2).replace('.',','));cursor._entered()
    assert cursor.t==(start+end)/2 and not cursor.entry.instate(['invalid'])


def test_temporal_layer_never_uses_lf_and_honors_energized(shell):
    view=shell.diagrams[None]
    view.set_loadflow_results(dict(buses=[dict(id='B01',u_pu=1.,energized=True),dict(id='B02',u_pu=1.,energized=True)]))
    view._time_cursor(t=1,bus_state={'B01':dict(u_pu=.9,energized=False)},branch_state={})
    assert view.element_color(view.index['B01'],dict(u_pu=1.),345)==theme.color('deenergized')
    assert view.element_color(view.index['B02'],dict(u_pu=1.),345)==theme.color('text_muted')
    for handle in view.canvas.find_withtag('result'):assert view.canvas.itemcget(handle,'state')=='hidden'
    view.draw()
    assert not view.canvas.find_withtag('flow_arrow')
    assert not view.canvas.find_withtag('result')
    view._time_cursor(t=None)
    assert view.cursor_t is None


def test_start_tracks_completed_study_not_selected_study(shell):
    page=shell.welcome_page;analysis=shell.analysis
    analysis.select_study('rms',open_panel=False)
    shell.bus.publish('results.updated',study='loadflow',result_id='flow-result')
    page.refresh();assert page.states['results']=='pending'
    analysis.select_study('flow',open_panel=False);page.refresh()
    assert page.states['results']=='ready'
    assert tr('v3.analysis.state.current') in page.buttons['flow'].cget('text')
    shell.bus.publish('worker.cancelled',kind='rms',requested=False)
    analysis.select_study('rms',open_panel=False);page.refresh()
    shell.bus.publish('worker.cancelled',kind='rms',requested=False)
    assert page.states['results']=='cancelled'


def test_keyboard_inspector_and_equipment_use_shared_editor(shell):
    app=shell;app.select('LD16');dock=app.properties_dock
    row,value=dock.rows['p_mw']
    assert str(value.cget('takefocus'))=='1'
    assert value.bind('<F2>') and value.bind('<Return>')
    edit=dock.begin_edit('p_mw');edit.delete(0,'end');edit.insert(0,'333,5')
    dock._finish_inline(True);assert dock.apply_changes()
    assert app.index['LD16']['p_mw']==333.5
    red=app.windows.window.workspace;red.documents.select(red.equipment_host)
    table=red.equipment;table.set_kind('ElmLod');table.tree.selection_set('LD16')
    table._active_column='#'+str(table.columns.index('q_mvar')+1)
    app.update();table._keyboard_edit()
    assert table._inline is not None
    assert table.tree.column('scale','stretch')==0
    assert app.diagrams[None].canvas.bind('<Shift-F10>')


def test_expanded_curves_restore_sidebar_and_error_recovery(shell,monkeypatch):
    app=shell;analysis=app.analysis
    analysis.toggle_curve_area();assert str(analysis.sidebar) not in analysis.panes.panes()
    analysis.toggle_curve_area();assert str(analysis.sidebar) in analysis.panes.panes()
    messages=[];monkeypatch.setattr(app.output,'log',lambda kind,text:messages.append((kind,text)))
    app.show_error(ValueError('Invalid target'))
    assert analysis.study_states[analysis.active_study]=='error'
    assert 'Invalid target' in messages[-1][1] and app.case_label.get() in messages[-1][1]
    assert analysis.results.select()==str(analysis.result_hosts['log'])


def test_cursor_cache_skips_hidden_pages_and_catches_up(shell):
    from power_machine.gui.plots import PlotPage
    app=shell;cursor=app.analysis.time_cursor
    samples=SignalBuffer();samples.reset([TIME_KEY,'B01|m:u|p.u.']);samples.append([[5,1],[10,.9]])
    pages=[]
    for i in range(3):
        page=PlotPage(app.book,dict(name='Cursor '+str(i),axes=1,curves=[dict(key='B01|m:u|p.u.',axis=0)]),samples)
        app.book.add(page,text='Cursor '+str(i));pages.append(page)
    app.deiconify();app.book.select(pages[0]);app.update();cursor.seek(7)
    assert pages[0]._time_value==7
    app.book.select(pages[2]);app.update()
    assert pages[2]._time_value==7
    view=app.diagrams[None];view.pin_data('B01');view._time_cursor(t=7,bus_state={'B01':{'u_pu':.95}})
    handles=view.canvas.find_withtag('pin:B01')
    view._time_cursor(t=8,bus_state={'B01':{'u_pu':.96}})
    assert handles==view.canvas.find_withtag('pin:B01')


def test_clear_selection_survives_queued_tree_events(shell):
    app=shell;app.select('LD16');app.update()
    app.select(None);app.update()
    assert app.selected_id is None
    assert app.properties_dock.current_id is None
    assert not app.data_manager.tree.selection()


def test_voltage_violation_is_readable_without_color(shell):
    from power_machine.gui.diagram.data_cards import element_data
    view=shell.diagrams[None]
    view._time_cursor(t=1,bus_state={'B01':dict(u_pu=1.0501)})
    data=element_data(view,'B01')
    assert any('>' in row.value and row.label==tr('v3.fix.voltage_limit') for row in data.rows)


def test_start_single_column_has_no_unused_weight(shell):
    page=shell.welcome_page
    page.resize(SimpleNamespace(width=600,height=400))
    assert page.body.grid_columnconfigure(1,'weight')==0
    assert not page.body.grid_columnconfigure(1,'uniform')
    page.resize(SimpleNamespace(width=1000,height=600))
    assert page.body.grid_columnconfigure(1,'weight')==1


def test_equipment_keyboard_column_navigation(shell):
    table=shell.windows.window.workspace.equipment
    table.set_kind('ElmLod')
    table._active_column='#'+str(table.columns.index('p_mw')+1)
    table._cycle_column(1)
    assert table.columns[int(table._active_column[1:])-1]=='q_mvar'
    assert 'F2' in table.message.get()


def test_lf_arrows_restore_after_temporal_redraw(shell):
    view=shell.diagrams[None]
    item=shell.model['branches'][0]
    result=dict(buses=[],branches=[dict(id=item['id'],bus_from=item['bus_from'],bus_to=item['bus_to'],p_from_mw=10.,p_to_mw=-9.,energized=True)])
    view.set_loadflow_results(result)
    assert view.canvas.find_withtag('flow_arrow')
    view._time_cursor(t=1,bus_state={'B01':{'u_pu':.9}})
    view.draw()
    assert not view.canvas.find_withtag('flow_arrow')
    view._time_cursor(t=None)
    assert view.canvas.find_withtag('flow_arrow')
