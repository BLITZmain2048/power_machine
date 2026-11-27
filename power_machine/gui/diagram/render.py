"""Dibujo y medición de rótulos Tk con detalle según escala."""

from power_machine.gui.theme import theme
from power_machine.gui.i18n import tr
from power_machine.gui.results_tables import display_value
import tkinter as tk
from tkinter import font as tkfont

from .layout import OFF_COLOR, clean_label, voltage_color


def wire_power(wire, result):
    """P [MW] orientada según la traza y el extremo declarado por el motor."""
    for end in ('from', 'to', 'hv', 'mv', 'lv'):
        bus = result.get('bus_' + end)
        power = result.get('p_' + end + '_mw')
        if bus is None or power is None:
            continue
        if wire.start == bus:
            return power
        if wire.end == bus:
            return -power
    return None


def wrap_pixels(text, font, width):
    """Ajusta todas las palabras sin recortar el contenido."""
    output = []
    for paragraph in text.splitlines():
        line = ''
        for word in paragraph.split():
            candidate = (line + ' ' + word).strip()
            if font.measure(candidate) <= width:
                line = candidate
                continue
            if line:
                output.append(line)
            line = word
        output.append(line)
    return '\n'.join(output)


def text_in_box(view, text, box, tags, *, fill=None, bold=False, background=None):
    c = view.canvas
    fill = fill or theme.color('text')
    x1, y1 = view._xy(*box[:2])
    x2, y2 = view._xy(*box[2:])
    font_key = (max(7, round(9 * view.scale)), bool(bold))
    if font_key not in view._fonts:
        view._fonts[font_key] = tkfont.Font(c, family=theme.font('body')[0], size=font_key[0],
                                          weight='bold' if bold else 'normal')
    font = view._fonts[font_key]
    label = wrap_pixels(clean_label(text), font, max(20, x2 - x1 - 4))
    ident = c.create_text(x1 + 2, y1, text=label, anchor='nw', justify='left',
                          font=font, fill=fill, tags=tags)
    if background:
        bounds = c.bbox(ident)
        rect = c.create_rectangle(bounds[0] - 2, bounds[1] - 2, bounds[2] + 2, bounds[3] + 2,
                                 fill=background, outline=theme.color('border'), tags=('result',) + tags[1:])
        c.tag_lower(rect, ident)
    return ident


def draw(view):
    from .view import result_text
    c, s = view.canvas, view.scale
    view._pin_cache={}
    c.delete('all')
    view.zoom_text.set(f'{s * 100:.0f} %')
    lx = 10
    levels = sorted({round(float(sym.un_kv), 3) for sym in view.scene.symbols.values()
                     if sym.un_kv is not None}, reverse=True)
    for kv in levels if view.color_mode == 'voltage' else []:
        ident = c.create_text(lx, c.winfo_height() - 8, text='━ '+display_value(kv)+' kV',
                             anchor='sw', fill=voltage_color(kv), font=theme.font('small'), tags='legend')
        lx = c.bbox(ident)[2] + 12
    for wire in view.scene.wires:
        item = view.index.get(wire.id, {})
        color = view.element_color(item, view.results.get(wire.id, {}), wire.un_kv)
        view._line(wire.points, fill=color, width=max(1, 1.2 * s), tags=('wire', 'element:' + wire.id))
        power = None if getattr(view,'cursor_t',None) is not None else wire_power(wire, view.results.get(wire.id, {}))
        if power is not None and power != 0 and not item.get('outserv') and view.results.get(wire.id, {}).get('energized') is not False and len(wire.points) > 1:
            a, b = wire.points[:2]
            if power < 0:
                a, b = b, a
            view._line([a, b], fill=color, arrow=tk.LAST, tags=('flow_arrow', 'small_symbol', 'element:' + wire.id))
    for key, symbol in view.scene.symbols.items():
        x, y = view._xy(symbol.x, symbol.y)
        kind, color = symbol.kind, view._color(symbol)
        item = view.index.get(symbol.item.get('id', key), symbol.item)
        tags = ('symbol', 'element:' + key)
        if kind in ('node','port_switch'):
            tags += ('small_symbol',)
        radius = max(2, 6 * s)
        if kind == 'generator':
            radius = max(7, 14 * s)
        if kind == 'bus':
            view._line([(symbol.x - symbol.width / 2, symbol.y), (symbol.x + symbol.width / 2, symbol.y)],
                       fill=color, width=max(2, 3 * s), tags=tags,
                       dash=(5, 3) if item.get('outserv') or (view.cursor_buses.get(key,{}) if getattr(view,'cursor_t',None) is not None else view.results.get(key,{})).get('energized') is False else ())
        elif kind == 'group':
            c.create_rectangle(x - symbol.width * s / 2, y - symbol.height * s / 2,
                               x + symbol.width * s / 2, y + symbol.height * s / 2,
                               fill=theme.color('surface_alt'), outline=color, width=2, tags=tags)
        elif kind == 'node':
            c.create_oval(x - 2, y - 2, x + 2, y + 2, fill=color, outline=color, tags=tags)
        elif kind in ('switch', 'port_switch'):
            r = max(2, (3 if kind == 'port_switch' else 4) * s)
            c.create_rectangle(x - r, y - r, x + r, y + r,
                               outline=color, fill=color if item.get('closed') else theme.color('diagram_bg'), tags=tags)
        elif kind in ('transformer', 'transformer3', 'generator'):
            centers = [(x, y)] if kind == 'generator' else [(x, y - 4 * s), (x, y + 4 * s)]
            if kind == 'transformer3':
                centers = [(x, y - 4 * s), (x - 4 * s, y + 4 * s), (x + 4 * s, y + 4 * s)]
            for cx, cy in centers:
                c.create_oval(cx - radius, cy - radius, cx + radius, cy + radius,
                              fill=theme.color('diagram_bg'), outline=color, width=max(1, s), tags=tags)
            if kind == 'generator':
                c.create_text(x, y, text='G', fill=color, font=theme.font('heading'), tags=tags)
        elif kind == 'load':
            c.create_polygon(x - radius, y - radius, x + radius, y - radius, x, y + radius,
                             fill=theme.color('diagram_bg'), outline=color, tags=tags)
        elif kind == 'outgoing':
            points = ([(symbol.x - 10, symbol.y), (symbol.x + 10, symbol.y)] if kind == 'outgoing'
                      else [(symbol.x, symbol.y - 10), (symbol.x, symbol.y + 10)])
            view._line(points,
                       fill=color, arrow=tk.LAST, width=max(1, s), tags=tags)
        elif kind == 'shunt':
            for dy in (-3, 3):
                view._line([(symbol.x - 7, symbol.y + dy), (symbol.x + 7, symbol.y + dy)], fill=color, tags=tags)
        else:
            c.create_rectangle(x - 3 * s, y - 8 * s, x + 3 * s, y + 8 * s,
                               fill=theme.color('diagram_bg'), outline=color, tags=tags)
        detailed = view.detail_visible(key)
        if symbol.label and symbol.label_box and (detailed or kind in ('group','generator') or (kind == 'bus' and s >= .14)):
            text_in_box(view, symbol.label, symbol.label_box,
                ('label', 'label_primary' if kind in ('bus','group','generator') else 'label_secondary',
                 'element:' + key), bold=kind == 'bus')
    draw_result_labels(view)
    # Rótulos de multiplicidad con fondo blanco, colocados fuera de las cajas.
    for wire in view.scene.wires:
        if wire.label:
            ident = text_in_box(view, wire.label, wire.label_box, ('label', 'bundle_label', 'element:' + wire.id))
            box = c.bbox(ident)
            center_x = view._xy((wire.label_box[0] + wire.label_box[2]) / 2, 0)[0]
            c.move(ident, center_x - (box[0] + box[2]) / 2, 0)
            box = c.bbox(ident)
            rect = c.create_rectangle(*box, fill=theme.color('diagram_bg'), outline='', tags='bundle_background')
            c.tag_lower(rect, ident)
    if not view.is_dense:
        arrange_callouts(view)
    else:
        arrange_dense_callouts(view)
    draw_bundle_guides(view)
    view._draw_highlight()
    view.draw_selected_result()
    c.create_text(c.winfo_width()-12,c.winfo_height()-8,anchor='se',
                  text=tr('diagram.symbols'),
                  font=theme.font('small'),fill=theme.color('text_muted'),tags='legend')
    suppress_overlapping_result_boxes(view)


def draw_result_labels(view):
    """Only result labels intersecting the current viewport are materialized."""
    from .view import result_text
    if getattr(view,'cursor_t',None) is not None:return
    for key,symbol in view.scene.symbols.items():
        if not view.result_visible(symbol):
            continue
        item = view.index.get(symbol.item.get('id',key),symbol.item)
        result = view.results.get(item.get('id',key))
        text = result_text(symbol,result,item=item,digits=view.result_digits,include_loading=True,
            boxes=view.box_formats,layers=getattr(view,'layers',None))
        if text:
            text_in_box(view,text,symbol.result_box,('result','element:'+key),
                background=theme.color('surface_alt'))


def suppress_overlapping_result_boxes(view):
    """Oculta cajas tardías cuando la superficie no admite una segunda caja."""
    c = view.canvas
    occupied = [c.bbox(item) for item in c.find_withtag('selected_detail')
                if c.type(item) == 'rectangle']
    rectangles = [item for item in c.find_withtag('result') if c.type(item) == 'rectangle']
    for rectangle in rectangles:
        if not c.type(rectangle):
            continue
        bounds = c.bbox(rectangle)
        if any(overlaps(bounds, other, 0) for other in occupied):
            element = next((tag[8:] for tag in c.gettags(rectangle)
                            if tag.startswith('element:')), None)
            if element is not None:
                for item in c.find_withtag('element:' + element):
                    if 'result' in c.gettags(item):
                        c.delete(item)
            continue
        occupied.append(bounds)


def arrange_dense_callouts(view):
    """Coloca detalles de malla sin ocultar resultados al ajustar la vista."""
    c = view.canvas
    view.unplaced_labels = []
    width, height = c.winfo_width(), c.winfo_height()
    def in_view(box):
        return box and box[2]>=0 and box[3]>=0 and box[0]<=width and box[1]<=height
    occupied = [box for i in c.find_withtag('symbol') if in_view(box:=c.bbox(i))]
    for ident in c.find_all():
        tags = c.gettags(ident)
        if c.type(ident) != 'text' or not ('label' in tags or 'result' in tags):
            continue
        box = c.bbox(ident)
        # Offscreen labels stay attached to their objects. Do not pull them into
        # the viewport, or spend collision-search time on invisible text.
        if not in_view(box):
            continue
        element = next((t[8:] for t in tags if t.startswith('element:')), None)
        chosen = None
        def fits(candidate):
            inside = (candidate[0] >= 4 and candidate[1] >= 4 and
                      candidate[2] <= width - 4 and candidate[3] <= height - 4)
            padded = (candidate[0]-3, candidate[1]-3, candidate[2]+3, candidate[3]+3)
            return inside and not any(overlaps(padded, other, 3) for other in occupied)
        for dx, dy in [(0, 0)] + [(dx, dy) for distance in (12, 30, 60, 90, 120, 160, 220)
                                  for dx, dy in ((0, distance), (0, -distance),
                                                 (distance, 0), (-distance, 0),
                                                 (distance, distance), (-distance, distance),
                                                 (distance, -distance), (-distance, -distance))]:
            candidate = (box[0]+dx, box[1]+dy, box[2]+dx, box[3]+dy)
            if fits(candidate):
                chosen = candidate
                break
        if chosen is None and len(view.scene.symbols) < 250:
            box_width, box_height = box[2] - box[0], box[3] - box[1]
            candidates = sorted(((x - box[0]) ** 2 + (y - box[1]) ** 2, x, y)
                                for x in range(4, max(5, int(width - box_width - 4)), 8)
                                for y in range(4, max(5, int(height - box_height - 4)), 8))
            for _, x, y in candidates:
                candidate = (x, y, x + box_width, y + box_height)
                if fits(candidate):
                    chosen = candidate
                    break
        related = [i for i in c.find_withtag('element:' + element)
                   if 'result' in tags and 'result' in c.gettags(i) and c.type(i) == 'rectangle']
        if chosen is None:
            view.unplaced_labels.append(element)
            if len(view.scene.symbols) >= 250:
                c.addtag_withtag('layout_hidden',ident)
                c.itemconfigure(ident, state='hidden')
                for i in related:
                    c.addtag_withtag('layout_hidden',i)
                    c.itemconfigure(i, state='hidden')
            continue
        dx, dy = chosen[0]-box[0], chosen[1]-box[1]
        c.move(ident, dx, dy)
        for i in related:
            c.move(i, dx, dy)
        occupied.append((chosen[0]-3, chosen[1]-3, chosen[2]+3, chosen[3]+3))
        if dx or dy:
            guide = c.create_line((box[0]+box[2])/2, (box[1]+box[3])/2,
                                 chosen[0], chosen[1], fill=theme.color('border'), dash=(2, 3), tags='label_guide')
            c.tag_lower(guide)


def draw_bundle_guides(view):
    """Vincula cada multiplicidad separada con un punto de su propia traza."""
    c = view.canvas
    for wire in view.scene.wires:
        if not wire.label:
            continue
        ident = next(i for i in c.find_withtag('element:' + wire.id)
                     if 'bundle_label' in c.gettags(i))
        left, top, right, bottom = c.bbox(ident)
        center = ((left + right) / 2, (top + bottom) / 2)
        candidates = []
        for a, b in zip(wire.points, wire.points[1:]):
            a, b = view._xy(*a), view._xy(*b)
            dx, dy = b[0] - a[0], b[1] - a[1]
            length2 = dx * dx + dy * dy
            t = max(0, min(1, ((center[0] - a[0]) * dx + (center[1] - a[1]) * dy) / length2)) if length2 else 0
            x, y = a[0] + t * dx, a[1] + t * dy
            edge = (max(left, min(right, x)), max(top, min(bottom, y)))
            candidates.append(((x - edge[0]) ** 2 + (y - edge[1]) ** 2, x, y, edge))
        distance2, x, y, edge = min(candidates)
        if distance2 <= 9:
            continue
        guide = c.create_line(x, y, *edge, fill=voltage_color(wire.un_kv),
                              dash=(2, 3), tags=('bundle_guide', 'element:' + wire.id))
        c.tag_lower(guide, 'symbol')


def overlaps(a, b, padding=6):
    if not a or not b:
        return False
    return (min(a[2], b[2]) + padding > max(a[0], b[0]) and
            min(a[3], b[3]) + padding > max(a[1], b[1]))


def arrange_callouts(view):
    """Reserva texto medido en pantalla, incluso en el zoom de ajuste.

    Conserva texto completo y fuente mínima de 7 pt. Cuando hace falta mover
    una caja se añade una guía fina hacia su posición original.
    """
    c = view.canvas
    width, height = c.winfo_width(), c.winfo_height()
    if width < 50 or height < 50:
        return
    obstacles = [c.bbox(i) for i in c.find_withtag('symbol')
                 if view.group is not None or c.type(i) != 'rectangle']
    obstacles += [c.bbox(i) for i in c.find_withtag('legend')]
    if view.group is None:
        obstacles += [c.bbox(i) for i in c.find_withtag('symbol') if c.type(i) == 'rectangle']
    texts = [i for i in c.find_all() if c.type(i) == 'text' and
             ('label' in c.gettags(i) or 'result' in c.gettags(i))]
    placed = []
    view.unplaced_labels = []
    for ident in texts:
        tags = c.gettags(ident)
        element = next((t[8:] for t in tags if t.startswith('element:')), None)
        symbol = view.scene.symbols.get(element)
        box = c.bbox(ident)
        relevant = obstacles
        if symbol and symbol.kind == 'group':
            # El nombre de una subestación pertenece al interior de su caja.
            relevant = []
        def fits(b):
            inside = (b[0] >= 4 and b[1] >= 4 and b[2] <= width - 4 and b[3] <= height - 4)
            return ((inside or view._user_zoom or getattr(view, '_exporting', False)) and
                    not any(overlaps(b, other) for other in relevant + placed))
        chosen = None
        if fits(box):
            chosen = box
        else:
            # Primero vecindad inmediata. El orden es estable entre repintados.
            for distance in range(6, max(width, height), 6):
                candidates = [(0, distance), (0, -distance), (distance, 0), (-distance, 0),
                              (distance, distance), (-distance, distance),
                              (distance, -distance), (-distance, -distance)]
                for dx, dy in candidates:
                    candidate = (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)
                    if fits(candidate):
                        chosen = candidate
                        break
                if chosen:
                    break
        if chosen is None and not view._user_zoom:
            # En paneles bajos quedan huecos que no pertenecen a los ocho
            # radios anteriores. Buscar el hueco libre más cercano, sin ocultar.
            bw, bh = box[2] - box[0], box[3] - box[1]
            candidates = sorted(((x - box[0]) ** 2 + (y - box[1]) ** 2, x, y)
                                for x in range(4, max(5, int(width - bw - 4)), 8)
                                for y in range(4, max(5, int(height - bh - 4)), 8))
            for _, x, y in candidates:
                candidate = (x, y, x + bw, y + bh)
                if fits(candidate):
                    chosen = candidate
                    break
        if chosen is None:
            view.unplaced_labels.append(element)
            placed.append(box)
            continue
        dx, dy = chosen[0] - box[0], chosen[1] - box[1]
        if dx or dy:
            c.move(ident, dx, dy)
            if 'result' in tags:
                for other in c.find_withtag('element:' + element):
                    if c.type(other) == 'rectangle' and 'result' in c.gettags(other):
                        c.move(other, dx, dy)
            if 'bundle_label' in tags:
                # El fondo se reconstruye después de mover el rótulo.
                pass
            if symbol and symbol.kind != 'group':
                origin = view._xy(symbol.x, symbol.y)
                guide = c.create_line(*origin, chosen[0], (chosen[1] + chosen[3]) / 2,
                                      fill=theme.color('border'), dash=(2, 3), tags='label_guide')
                c.tag_lower(guide)
        placed.append(chosen)
    c.delete('bundle_background')
    for ident in c.find_withtag('bundle_label'):
        rect = c.create_rectangle(*c.bbox(ident), fill=theme.color('diagram_bg'), outline='', tags='bundle_background')
        c.tag_lower(rect, ident)
