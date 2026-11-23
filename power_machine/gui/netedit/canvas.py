"""Lienzo de edición que reutiliza la geometría del unifilar operativo."""
from power_machine.gui.theme import theme
import tkinter as tk
from power_machine.gui.diagram.layout import build_scene, diagram_groups, apply_positions, voltage_color
from power_machine.netedit.commands import index_model


def scene_positions(model, layout, group=None):
    scene = build_scene(model, group)
    apply_positions(scene, layout.get('groups', {}).get(group or '__general__', {}))
    return {iid: (s.x, s.y) for iid, s in scene.symbols.items()}


class EditorCanvas(tk.Canvas):
    def __init__(self, parent, controller, *, on_edit=None, on_port=None, on_create=None, on_group=None):
        super().__init__(parent, background=theme.color('diagram_bg'), width=600, height=300, highlightthickness=0)
        self.controller = controller
        self.on_edit = on_edit or (lambda iid: None)
        self.on_port = on_port or (lambda iid: None)
        self.on_create = on_create or (lambda x, y: None)
        self.on_group = on_group or (lambda group: None)
        self.group = None
        self.scale = 1.0
        self._drag = None
        self._pending_port = None
        self.bind('<ButtonPress-1>', self.press)
        self.bind('<B1-Motion>', self.motion)
        self.bind('<ButtonRelease-1>', self.release)
        self.bind('<Motion>', self.preview_wire)
        self.bind('<Double-Button-1>', self.double)
        self.bind('<Button-3>', lambda e: self.on_port(self.hit(e)))
        self.bind('<Escape>', lambda e: self.cancel_port())
        self.bind('<MouseWheel>', lambda e: self.zoom(1.2 if e.delta > 0 else 1/1.2))
        self.bind('<ButtonPress-2>', lambda e: self.scan_mark(e.x, e.y))
        self.bind('<B2-Motion>', lambda e: self.scan_dragto(e.x, e.y, gain=1))

    def navigate(self, group):
        self.group = group
        self.redraw()
        self.fit()

    def zoom(self, factor):
        self.scale = min(3, max(.12, self.scale * factor))
        self.redraw()

    def fit(self):
        self.update_idletasks()
        self.scale = 1
        self.redraw()
        bounds = self.bbox('all')
        if bounds:
            self.scale = min(1.2, max(.85, min((self.winfo_width()-30)/max(1, bounds[2]-bounds[0]),
                                               (self.winfo_height()-30)/max(1, bounds[3]-bounds[1]))))
            self.redraw()
            self.xview_moveto(0)
            self.yview_moveto(0)

    def redraw(self):
        self.delete('all')
        model = self.controller.model
        drafts = self.controller.drafts
        if self.group and self.group not in {g['id'] for g in diagram_groups(model)}:
            self.group = None
        scene = build_scene(model, self.group)
        apply_positions(scene, self.controller.layout.get('groups', {}).get(self.group or '__general__', {}))
        self.scene = scene
        self.positions = {iid: (s.x, s.y) for iid, s in scene.symbols.items()}
        s = self.scale
        for wire in scene.wires:
            self.create_line(*[v*s for p in wire.points for v in p], fill=voltage_color(wire.un_kv),
                             tags=('wire', 'element:' + wire.id))
        index = index_model(model)
        self.index = index
        dense = sum(sym.kind == 'bus' for sym in scene.symbols.values()) >= 20
        for iid, symbol in scene.symbols.items():
            x, y = symbol.x*s, symbol.y*s
            tags = ('symbol', 'element:' + iid)
            color = voltage_color(symbol.un_kv)
            if symbol.kind == 'group':
                w, h = symbol.width*s/2, symbol.height*s/2
                self.create_rectangle(x-w, y-h, x+w, y+h, fill=theme.color('surface_alt'), outline=color, tags=tags)
                self.create_text(x, y, text=symbol.label, width=max(50, 2*w-8),
                                 font=(theme.font('small')[0], max(7, round(theme.font('small')[1]*s))), tags=tags)
                continue
            item = index.get(iid, ('', {}))[1]
            fill = theme.color('deenergized') if item.get('outserv') else theme.color('diagram_bg')
            if symbol.kind in ('bus', 'node'):
                self.create_line(x-symbol.width*s/2, y, x+symbol.width*s/2, y, width=max(3, 4*s), fill=color, tags=tags)
            elif symbol.kind in ('gen', 'generator'):
                self.create_oval(x-12*s, y-12*s, x+12*s, y+12*s, fill=fill, outline=color, tags=tags)
                self.create_text(x, y, text='G', tags=tags)
            else:
                self.create_rectangle(x-9*s, y-9*s, x+9*s, y+9*s, fill=fill, outline=color, tags=tags)
            if dense and symbol.kind not in ('bus', 'group') and iid != self.controller.selected_id:
                continue
            box = symbol.label_box
            if box and symbol.label:
                self.create_text(box[0]*s, box[1]*s, anchor='nw', text=symbol.label,
                                 width=max(30, (box[2]-box[0])*s), font=(theme.font('small')[0], max(7, round(theme.font('small')[1]*s))), tags=tags)
            elif symbol.label:
                self.create_text(x, y+20*s, text=symbol.label, width=150*s, tags=tags)
            if iid == self.controller.selected_id:
                self.create_rectangle(x-20*s, y-18*s, x+20*s, y+18*s, outline=theme.color('warn'), width=2, tags=tags)
        # Borradores visibles, separados del modelo calculable.
        for i, (iid, item) in enumerate(drafts.items()):
            x, y = item.get('editor_position', [80+i*180, 40])
            self.positions[iid] = (x, y)
            tags = ('draft', 'symbol', 'element:' + iid)
            self.create_rectangle((x-30)*s, (y-16)*s, (x+30)*s, (y+16)*s, dash=(3, 3), tags=tags)
            self.create_text(x*s, (y+20)*s, text='Borrador\n'+item.get('name', ''), width=150*s, tags=tags)
        self.port_points = {}
        for iid, (x, y) in self.positions.items():
            if iid != self.controller.selected_id and iid not in drafts:
                continue
            for n, port in enumerate(self.controller.ports(iid)):
                px, py = (x-16+n*30)*s, (y-22)*s
                self.port_points[(iid, port['field'])] = (px, py)
                self.create_oval(px-4, py-4, px+4, py+4, fill=theme.color('ok') if port.get('bus_id') else theme.color('warn'),
                                 tags=('port', 'element:'+iid, 'port:'+port['field']))
        bounds = self.bbox('all')
        if bounds:
            self.configure(scrollregion=(bounds[0]-20, bounds[1]-20, bounds[2]+20, bounds[3]+20))

    def hit_tags(self, event):
        x, y = self.canvasx(event.x), self.canvasy(event.y)
        for ident in reversed(self.find_overlapping(x-4, y-4, x+4, y+4)):
            tags = self.gettags(ident)
            if any(t in tags for t in ('symbol', 'draft', 'port')):
                return tags
        return ()

    def hit(self, event):
        return next((t[8:] for t in self.hit_tags(event) if t.startswith('element:')), None)

    def double(self, event):
        iid = self.hit(event)
        if iid in self.scene.symbols and self.scene.symbols[iid].kind == 'group':
            self.on_group(iid)
        else:
            self.on_edit(iid)

    def begin_port(self, object_id, port):
        self._pending_port = (object_id, port)
        self.configure(cursor='crosshair')
        self.focus_set()

    def cancel_port(self):
        self._pending_port = None
        self.delete('preview')
        self.configure(cursor='')

    def preview_wire(self, event):
        self.delete('preview')
        self.delete('hover')
        iid = self.hit(event)
        if iid in self.index and not self._pending_port:
            item = self.index[iid][1]
            x, y = self.canvasx(event.x)+15, self.canvasy(event.y)+18
            label = self.create_text(x, y, anchor='nw', text=item.get('name', iid)+'\n'+iid, width=280, tags='hover')
            box = self.bbox(label)
            rect = self.create_rectangle(*box, fill=theme.color('surface'), outline=theme.color('border'), tags='hover')
            self.tag_lower(rect, label)
        if self._pending_port:
            start = self.port_points.get(self._pending_port)
            if start:
                self.create_line(*start, self.canvasx(event.x), self.canvasy(event.y), dash=(4, 3), fill=theme.color('warn'), tags='preview')

    def press(self, event):
        self.focus_set()
        iid = self.hit(event)
        if self._pending_port:
            item = self.controller.session.get_element(iid)
            if item and item['group'] == 'buses':
                try:
                    self.controller.reconnect(*self._pending_port, iid)
                    self.cancel_port()
                except ValueError as exc:
                    self.controller.context.log('ERROR', str(exc))
            return
        port = next((t[5:] for t in self.hit_tags(event) if t.startswith('port:')), None)
        if port:
            self.begin_port(iid, port)
            return
        if not iid:
            self.on_create(self.canvasx(event.x)/self.scale, self.canvasy(event.y)/self.scale)
            return
        if iid not in index_model(self.controller.model) and iid not in self.controller.drafts:
            return
        self.controller.select(iid)
        self.redraw()
        self._drag = (iid, self.canvasx(event.x), self.canvasy(event.y), *self.positions[iid], False)

    def motion(self, event):
        if self._drag:
            iid, x, y, ox, oy, _ = self._drag
            nx, ny = self.canvasx(event.x), self.canvasy(event.y)
            self.move('element:'+iid, nx-x, ny-y)
            self._drag = (iid, nx, ny, ox+(nx-x)/self.scale, oy+(ny-y)/self.scale, True)

    def release(self, event):
        if self._drag:
            iid, _, _, x, y, moved = self._drag
            self._drag = None
            if moved:
                try:
                    self.controller.move(iid, x, y, self.group or '__general__')
                except ValueError as exc:
                    self.controller.context.log('ERROR', str(exc))
            self.redraw()
