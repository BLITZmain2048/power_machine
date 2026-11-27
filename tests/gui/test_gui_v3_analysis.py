"""Temporal transport and analysis composition use acquired samples only."""
from types import SimpleNamespace
import numpy as np
import pytest
from test_gui_shell_windows import shell
from power_machine.gui.plots import SignalBuffer, TIME_KEY
from power_machine.gui.time_cursor import TemporalResult
from power_machine.gui.i18n import tr,set_language


def samples():
    data=SignalBuffer()
    data.reset([TIME_KEY,'bus/path|m:u|p.u.','bus/path|energized|bool','line/path|c:loading|%'])
    data.append([[0,1,1,40],[1,.9,1,60],[1,.4,0,80],[2,.8,1,100],[3,None,1,None]])
    model={'buses':[{'id':'B1','path':'bus/path'}],'branches':[{'id':'L1','path':'line/path'}]}
    return data,model


def test_real_sample_interpolation_discontinuities_and_missing_coverage():
    buffer,model=samples()
    result=TemporalResult(buffer,model)
    assert result.sample(buffer.keys[1],.5)==pytest.approx(.95)
    assert result.sample(buffer.keys[1],1)==.4
    buses,branches=result.state(1.5)
    assert buses['B1']=={'u_pu':pytest.approx(.6),'energized':False}
    assert branches=={'L1':90}
    assert result.sample(buffer.keys[1],2.5) is None
    assert result.state(2.5)[0]=={'B1':{'energized':True}}
    assert result.state(-1)==({}, {})
    assert TemporalResult(buffer).state(.5)==({}, {})
    buffer.keys[0]='time_s'
    assert TemporalResult(buffer,model).state(.5)[0]['B1']['u_pu']==pytest.approx(.95)


def test_disabled_transport_and_invalidation_publish_no_time(shell,monkeypatch):
    app=shell
    cursor=app.analysis.time_cursor
    assert cursor.data is None
    assert cursor.play_button.instate(['disabled'])
    assert cursor.note.cget('text')==tr('v3.analysis.no_temporal')
    assert not cursor.seek(1)
    emitted=[]
    token=app.bus.subscribe('time.cursor',lambda **event:emitted.append(event))
    data,model=samples()
    cursor.set_data(TemporalResult(data,model))
    cursor.seek(1.5)
    assert emitted[-1]['source']=='analysis'
    assert emitted[-1]['bus_state']['B1']['u_pu']==pytest.approx(.6)
    cursor.seek(0)
    cursor.speed.set('2×')
    cursor.playing=True
    cursor._clock=0
    with monkeypatch.context() as clock:
        clock.setattr('power_machine.gui.time_cursor.time.perf_counter',lambda:.1)
        cursor._tick()
    assert cursor.t==pytest.approx(.2)
    cursor.stop()
    assert emitted[-1]['t']==0
    app.bus.publish('results.stale',reason='synthetic revision')
    assert emitted[-1]==dict(t=None,bus_state={},branch_state={},source='analysis')
    assert cursor.play_button.instate(['disabled'])
    cursor.set_data(TemporalResult(data,model))
    original=app.document
    app.document=SimpleNamespace(active_case_id='another-case')
    cursor.check_case()
    app.document=original
    assert emitted[-1]['t'] is None
    assert cursor.data is None
    app.bus.unsubscribe(token)


def test_event_cards_seek_and_open_same_editor_row(shell):
    app=shell
    app.document.edit(lambda study:setattr(study,'events',[
        app.api.SimEvent(.5,'short_circuit','B16',{'r_ohm':0,'x_ohm':0}),
        app.api.SimEvent(1,'clear_short_circuit','B16',{})]))
    app.analysis.refresh_document()
    editor=app.analysis.show_events()
    data,model=samples()
    app.analysis.time_cursor.set_data(TemporalResult(data,model))
    app.analysis.seek_event(1)
    assert app.analysis.results.select()==str(app.analysis.result_hosts['curves'])
    assert app.analysis.time_cursor.t==1
    app.analysis.edit_event(1)
    assert app.analysis.results.select()==str(app.analysis.result_hosts['events'])
    app.update_idletasks()
    cards=app.analysis.event_cards
    last=cards.widgets[-1]
    assert last.winfo_rooty()+last.winfo_height()<=cards.canvas.winfo_rooty()+cards.canvas.winfo_height()
    from tkinter import ttk
    from power_machine.gui.theme import theme
    assert ttk.Style(app).lookup('Panel.EventTime.TLabel','foreground')==theme.color('accent2')
    assert editor._editing_index==1
    from power_machine.gui.events_editor import input_number
    assert input_number(editor.time.get())==1
    assert len(app.analysis.event_cards.widgets)==2
    app.analysis.event_cards._schedule_seek(0)
    app.analysis.event_cards._double_click(0)
    assert app.analysis.event_cards._pending is None
    assert editor._editing_index==0
    assert app.analysis.results.select()==str(app.analysis.result_hosts['events'])


def test_study_tree_states_and_real_rms_configuration(shell):
    app=shell
    area=app.analysis
    groups=area.study_tree.get_children()
    assert groups==('group:stationary','group:dynamic','group:tools')
    assert sum(len(area.study_tree.get_children(group)) for group in groups)==12
    app.maxsize(5000,4000)
    app.deiconify()
    app.geometry('1904x1040')
    app.update()
    app.update_idletasks()
    area._resize_studies()
    # Windows can clamp a requested size to the current remote-desktop work area.
    assert int(area.study_tree.cget('height')) == (18 if app.winfo_height()>=900 else 9)
    if app.winfo_height()<900:area.study_tree.see('calc_lfctl')
    app.update_idletasks()
    assert area.study_tree.bbox('calc_lfctl')
    assert all(area.study_tree.item(group,'open') for group in groups)
    app.bus.publish('results.updated',study='loadflow',result_id='loadflow')
    assert area.study_states['flow']=='current'
    app.bus.publish('results.stale',reason='changed')
    assert area.study_states['flow']=='stale'
    assert area.study_states['rms']=='never'
    app.parity_results['contingency']={'status':'error'}
    app.bus.publish('results.updated',study='contingency')
    assert area.study_states['contingency']=='error'
    app.document.edit(lambda study:(setattr(study.options,'t_start_s',0),setattr(study.options,'t_end_s',2.5),setattr(study.options,'dt_s',.002)))
    area.select_study('rms',open_panel=False)
    for code,duration in [('es','2,50'),('en','2.50')]:
        set_language(code)
        area.refresh_configuration()
        assert duration+' s' in app.configuration_text.get()
        assert '2 ms' in app.configuration_text.get()
        assert area.study_tree.item('group:dynamic','text')==tr('v3.analysis.group.dynamic')


def test_plot_cursor_updates_artists_without_full_redraw(shell,monkeypatch,tmp_path):
    from power_machine.gui.app_plots import EngineeringPlotPage
    app=shell
    data,model=samples()
    key=data.keys[1]
    spec=dict(name_key='v3.analysis.fault_response',axes=1,curves=[dict(key=key,label='B1')],show_pf=False)
    page=EngineeringPlotPage(app.book,spec,data)
    app.book.add(page,text='RMS',area='curves')
    app.book.select(page)
    page.set_events([dict(time_s=.5,kind='short_circuit',target_id='B1'),dict(time_s=1,kind='clear_short_circuit',target_id='B1')])
    app.update()
    page.canvas.draw()
    assert len(page.axes[0].patches)==1
    draws=[]
    monkeypatch.setattr(page.canvas,'draw',lambda *a,**k:draws.append(1))
    for t in np.linspace(0,2,20):page.set_time_cursor(float(t))
    assert not draws
    dot,text,*_=page._time_markers[0]
    assert dot.get_ydata()[0]==pytest.approx(.8)
    assert page._time_verticals[0].get_xdata()==[2,2]
    assert '0.8000' in text.get_text() or '0,8000' in text.get_text()
    page.set_time_cursor(None)
    assert not dot.get_visible()
    assert not page._time_verticals[0].get_visible()
    page.set_time_cursor(1.5)
    path=tmp_path/'cursor.svg'
    page.save_figure(path,format='svg')
    exported=path.read_text(encoding='utf-8')
    assert '0.6000' in exported or '0,6000' in exported
    assert all(artist.get_animated() for artist in page._time_artists)
    page.toolbar.pan()
    assert page.toolbar._buttons['Pan'].var.get()==1
    page.toolbar.zoom()
    assert page.toolbar._buttons['Pan'].var.get()==0
    assert page.toolbar._buttons['Zoom'].var.get()==1
    page.toolbar.zoom()
    assert not page.toolbar.mode
    previous=page.axes[0].get_xlim()
    page.toolbar.push_current()
    page.axes[0].set_xlim(0,1)
    page.toolbar.push_current()
    assert page.toolbar._buttons['Back'].instate(['!disabled'])
    page.toolbar.back()
    assert page.axes[0].get_xlim()==pytest.approx(previous)
