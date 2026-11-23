"""Interaction contracts for the reusable diagram, with real Tk rendering."""
from types import SimpleNamespace
from pathlib import Path
import json

from power_machine.gui.bus import EventBus
from power_machine.gui.diagram import DiagramWidget
from power_machine.gui.diagram.layout import load_positions
from power_machine.gui.diagram.view import result_text
from power_machine.gui.theme import theme
from power_machine.gui.i18n import set_language, tr
from gui_support import ieee_model


def widget(root, tmp_path):
    app = SimpleNamespace(model=ieee_model(), layout_path=tmp_path/'layout.json')
    bus = EventBus(root)
    view = DiagramWidget(root, app, bus)
    view.pack(fill='both', expand=True)
    root.update()
    return view, bus


def event(x, y, state=0):
    return SimpleNamespace(x=x, y=y, state=state)


def test_selection_zoom_group_move_and_persistence(tk_root, tmp_path):
    view, bus = widget(tk_root, tmp_path)
    selected = []
    bus.subscribe('selection.changed', lambda **p: selected.append(p))
    ids = list(view.scene.symbols)[:2]
    view.select_ids(ids)
    assert selected[-1]['ids'] == sorted(ids)
    bus.publish('selection.changed', ids=[ids[1]], source='table')
    assert view.selected_ids == {ids[1]}
    x,y = 300,200
    before = ((x-view.offset[0])/(view.scale*view.x_factor), (y-view.offset[1])/view.scale)
    view._zoom(x,y,1.3)
    after = ((x-view.offset[0])/(view.scale*view.x_factor), (y-view.offset[1])/view.scale)
    assert all(abs(a-b)<1e-8 for a,b in zip(before,after))
    view.select_ids(ids)
    old = {key:(view.scene.symbols[key].x,view.scene.symbols[key].y) for key in ids}
    view._drag = (ids[0], 100,100,False)
    view._motion(event(120,115))
    view._release(event(120,115))
    saved = load_positions(view.layout_path,view.group)
    for key in ids:
        assert saved[key] != old[key]
        assert saved[key] == (view.scene.symbols[key].x,view.scene.symbols[key].y)
    view.destroy()
    assert not view._subscriptions
    bus.close()


def test_events_layers_contingency_and_live_theme(tk_root, tmp_path):
    view,bus = widget(tk_root,tmp_path)
    requested=[]
    bus.subscribe('study.event.requested',lambda **p:requested.append(p))
    key = next(iter(view.scene.symbols))
    view.request_action(key,'short_circuit')
    view.request_action(key,'disconnect')
    assert [p['kind'] for p in requested] == ['fault','disconnect']
    assert all(p['target_id']==key for p in requested)
    view.set_contingency(key, list(view.scene.symbols)[1:3])
    assert view.canvas.find_withtag('contingency')
    view.toggle_minimap()
    assert view.canvas.find_withtag('minimap')
    try:
        theme.set('dark')
        view._theme_changed()
        assert view.canvas.cget('background') == theme.color('diagram_bg')
        set_language('en')
        view.retranslate()
        assert view._toolbar.winfo_children()[0]._diagram_tooltip.text == tr('diagram.select')
        symbol=view.scene.symbols[key]
        text=result_text(symbol,dict(u_pu=1.01,u_kv=345,angle_deg=12),layers=dict(U=False,angle=True))
        assert '345' not in text and '12.0' in text
    finally:
        theme.set('light')
        set_language('es')
        view.destroy()
        bus.close()


def test_rectangle_double_click_and_pan(tk_root, tmp_path):
    view,bus = widget(tk_root,tmp_path)
    opened=[]
    bus.subscribe('element.open',lambda id:opened.append(id))
    view._rubber = (0,0)
    view._release(event(view.canvas.winfo_width(),view.canvas.winfo_height()))
    assert len(view.selected_ids) > 1
    key=next(k for k,s in view.scene.symbols.items() if s.kind=='bus')
    x,y=view._xy(view.scene.symbols[key].x,view.scene.symbols[key].y)
    view._double(event(x,y))
    assert opened[-1] == key
    before=view.offset
    view._pan_press(event(10,10))
    view._motion(event(40,30))
    view._release(event(40,30))
    assert view.offset == (before[0]+30,before[1]+20)
    view.destroy()
    bus.close()


def test_operating_action_uses_document_and_invalidates_results(tk_root, tmp_path):
    view,bus=widget(tk_root,tmp_path)
    edits=[]
    updates=[]
    view.app.document=SimpleNamespace(operation=lambda item,changes:edits.append((item['id'],changes)))
    view.app.update_document=lambda:updates.append(True)
    key=next(iter(view.index))
    view.results={key:dict(u_pu=1)}
    view.request_action(key,'outserv')
    assert edits==[(key,dict(outserv=True))]
    assert updates==[True]
    assert not view.results
    view.app.available=lambda:False
    view.request_action(key,'outserv')
    assert len(edits)==1
    view.destroy()
    bus.close()


def test_diagram_catalogues_and_theme_literals():
    import ast, re
    root=Path(__file__).parents[2]/'power_machine'/'gui'
    catalogues=[json.loads((root/'locale'/f'{code}.json').read_text(encoding='utf8')) for code in ('en','es')]
    for path in (root/'diagram').glob('*.py'):
        text=path.read_text(encoding='utf8')
        assert not re.search(r'#[0-9a-fA-F]{6}',text), path
        for node in ast.walk(ast.parse(text)):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='tr' and node.args and isinstance(node.args[0],ast.Constant):
                assert all(node.args[0].value in c for c in catalogues), node.args[0].value
