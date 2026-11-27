"""Canvas gestures, element-attached values and bounded result UI with real Tk."""
from types import SimpleNamespace

import pytest

from power_machine.gui.bus import EventBus
from power_machine.gui.diagram import DiagramWidget
from power_machine.gui.diagram.data_cards import element_data
from power_machine.gui.i18n import language, set_language, tr
from power_machine.gui.settings import Settings
from power_machine.gui.theme import theme


def event(x,y):
    return SimpleNamespace(x=x,y=y,state=0,x_root=x+30,y_root=y+30)


@pytest.fixture
def diagram(tk_root,tmp_path):
    model=dict(meta={},buses=[dict(id=k,name=k,un_kv=110,scope='test',
        relative_path='Test.ElmNet',**{'class':'ElmTerm'}) for k in ('B1','B2','B3')],
        branches=[dict(id='L1',name='Line 1',bus_from='B1',bus_to='B2',**{'class':'ElmLne'})],
        loads=[dict(id='LD1',name='Load 1',bus='B2',**{'class':'ElmLod'})],
        gens=[dict(id='G1',name='Generator 1',bus='B1',**{'class':'ElmSym'})])
    app=SimpleNamespace(model=model,layout_path=tmp_path/'layout.json',settings=Settings(tmp_path/'settings.json'))
    bus=EventBus(tk_root)
    tk_root.geometry('1280x720')
    view=DiagramWidget(tk_root,app,bus)
    view.pack(fill='both',expand=True)
    tk_root.update()
    view.set_loadflow_results(dict(buses=[dict(id='B1',u_pu=1.012,u_kv=111.32,angle_deg=-2.45)],
        loads=[dict(id='LD1',p_mw=97.6,q_mvar=35.2)],
        gens=[dict(id='G1',p_mw=101.5,q_mvar=-6.1)],
        branches=[dict(id='L1',bus_from='B1',bus_to='B2',p_from_mw=12.3,q_from_mvar=4.5,
            i_from_ka=.071,p_to_mw=-12.1,q_to_mvar=-4.2,i_to_ka=.069)]))
    view._user_zoom=True
    previous=language()
    yield view,bus
    set_language(previous)
    view.destroy()
    bus.close()


def wait(root,ms):
    root.after(ms,root.quit)
    root.mainloop()


def texts(card):
    return ' '.join(label.cget('text') for pair in card.labels for label in pair)


def test_result_labels_default_off_and_settings_opt_in(diagram):
    view,_=diagram
    assert not view.show_results and not view._result_var.get()
    assert not view.canvas.find_withtag('result')
    assert view.scene.results is view.results
    view.set_result_boxes_visible(True)
    assert view.show_results
    assert Settings(view._settings.path).get('diagram_result_labels') is True
    view.set_result_boxes_visible(False)
    assert not view.canvas.find_withtag('result')


def test_pan_and_release_never_redraw_and_pin_tracks_element(diagram,monkeypatch):
    view,_=diagram
    view.toggle_minimap()
    minimap_items=len(view.canvas.find_withtag('minimap'))
    view.pin_data('B1')
    handle=view.canvas.find_withtag('pin:B1')[1]
    before=view.canvas.coords(handle)
    monkeypatch.setattr(view,'draw',lambda:pytest.fail('full redraw during pan'))
    monkeypatch.setattr(view,'_rebuild_scene',lambda:pytest.fail('scene rebuild during pan'))
    view._pan_press(event(100,100))
    view._motion(event(132,121))
    view._release(event(132,121))
    after=view.canvas.coords(handle)
    assert after==pytest.approx([v+(32 if i%2==0 else 21) for i,v in enumerate(before)])
    assert view._paint_job is None
    assert len(view.canvas.find_withtag('minimap'))==minimap_items
    close=view.canvas.find_withtag('pin_close:B1')[0]
    x,y=view.canvas.coords(close)
    view._press(event(x,y))
    assert not view.pinned_data


def test_zoom_scales_immediately_and_debounces_one_full_draw(diagram,monkeypatch):
    view,_=diagram
    calls=[]
    original=view.draw
    monkeypatch.setattr(view,'draw',lambda:(calls.append('draw'),original()))
    item=next(i for i in view.canvas.find_withtag('element:B1') if 'symbol' in view.canvas.gettags(i))
    before=view.canvas.coords(item)
    view._zoom(300,200,1.15)
    first_job=view._paint_job
    assert not calls
    assert view.canvas.coords(item)!=before
    for _ in range(5):view._zoom(300,200,1.02)
    assert first_job!=view._paint_job
    pending=view.tk.call('after','info')
    assert first_job not in pending
    wait(view.winfo_toplevel(),240)
    assert calls==['draw']
    assert view._paint_job is None


def test_hover_waits_300ms_and_reuses_one_localized_card(diagram):
    view,_=diagram
    set_language('es')
    symbol=view.scene.symbols['B1']
    x,y=view._xy(symbol.x,symbol.y)
    view._hover(event(x,y))
    assert view._tooltip is None
    wait(view.winfo_toplevel(),340)
    assert view._tooltip_key=='B1'
    card=view._tooltip
    assert '1,0120 p.u.' in texts(card)
    assert '111,32 kV' in texts(card) and '-2,45 °' in texts(card)
    assert tr('v3.fix.last_lf') in texts(card)
    view._hide_tooltip()
    view.show_element_tooltip('LD1',200,120)
    assert view._tooltip is card
    assert '97,60 MW' in texts(card)
    view._zoom(200,120,1.1)
    assert not card.winfo_manager()


def test_branch_both_ends_and_generator_solved_terminal_voltage(diagram):
    view,_=diagram
    set_language('en')
    data=element_data(view,'L1')
    text=' '.join(row.label+' '+row.value for row in data.rows)
    assert 'From B1' in text and 'To B2' in text
    assert '12.30 MW' in text and '-12.10 MW' in text
    assert '4.50 Mvar' in text and '-4.20 Mvar' in text
    assert '0.071 kA' in text and '0.069 kA' in text
    assert any(row.value=='1.0120 p.u.' for row in element_data(view,'G1').rows)


def test_time_cursor_refreshes_visible_tooltip_and_pin_without_steady_values(diagram):
    view,bus=diagram
    set_language('es')
    view.pin_data('B1')
    view.show_element_tooltip('B1',100,100)
    bus.publish('time.cursor',t=.32,bus_state={'B1':dict(u_pu=.82)},branch_state={'L1':112.3})
    text=texts(view._tooltip)
    assert 't = 0,320 s' in text and '0,8200 p.u.' in text
    assert '111,32' not in text
    data=element_data(view,'B1')
    assert data.rows[0].token=='v_low'
    view.winfo_toplevel().update_idletasks()
    assert view._tooltip.labels[3][1].cget('foreground')==theme.color('v_low')
    assert any('0,820' in view.canvas.itemcget(i,'text') for i in view.canvas.find_withtag('pin:B1') if view.canvas.type(i)=='text')
    branch=element_data(view,'L1')
    assert any(row.value=='112,3 %' and row.token=='load_112' for row in branch.rows)
    assert not any(row.value=='12,30 MW' for row in branch.rows)
    assert all('—' in row.value for row in element_data(view,'G1').rows)
    bus.publish('time.cursor',t=None)
    assert '1,0120 p.u.' in texts(view._tooltip)
    assert tr('v3.fix.last_lf') in texts(view._tooltip)


def test_drag_updates_only_incident_traces_and_keeps_other_handles(diagram,monkeypatch):
    view,_=diagram
    unrelated=view.canvas.find_withtag('element:B3')
    before={i:view.canvas.coords(i) for i in unrelated}
    wire=next(w for w in view.scene.wires if w.start=='B1' or w.end=='B1')
    initial=list(wire.points)
    scene=view.scene
    monkeypatch.setattr(view,'draw',lambda:pytest.fail('full redraw during element move'))
    monkeypatch.setattr(view,'_rebuild_scene',lambda:pytest.fail('scene rebuild during element move'))
    view.select_ids(['B1'])
    view._drag=('B1',100,100,False)
    view._motion(event(110,110))
    count=len(wire.points)
    view._motion(event(120,120))
    assert len(wire.points)==count and wire.points!=initial
    view._release(event(120,120))
    assert view.scene is scene
    assert all(view.canvas.coords(i)==coords for i,coords in before.items())


def test_context_card_preserves_actions_and_properties_selects_inspector(diagram):
    view,bus=diagram
    selected=[]
    bus.subscribe('selection.changed',lambda **payload:selected.append(payload))
    s=view.scene.symbols['B1']
    x,y=view._xy(s.x,s.y)
    view._context(event(x,y))
    labels=[b.cget('text') for b in view._menu.buttons]
    assert tr('v3.diagram.pin') in labels
    assert tr('v3.diagram.properties') in labels
    assert tr('diagram.fault') in labels
    assert tr('edit.delete') in labels
    view._menu.destroy()
    view.view_properties('LD1')
    assert selected[-1]['ids']==['LD1']


def test_label_culling_and_lod_tags_without_recreation(diagram):
    view,_=diagram
    view.set_result_boxes_visible(True)
    view.offset=(-10000.,-10000.)
    view.draw()
    assert not view.canvas.find_withtag('result')
    view.offset=(30.,30.)
    view.fit()
    view._dense=True
    view._density_scene=view.scene
    names=view.canvas.find_withtag('label_secondary')
    view._zoom(300,200,.03)
    assert view.canvas.find_withtag('label_secondary')==names
    assert all(view.canvas.itemcget(i,'state')=='hidden' for i in names)


def test_pinned_card_limit_is_bounded(diagram):
    view,_=diagram
    for i in range(21):
        key='extra'+str(i)
        view.index[key]=dict(id=key,name=key)
        view.pin_data(key)
    assert len(view.pinned_data)==20
    assert '20' in view.status.get()


def test_localized_voltage_legend_and_full_color_combo(diagram):
    from tkinter import font as tkfont
    view,_=diagram
    set_language('es')
    view.retranslate()
    view.set_color_mode('voltage_band')
    assert '0,95–1,05' in view._legend_var.get()
    assert '0.95' not in view._legend_var.get()
    font=tkfont.Font(view,font=theme.font('body'))
    capacity=int(view._choices.cget('width'))*font.measure('0')
    assert capacity>=max(font.measure(label) for label in view._choices['values'])
    set_language('en')
    view.retranslate()
    assert '0.95–1.05' in view._legend_var.get()


def test_time_badge_chooses_free_corner_and_stays_fixed_during_gestures(diagram):
    view,bus=diagram
    # Put the old problematic top-left bar directly under the former badge.
    c=view.canvas
    c.create_line(8,20,180,20,width=6,tags=('symbol','obstacle'))
    bus.publish('time.cursor',t=1.05,bus_state={'B1':dict(u_pu=.9)})
    assert view._time_corner!='nw'
    handles=c.find_withtag('time_cursor')
    rectangle=next(i for i in handles if c.type(i)=='rectangle')
    assert c.itemcget(rectangle,'stipple')==''  # Opaque source badge keeps underlying symbols out of its text.
    before={i:c.coords(i) for i in handles}
    view._pan_press(event(50,50))
    view._motion(event(70,80))
    view._release(event(70,80))
    assert all(c.coords(i)==coords for i,coords in before.items())
    view._zoom(300,200,1.15)
    assert all(c.coords(i)==pytest.approx(coords) for i,coords in before.items())


def test_stale_result_data_never_survives_in_cards(diagram):
    view,bus=diagram
    view.pin_data('B1')
    view.show_element_tooltip('B1',100,100)
    bus.publish('results.stale',reason='test')
    assert view.scene.results=={}
    assert all('—' in row.value for row in element_data(view,'B1').rows)
