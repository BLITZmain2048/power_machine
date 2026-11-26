"""Canvas transforms during gestures, with one settled zoom repaint."""
from .layout import apply_positions


class CanvasNavigation:
    _fixed_tags = ('legend', 'selected_detail', 'minimap', 'time_cursor')

    def _apply_lod(self):
        dense = self.is_dense
        for tag, visible in (
            ('label_primary', not dense or self.scale >= .14),
            ('label_secondary', not dense or self.scale >= self.detail_zoom),
            ('small_symbol', not dense or self.scale >= .20),
            ('result', self.show_results and (not dense or self.scale >= .35))):
            self.canvas.itemconfigure(tag, state='normal' if visible else 'hidden')
        self.canvas.itemconfigure('layout_hidden', state='hidden')

    def _pan_pixels(self, dx, dy):
        self._hide_tooltip()
        self.offset = self.offset[0]+dx, self.offset[1]+dy
        self._user_zoom = True
        self.canvas.move('all', dx, dy)
        for tag in self._fixed_tags:
            self.canvas.move(tag, -dx, -dy)

    def _zoom(self, x, y, factor):
        self._hide_tooltip()
        self._user_zoom = True
        new = min(4., max(.025, self.scale*factor))
        ratio = new/self.scale
        self.offset = x-(x-self.offset[0])*ratio, y-(y-self.offset[1])*ratio
        self.scale = new
        self.canvas.scale('all',x,y,ratio,ratio)
        for tag in self._fixed_tags:
            self.canvas.scale(tag,x,y,1/ratio,1/ratio)
        self._apply_lod()
        self.zoom_text.set(f'{new*100:.0f} %')
        if hasattr(self,'_set_status'):
            self._set_status()
        if self._paint_job:
            self.after_cancel(self._paint_job)
        self._paint_job = self.after(180,self._finish_zoom)

    def _finish_zoom(self):
        self._paint_job = None
        if self._drag:
            self._paint_job = self.after(180,self._finish_zoom)
        else:
            self.draw()

    def _move_symbols(self, ids, dx, dy):
        """Move only selected symbols and incident traces, keeping stable knees.

        The initial trace is retained for the gesture. Orthogonal endpoint bends
        are recomputed from it rather than accumulated on every pointer event.
        Unrelated canvas items and the scene object keep their identities.
        """
        self._hide_tooltip()
        ids = {key for key in ids if key in self.scene.symbols}
        if not getattr(self,'_moving_wires',None):
            self._moving_wires = [(wire,list(wire.points),
                {end:(self.scene.symbols[end].x,self.scene.symbols[end].y)
                 for end in (wire.start,wire.end) if end in self.scene.symbols})
                for wire in self.scene.wires if wire.start in ids or wire.end in ids]
        sx, sy = dx*self.scale*self.x_factor, dy*self.scale
        positions = {}
        for key in ids:
            symbol = self.scene.symbols[key]
            positions[key] = symbol.x+dx, symbol.y+dy
            for handle in self.canvas.find_withtag('element:'+key):
                if any(t in self.canvas.gettags(handle) for t in ('symbol','label','result','label_guide')):
                    self.canvas.move(handle,sx,sy)
            self.canvas.move('overlay:'+key,sx,sy)
            self.positions[key] = positions[key]
        # Shift boxes without using apply_positions' accumulating wire bends.
        wires, self.scene.wires = self.scene.wires, []
        try:
            apply_positions(self.scene,positions)
        finally:
            self.scene.wires = wires
        from .render import wire_power
        for wire, original, starts in self._moving_wires:
            points = list(original)
            for key, pos in ((wire.start,0),(wire.end,-1)):
                if key not in starts:
                    continue
                symbol = self.scene.symbols[key]
                ddx, ddy = symbol.x-starts[key][0], symbol.y-starts[key][1]
                if not (ddx or ddy):
                    continue
                old = original[pos]
                new = old[0]+ddx, old[1]+ddy
                if self.scene.group is None:
                    points[pos] = new
                elif pos == 0:
                    points = [new,(new[0],old[1]),old]+points[1:]
                else:
                    points = points[:-1]+[old,(new[0],old[1]),new]
            wire.points = points
            for handle in self.canvas.find_withtag('element:'+wire.id):
                tags = self.canvas.gettags(handle)
                if 'wire' in tags:
                    self.canvas.coords(handle,*[v for point in points for v in self._xy(*point)])
                elif 'flow_arrow' in tags:
                    a,b = points[:2]
                    if (wire_power(wire,self.results.get(wire.id,{})) or 0)<0:
                        a,b = b,a
                    self.canvas.coords(handle,*self._xy(*a),*self._xy(*b))
        self._draw_highlight()
        if self.pinned_data:
            self._draw_pins()

    def _finish_pan(self):
        if getattr(self,'minimap_visible',False):
            self._draw_minimap()
        # Result labels entering the viewport are refreshed without touching the
        # geometry. The default clean view needs no work at all on pan release.
        if self.show_results:
            if getattr(self,'_labels_job',None):
                self.after_cancel(self._labels_job)
            self._labels_job = self.after(180,self._refresh_result_labels)

    def _refresh_result_labels(self):
        from .render import draw_result_labels, suppress_overlapping_result_boxes
        self._labels_job = None
        self.canvas.delete('result')
        draw_result_labels(self)
        suppress_overlapping_result_boxes(self)
        self._apply_lod()
        self.canvas.tag_raise('pinned_data')
