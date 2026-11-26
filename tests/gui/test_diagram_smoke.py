from gui_support import ieee_model, ieee_root
"""Pruebas reales de legibilidad en el zoom de ajuste, study."""

import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import font as tkfont
from types import SimpleNamespace

import pytest

from power_machine.gui.diagram import DiagramCallbacks, DiagramView, diagram_groups
from power_machine.gui.diagram.layout import clean_label
from power_machine.study import load_model, new_study, run_loadflow

from gui_support import ROOT


def collision(a, b):
    return min(a[2], b[2]) > max(a[0], b[0]) and min(a[3], b[3]) > max(a[1], b[1])


@pytest.fixture
def root(tk_root):
    return tk_root


@pytest.fixture(scope='module')
def inputs():
    model = ieee_model()
    result = run_loadflow(ROOT, new_study(ieee_root()), model)
    # La prueba de dibujo acepta el diagnóstico real de islas del motor.
    assert any(bus.get('energized') for bus in result['buses'])
    assert result['max_mismatch_pu'] < 1e-6
    return model, result


def verify_text(view):
    c = view.canvas
    assert not view.unplaced_labels, (view.scene.title, view.unplaced_labels)
    labels = [i for i in c.find_all() if c.type(i) == 'text' and
              ('label' in c.gettags(i) or 'result' in c.gettags(i))]
    symbol_items = list(c.find_withtag('symbol'))
    for n, ident in enumerate(labels):
        box = c.bbox(ident)
        text = c.itemcget(ident, 'text')
        assert box[0] >= 0 and box[1] >= 0 and box[2] <= c.winfo_width() and box[3] <= c.winfo_height(), (view.scene.title, text, box)
        assert tkfont.Font(c, font=c.itemcget(ident, 'font')).actual('size') >= 7
        assert chr(59) not in text
        for other in labels[n + 1:]:
            assert not collision(box, c.bbox(other)), (view.scene.title, text, c.itemcget(other, 'text'))
        element = next(t[8:] for t in c.gettags(ident) if t.startswith('element:'))
        for other in symbol_items:
            # La caja de una subestación contiene su propio nombre.
            if view.group is None and 'element:' + element in c.gettags(other):
                assert all((box[0] >= c.bbox(other)[0], box[1] >= c.bbox(other)[1],
                            box[2] <= c.bbox(other)[2], box[3] <= c.bbox(other)[3]))
                continue
            assert not collision(box, c.bbox(other)), (view.scene.title, text, c.gettags(other))
    for key, symbol in view.scene.symbols.items():
        if not symbol.label:
            continue
        ids = [i for i in c.find_withtag('element:' + key) if 'label' in c.gettags(i)]
        if not ids:
            assert not view.detail_visible(key)
            continue
        assert len(ids) == 1
        assert ''.join(c.itemcget(ids[0], 'text').split()) == ''.join(clean_label(symbol.label).split())
    for wire in view.scene.wires:
        if not wire.label:
            continue
        ids = [i for i in c.find_withtag('element:' + wire.id) if 'bundle_label' in c.gettags(i)]
        assert len(ids) == 1
        assert ''.join(c.itemcget(ids[0], 'text').split()) == ''.join(wire.label.split())
        box = c.bbox(ids[0])
        segments = [(view._xy(*a), view._xy(*b)) for a, b in zip(wire.points, wire.points[1:])]
        # Si el rótulo está apartado de la traza debe conservar una guía visible.
        touches = any(collision(box, (min(a[0], b[0]) - 2, min(a[1], b[1]) - 2,
                                     max(a[0], b[0]) + 2, max(a[1], b[1]) + 2))
                      for a, b in segments)
        guides = [i for i in c.find_withtag('element:' + wire.id) if 'bundle_guide' in c.gettags(i)]
        assert touches or guides, (wire.id, wire.label, 'sin guía a su traza')
        for guide in guides:
            x, y = c.coords(guide)[:2]
            assert any(abs((x - a[0]) * (b[1] - a[1]) - (y - a[1]) * (b[0] - a[0])) < 1e-5
                       and min(a[0], b[0]) - .01 <= x <= max(a[0], b[0]) + .01
                       and min(a[1], b[1]) - .01 <= y <= max(a[1], b[1]) + .01
                       for a, b in segments)
    rectangles = [i for i in c.find_withtag('result') if c.type(i) == 'rectangle']
    for n, ident in enumerate(rectangles):
        for other in rectangles[n + 1:]:
            assert not collision(c.bbox(ident), c.bbox(other)), (view.scene.title, c.gettags(ident), c.gettags(other))
        for other in symbol_items:
            assert not collision(c.bbox(ident), c.bbox(other)), (view.scene.title, c.gettags(ident), c.gettags(other))


@pytest.mark.parametrize('size', ['1500x790', '1200x760'])
def test_all_views_fit_labels_and_real_loadflow(root, inputs, tmp_path, size):
    model, result = inputs
    root.geometry(size + '+0+0')
    callbacks = DiagramCallbacks(lambda *_: None, lambda *_: None, lambda *_: None)
    for group in [None] + [g['id'] for g in diagram_groups(model)]:
        view = DiagramView(root, model, tmp_path / 'layout.json', callbacks, group)
        view.pack(fill='both', expand=True)
        root.update()
        try:
            verify_text(view)
            assert view.zoom_text.get() == f'{view.scale * 100:.0f} %'
            # El resultado llega sin pulsar Ajustar ni cambiar zoom.
            view.set_loadflow_results(result)
            root.update()
            verify_text(view)
            energized = {r['id'] for r in result['buses'] if r.get('energized')}
            for key, symbol in view.scene.symbols.items():
                if symbol.kind == 'bus' and key in energized:
                    boxes = [i for i in view.canvas.find_withtag('element:' + key)
                             if 'result' in view.canvas.gettags(i) and view.canvas.type(i) == 'rectangle']
                    assert len(boxes) == 1, key
                row = view.results.get(key, {})
                if symbol.result_box and (row.get('energized') is False or
                                         row.get('status') in ('sin_energia_pasiva', 'ambos_puertos_abiertos')):
                    texts = [view.canvas.itemcget(i, 'text') for i in view.canvas.find_withtag('element:' + key)
                             if 'result' in view.canvas.gettags(i) and view.canvas.type(i) == 'text']
                    assert texts == ['—'], (key, texts)
            if group is None:
                assert bool(view.canvas.find_withtag('wire')) == bool(view.scene.wires)
            view.set_result_boxes_visible(False)
            assert not view.canvas.find_withtag('result')
            view.set_result_boxes_visible(True)
            assert view.canvas.find_withtag('result') or group is None
        finally:
            view.destroy()
            root.update()


def test_zoom_resize_navigation_hover_drag_and_export(root, inputs, tmp_path, monkeypatch):
    model, result = inputs
    calls = []
    callbacks = DiagramCallbacks(lambda k: calls.append(('select', k)),
                                 lambda k: calls.append(('open', k)),
                                 lambda k, a: calls.append((a, k)))
    from copy import deepcopy
    model = deepcopy(model)
    for number, bus in enumerate(model['buses']):
        bus['relative_path'] = f'Red\\Grupo{number % 2}.ElmSubstat\\{bus["id"]}'
    group = diagram_groups(model)[0]['id']
    root.geometry('1200x760+0+0')
    view = DiagramView(root, model, tmp_path / 'layout.json', callbacks, group)
    view.pack(fill='both', expand=True)
    root.update()
    try:
        initial = view.scale
        root.geometry('1500x790+0+0')
        root.update()
        assert view.scale != initial or view.x_factor != 1
        point = ((350 - view.offset[0]) / (view.scale * view.x_factor), (220 - view.offset[1]) / view.scale)
        view._zoom(350, 220, 1.15)
        assert view._xy(*point) == pytest.approx((350, 220))
        zoom = view.scale
        root.geometry('1300x750+0+0')
        root.update()
        assert view.scale == zoom
        assert view.zoom_text.get() == f'{zoom * 100:.0f} %'
        view.fit()
        view.set_result_boxes_visible(True)
        view.set_loadflow_results(result)
        assert view.canvas.find_withtag('result')
        view._zoom(350, 220, .8)
        assert view.canvas.find_withtag('result')
        outgoing = next(s for s in view.scene.symbols.values() if s.remote_group)
        event = SimpleNamespace(x=view._xy(outgoing.x, outgoing.y)[0], y=view._xy(outgoing.x, outgoing.y)[1])
        view._double(event)
        assert calls[-1] == ('open', outgoing.remote_group)
        view._hover(event)
        assert view._hover_job is not None
        root.after(340,root.quit)
        root.mainloop()
        assert view._tooltip is not None
        assert clean_label(outgoing.item['name']) in view._tooltip.winfo_children()[0].cget('text')
        view._hide_tooltip()
        symbol = next(s for s in view.scene.symbols.values() if s.kind == 'bus')
        x, y = view._xy(symbol.x, symbol.y)
        view._press(SimpleNamespace(x=x, y=y))
        view._motion(SimpleNamespace(x=x + 20, y=y + 15))
        view._release(SimpleNamespace(x=x + 20, y=y + 15))
        assert (tmp_path / 'layout.json').exists()
        view.highlight(symbol.id)
        assert view.canvas.find_withtag('selection')
        view.export_postscript(tmp_path / 'fitted.ps', fitted=True)
        view.export_postscript(tmp_path / 'full.ps')
        assert (tmp_path / 'fitted.ps').read_bytes().startswith(b'%!PS')
        assert (tmp_path / 'full.ps').read_bytes().startswith(b'%!PS')
        with monkeypatch.context() as p:
            from power_machine.gui.diagram.context_menu import ElementContextMenu
            p.setattr(ElementContextMenu, 'tk_popup', lambda *_a, **_k: None)
            symbol = view.scene.symbols[symbol.id]
            sx, sy = view._xy(symbol.x, symbol.y)
            view._context(SimpleNamespace(x=sx, y=sy, x_root=0, y_root=0))
        from power_machine.gui.i18n import tr
        button=next(b for b in view._menu.buttons if b.cget('text')==tr('v3.diagram.properties'))
        button.invoke()
        assert calls[-1] == ('select', symbol.id)
    finally:
        view.destroy()


def test_ieee39_result_boxes_remain_visible_after_dense_arrangement(root, tmp_path):
    model = load_model(ROOT, ROOT / 'cases/ieee39/model.json')
    result = run_loadflow(ROOT, new_study(ieee_root()), model)
    root.geometry('1552x824+0+0')
    callbacks = DiagramCallbacks(lambda *_: None, lambda *_: None, lambda *_: None)
    view = DiagramView(root, model, tmp_path / 'ieee39_layout.json', callbacks,
                       diagram_groups(model)[0]['id'])
    view.pack(fill='both', expand=True)
    root.update()
    try:
        view.set_result_boxes_visible(True)
        view.set_loadflow_results(result)
        root.update()
        assert not view.unplaced_labels
        assert view.canvas.find_withtag('result')
    finally:
        view.destroy()
        root.update()
