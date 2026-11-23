"""Reusable diagram host. Electrical actions are requests to the application."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from ..theme import theme
from ..i18n import tr
from .view import DiagramView, DiagramCallbacks, result_text
from .layout import diagram_groups, save_positions
from .icons import icon, Tooltip
from .editing import DiagramEditing


class DiagramWidget(DiagramEditing,DiagramView):
    def __init__(self, parent, app, bus, group=None):
        self.app, self.bus = app, bus
        self.selected_ids = set()
        self.ranking_ids = ()
        self.contingency_id = None
        self.mode = 'select'
        self._space = False
        self._rubber = None
        self._subscriptions = []
        self._publishing_selection = False
        self.layers = dict(U=True, angle=True, P=True, Q=True, loading=True)
        self.minimap_visible = False
        self.legend_visible = True
        self.detail_expanded = True
        self._icon_buttons = {}
        model = getattr(app, 'operational', app.model)
        groups = diagram_groups(model)
        if group is None and len(groups) == 1:
            group = groups[0]['id']
        layout = getattr(app, 'layout_path', None)
        if layout is None:
            from ..paths import results_root
            from power_machine.netedit.schema import model_sha256
            layout = results_root(getattr(app, 'root_path', None)) / (model_sha256(app.model) + '_layout.json')
        callbacks = DiagramCallbacks(self._select_one,
            lambda key: bus.publish('element.open', id=key), self.request_action)
        super().__init__(parent, model, layout, callbacks, group)
        # Replace the legacy bars, keeping the rendering and presentation API.
        for child in tuple(self.winfo_children()):
            if child not in (self.canvas, self._body):
                child.destroy()
        self._toolbar = ttk.Frame(self)
        self._toolbar.pack(fill='x', before=self._body)
        self._options_bar = ttk.Frame(self)
        self._options_bar.pack(fill='x', before=self._body)
        self._legend_label = ttk.Label(self, textvariable=self._legend_var)
        self._legend_label.pack(fill='x', before=self._body)
        self.canvas.pack_configure(side='left')
        self._detail_panel = ttk.Frame(self._body, width=240, padding=8)
        self._detail_panel.pack_propagate(False)
        self._detail_title = tk.StringVar(self)
        self._detail_text = tk.StringVar(self)
        self._detail_heading = ttk.Label(self._detail_panel, textvariable=self._detail_title,
                                        font=theme.font('heading'), wraplength=220)
        self._detail_heading.pack(fill='x', pady=8)
        ttk.Label(self._detail_panel, textvariable=self._detail_text, wraplength=220,
                  justify='left').pack(fill='x')
        self._footer = tk.Canvas(self, height=54, highlightthickness=0)
        self._footer.pack(side='bottom',fill='x',before=self._body)
        self._footer.bind('<Configure>', lambda e: self._draw_footer())
        self._status_label=ttk.Label(self, textvariable=self.status)
        self._status_label.pack(side='bottom',fill='x',before=self._footer)
        self.bind('<Configure>',self._layout_labels,add='+')
        self.setup_editing()
        self.retranslate()
        self.canvas.bind('<KeyPress-space>', lambda e: self._set_space(True))
        self.canvas.bind('<KeyRelease-space>', lambda e: self._set_space(False))
        self.canvas.bind('<FocusOut>', lambda e: self._set_space(False))
        for topic, callback in (
            ('selection.changed', self._selection_changed),
            ('theme.changed', self._theme_changed),
            ('language.changed', self.retranslate),
            ('network.edited', self._network_changed),
            ('results.updated', self._results_changed),
            ('results.stale', self._stale),
            ('contingency.selected', self._contingency_changed),
            ('contingency.ranking.changed', self._ranking_changed)):
            self._subscriptions.append(bus.subscribe(topic, callback))
        self._theme_changed()

    def _canvas_parent(self):
        self._body = ttk.Frame(self)
        self._body.pack(fill='both', expand=True)
        return self._body

    def retranslate(self, **payload):
        if not hasattr(self, '_toolbar'):
            return
        self._release_icons()
        for bar in (self._toolbar, self._options_bar):
            for child in bar.winfo_children():
                child.destroy()
        self._icon_buttons = {}
        for mode in ('select', 'move', 'pan'):
            self._icon_button(self._toolbar, mode, lambda m=mode: self.set_tool(m))
        ttk.Separator(self._toolbar, orient='vertical').pack(side='left', fill='y', padx=6)
        for key, command in (('fit', self.fit), ('save', self.save_layout),
                             ('legend', self.toggle_legend), ('minimap', self.toggle_minimap),
                             ('detail', self.toggle_detail)):
            self._icon_button(self._toolbar, key, command)
        ttk.Button(self._toolbar, textvariable=self.zoom_text, command=self._actual_size).pack(side='left')
        layers = self._icon_button(self._options_bar, 'layers', kind=ttk.Menubutton)
        menu = tk.Menu(layers, tearoff=False, background=theme.color('surface'), foreground=theme.color('text'))
        self._layer_vars = {}
        for key in self.layers:
            var = self._layer_vars[key] = tk.BooleanVar(self, self.layers[key])
            menu.add_checkbutton(label=tr('diagram.layer.' + key), variable=var,
                command=lambda k=key, v=var: self.set_layer(k, v.get()))
        layers.configure(menu=menu)
        modes = ('voltage', 'voltage_band', 'loading', 'state')
        labels = [tr('diagram.color.' + m) for m in modes]
        self._choices = ttk.Combobox(self._options_bar, state='readonly', width=24, values=labels)
        self._choices.current(modes.index(self.color_mode))
        self._choices.pack(side='left', padx=4)
        self._choices.bind('<<ComboboxSelected>>', lambda e: self.set_color_mode(modes[self._choices.current()]))
        search = ttk.Entry(self._options_bar, textvariable=self._search_var, width=22)
        search.pack(side='left', padx=4)
        search.bind('<Return>', lambda e: self.find_element(self._search_var.get()))
        self._icon_button(self._options_bar, 'search', self._search)
        self.editing_toolbar()
        self._update_legend()
        self._set_status()
        self.draw()

    def _icon_button(self, parent, name, command=None, kind=ttk.Button):
        options = dict(padding=4, takefocus=True)
        if command is not None:
            options['command'] = command
        button = kind(parent, **options)
        button.pack(side='left', padx=1, pady=2)
        button._diagram_tooltip = Tooltip(button, tr('diagram.' + name))
        self._icon_buttons[name] = button
        self._refresh_icon(name)
        return button

    def _refresh_icon(self, name):
        button = self._icon_buttons[name]
        button._diagram_image = icon(button, name, active=name == self.mode)
        button.configure(image=button._diagram_image)

    def _release_icons(self):
        # Release Tcl resources on the Tk thread, including when rebuilding a bar.
        for button in self._icon_buttons.values():
            try:
                button._diagram_tooltip.hide()
            except tk.TclError:
                pass
            button._diagram_image = None
        self._icon_buttons.clear()

    def _set_status(self):
        name = 'IEEE 39' if getattr(self.scene, 'curated', False) else tr('diagram.network')
        self.status.set(tr('diagram.status', name=name, zoom=round(self.scale*100))+' · '+tr('diagram.focus_hint'))

    def _layout_labels(self,event):
        if event.widget is self:
            self._legend_label.configure(wraplength=max(200,event.width-16))
            self._draw_footer()

    def toggle_detail(self):
        self.detail_expanded = not self.detail_expanded
        self.draw_selected_result()

    def draw_selected_result(self):
        self.canvas.delete('selected_detail')
        if not hasattr(self, '_detail_panel'):
            return
        symbol = self.scene.symbols.get(self.selected_id)
        if not self.detail_expanded or not symbol or not self.show_results:
            self._detail_panel.pack_forget()
            return
        item = self.index.get(symbol.item.get('id', symbol.id), symbol.item)
        row = self.results.get(item.get('id'), {})
        self._detail_title.set(item.get('name', symbol.id))
        sections = []
        ends = [e for e in ('from','to','hv','mv','lv') if 'bus_' + e in row]
        if symbol.kind == 'bus' or not ends:
            sections.append(result_text(symbol, row, item=item, digits=self.result_digits,
                include_loading=True, boxes=self.box_formats, layers=self.layers))
        else:
            for end in ends:
                bus = row['bus_' + end]
                local = type(symbol)(**{**symbol.__dict__, 'item': dict(symbol.item, _local_bus=bus)})
                sections.append(tr('diagram.end') + ' ' + self.index.get(bus, {}).get('name',bus) + '\n' +
                    result_text(local,row,item=item,digits=self.result_digits,include_loading=True,
                                boxes=self.box_formats,layers=self.layers))
        rating = row.get('loading_rating', {})
        if rating.get('source') == 'rate_a_mva':
            sections.append(tr('diagram.rating_rate_a', value=rating['rating_mva']))
        self._detail_text.set('\n\n'.join(sections) or tr('diagram.no_results'))
        if not self._detail_panel.winfo_manager():
            self._detail_panel.pack(side='right', fill='y', before=self.canvas)

    def _search(self):
        key = self.find_element(self._search_var.get())
        if key:
            self._select_one(key)

    def _set_space(self, value):
        self._space = value
        return 'break'

    def set_tool(self, mode):
        self.mode = mode
        self.canvas.configure(cursor='fleur' if mode in ('pan', 'move') else 'arrow')
        for name in ('select','move','pan'):
            if name in self._icon_buttons:
                self._refresh_icon(name)

    def set_color_mode(self, mode='voltage', **kwargs):
        super().set_color_mode(mode, **kwargs)
        if hasattr(self, '_choices'):
            self._choices.current(('voltage', 'voltage_band', 'loading', 'state').index(mode))

    def set_operating_state(self, model):
        super().set_operating_state(model)
        self.selected_ids.intersection_update(self.index)
        self._rebuild_scene()
        self.draw()

    def _select_one(self, key):
        controller=getattr(self.app,'_diagram_edit_controller',None)
        if controller:controller.selected_id=key
        self.select_ids([key])

    def select_ids(self, ids, publish=True):
        self.selected_ids = set(ids) & set(self.index)
        self.selected_id = next(iter(sorted(self.selected_ids)), None)
        self._draw_highlight()
        self.draw_selected_result()
        if publish:
            self._publishing_selection = True
            try:
                self.bus.publish('selection.changed', ids=sorted(self.selected_ids), source='diagram')
            finally:
                self._publishing_selection = False

    def _selection_changed(self, ids, source=None, **payload):
        if not self._publishing_selection:
            self.select_ids(ids, publish=False)

    def _press(self, event):
        self.canvas.focus_set()
        if self.drawing_press(event):return
        if self.mode == 'pan' or self._space:
            return self._pan_press(event)
        key = self._hit(event)
        if key:
            ids = self.selected_ids ^ {key} if event.state & 0x0005 else (
                self.selected_ids if key in self.selected_ids else {key})
            self.select_ids(ids)
            self._drag = (key, event.x, event.y, False) if self.mode == 'move' and self.editing else None
        else:
            if not event.state & 0x0005:
                self.select_ids([])
            self._rubber = (event.x, event.y)

    def _motion(self, event):
        self.drawing_motion(event)
        if self._rubber:
            self.canvas.delete('rubber')
            self.canvas.create_rectangle(*self._rubber, event.x, event.y,
                outline=theme.color('accent'), dash=(3, 2), tags='rubber')
            return
        if self._drag and self._drag[0]:
            key, x, y, moved = self._drag
            dx, dy = (event.x-x)/(self.scale*self.x_factor), (event.y-y)/self.scale
            for ident in self.selected_ids:
                symbol = self.scene.symbols.get(ident)
                if symbol:
                    self.positions[ident] = (symbol.x+dx, symbol.y+dy)
            self._rebuild_scene()
            self._drag = (key, event.x, event.y, moved or bool(dx or dy))
            if self._paint_job is None:
                self._paint_job = self.after(16, self.draw)
        else:
            if self._drag:
                key, x, y, moved = self._drag
                dx, dy = event.x-x, event.y-y
                self.offset = (self.offset[0]+dx, self.offset[1]+dy)
                self._user_zoom = True
                self.canvas.move('all', dx, dy)
                for tag in ('legend', 'selected_detail', 'minimap'):
                    self.canvas.move(tag, -dx, -dy)
                self._drag = (key, event.x, event.y, moved or bool(dx or dy))

    def _release(self, event):
        panned = self._drag and self._drag[0] is None and self._drag[3]
        if self.editing and self._drag and self._drag[0] and self._drag[3]:
            controller=self.editor_controller
            if controller:
                from power_machine.netedit import edit_command
                commands=[]
                for key in self.selected_ids:
                    symbol=self.scene.symbols.get(key)
                    if not symbol:continue
                    if key in controller.drafts:
                        controller.move(key,symbol.x,symbol.y)
                    else:
                        item=controller.session.get_element(key)
                        commands.append(edit_command(controller.model,'move',item['group'],key,
                            dict(x_canvas=symbol.x,y_canvas=symbol.y,diagram_group='__editor__')))
                if commands:controller.apply(commands)
        if self._rubber:
            x, y = self._rubber
            bounds = (min(x,event.x), min(y,event.y), max(x,event.x), max(y,event.y))
            ids = {tag[8:] for ident in self.canvas.find_enclosed(*bounds)
                   for tag in self.canvas.gettags(ident) if tag.startswith('element:')}
            self.select_ids(self.selected_ids | ids)
            self._rubber = None
            self.canvas.delete('rubber')
        super()._release(event)
        if panned:
            self.draw()

    def _rebuild_scene(self):
        self.editing_positions()
        super()._rebuild_scene()
        self.editing_scene()

    def save_layout(self):
        self.positions.update({key: (s.x, s.y) for key, s in self.scene.symbols.items()})
        try:
            save_positions(self.layout_path, self.group, self.positions)
            self.status.set(tr('diagram.saved'))
        except (OSError, ValueError) as exc:
            self.status.set(tr('diagram.save_error', error=str(exc)))

    def request_action(self, key, action):
        if (getattr(getattr(self.app,'document',None),'network',None) is not None and
                (action=='outserv' or action in ('switch_open','switch_close') and
                 self.index.get(key,{}).get('class') in ('ElmCoup','StaSwitch'))):
            if not self.editing:
                self.status.set(tr('edit.readonly'))
                return
            changes=dict(outserv=not bool(self.index[key].get('outserv'))) if action=='outserv' else dict(closed=action=='switch_close')
            self.editor_controller.edit(key,changes)
            return
        if action == 'properties':
            self.bus.publish('element.open', id=key)
        elif action in ('short_circuit', 'disconnect'):
            self.bus.publish('study.event.requested', kind='fault' if action == 'short_circuit' else 'disconnect', target_id=key)
        elif action == 'results':
            self.bus.publish('results.open', id=key)
            callback = getattr(self.app, 'show_results', None)
            if callback:
                callback()
        elif action == 'outserv' or (action in ('switch_open', 'switch_close') and
                self.index.get(key, {}).get('class') in ('StaSwitch', 'ElmCoup')):
            available = getattr(self.app, 'available', lambda: True)
            if not available():
                return
            document = getattr(self.app, 'document', None)
            if document is not None:
                item = self.index[key]
                changes = {'outserv': not bool(item.get('outserv'))} if action == 'outserv' else {'closed': action == 'switch_close'}
                document.operation(item, changes)
                self.app.update_document()
                self.bus.publish('network.edited', ids=[key])
                self.bus.publish('results.stale', reason=tr('diagram.operating_changed'))
        else:
            callback = getattr(self.app, 'context_action', None)
            if callback:
                callback(key, action)

    def _theme_changed(self, **payload):
        self.canvas.configure(background=theme.color('diagram_bg'))
        self._fonts.clear()
        for name in self._icon_buttons:
            self._refresh_icon(name)
        if hasattr(self,'_toolbox'):self._build_toolbox()
        self.draw()

    def _network_changed(self, **payload):
        self.set_operating_state(getattr(self.app, 'operational', self.app.model))
        self._rebuild_scene()
        self.draw()

    def _results_changed(self, study=None, result_id=None, **payload):
        result = payload.get('result') or getattr(self.app, 'result', None)
        if isinstance(result, dict):
            self.set_loadflow_results(result)

    def _hover(self, event):
        self.drawing_motion(event)
        super()._hover(event)
        x = (event.x-self.offset[0])/(self.scale*self.x_factor)
        y = (event.y-self.offset[1])/self.scale
        key = self._hit(event)
        label = self.index.get(key, {}).get('name', key or '')
        self.status.set(f'{x:.0f}, {y:.0f}  ·  {label}  ·  {self.scale*100:.0f} % · '+tr('diagram.focus_hint'))

    def _stale(self, reason='', **payload):
        self.results = {}
        self.status.set(tr('diagram.stale', reason=reason))
        self.draw()

    def set_layer(self, name, visible):
        self.layers[name] = bool(visible)
        self.draw()
        self.bus.publish('diagram.overlay.changed', mode=name)

    def set_contingency(self, target_id=None, ranking_ids=()):
        self.contingency_id = target_id
        self.ranking_ids = tuple(dict.fromkeys(ranking_ids))
        self._update_legend()
        self.draw()

    def _update_legend(self):
        super()._update_legend()
        if self.contingency_id or self.ranking_ids:
            self._legend_var.set(self._legend_var.get() + ' · ' + tr('diagram.contingency_legend'))

    def _contingency_changed(self, target_id=None, **payload):
        self.set_contingency(target_id, self.ranking_ids)

    def _ranking_changed(self, ids=(), **payload):
        self.set_contingency(self.contingency_id, ids)

    def _draw_highlight(self):
        super()._draw_highlight()
        for key in self.selected_ids:
            if key == self.selected_id:
                continue
            items = [item for item in self.canvas.find_withtag('element:' + key)
                     if 'symbol' in self.canvas.gettags(item)]
            box = self.canvas.bbox(*items) if items else None
            if box:
                self.canvas.create_rectangle(*box, outline=theme.color('accent'), width=2, tags='selection')

    def toggle_legend(self):
        self.legend_visible = not self.legend_visible
        if self.legend_visible:
            self._legend_label.pack(fill='x', before=self._body)
            self._footer.pack(side='bottom',fill='x',before=self._body)
        else:
            self._legend_label.pack_forget()
            self._footer.pack_forget()
        self.draw()

    def toggle_minimap(self):
        self.minimap_visible = not self.minimap_visible
        self.draw()

    def draw(self):
        super().draw()
        for key in dict.fromkeys((*self.ranking_ids, self.contingency_id)):
            if not key:
                continue
            color = theme.color('error' if key == self.contingency_id else 'warn')
            for item in self.canvas.find_withtag('element:' + key):
                if self.canvas.type(item) == 'line':
                    self.canvas.itemconfigure(item, fill=color, width=4)
            symbol = self.scene.symbols.get(key)
            if symbol:
                x,y = self._xy(symbol.x, symbol.y)
                self.canvas.create_oval(x-12,y-12,x+12,y+12,outline=color,width=3,tags='contingency')
                if key in self.ranking_ids:
                    self.canvas.create_text(x+14,y-14,text=str(self.ranking_ids.index(key)+1),
                        fill=color,font=theme.font('heading'),anchor='sw',tags=('contingency','ranking'))
        if hasattr(self, '_footer'):
            self.canvas.delete('legend')
            self._draw_footer()
        elif not self.legend_visible:
            self.canvas.delete('legend')
        if self.minimap_visible:
            self._draw_minimap()

    def _draw_footer(self):
        if not hasattr(self, '_footer'):
            return
        c = self._footer
        c.configure(background=theme.color('bg'))
        c.delete('all')
        entries = []
        if self.color_mode == 'voltage':
            levels = sorted({s.un_kv for s in self.scene.symbols.values() if s.un_kv is not None},reverse=True)
            from .layout import voltage_color
            entries.extend((f'{v:g} kV',voltage_color(v)) for v in levels)
        elif self.color_mode == 'loading':
            entries.extend([(tr('diagram.load_low'),theme.color('ok')),
                            (tr('diagram.load_mid'),theme.color('warn')),
                            (tr('diagram.load_high'),theme.color('error'))])
        elif self.color_mode == 'voltage_band':
            entries.extend([(tr('diagram.voltage_ok'),theme.color('ok')),
                            (tr('diagram.voltage_bad'),theme.color('error'))])
        entries.extend([(tr('diagram.missing_off'),theme.color('deenergized')),
                        (tr('diagram.deenergized'),theme.color('deenergized'))])
        width=max(200,c.winfo_width())
        from tkinter import font as tkfont
        font=tkfont.Font(c,font=theme.font('small'))
        x,y=8,12
        for label,color in entries:
            needed=font.measure(label)+44
            if x>8 and x+needed>width-8:
                x,y=8,y+24
            dashed = label == tr('diagram.deenergized')
            c.create_line(x,y,x+18,y,fill=color,width=3,dash=(3,2) if dashed else ())
            ident = c.create_text(x+24,y,text=label,anchor='w',font=theme.font('small'),fill=theme.color('text'))
            x=c.bbox(ident)[2]+20
        x,y=8,y+26
        for label in tr('diagram.symbols_full').split('   '):
            needed=font.measure(label)+18
            if x>8 and x+needed>width-8:
                x,y=8,y+24
            c.create_text(x,y,anchor='w',text=label,font=theme.font('small'),fill=theme.color('text_muted'))
            x+=needed
        height=y+14
        if int(c.cget('height'))!=height:
            c.configure(height=height)

    def _draw_minimap(self):
        c = self.canvas
        left, top, right, bottom = self.scene.bounds
        ratio = min(150/max(1,right-left), 90/max(1,bottom-top))
        ox, oy = c.winfo_width()-170, 15
        c.create_rectangle(ox,oy,ox+160,oy+100,fill=theme.color('surface'),outline=theme.color('border'),tags='minimap')
        def point(x,y):
            return ox+5+(x-left)*ratio, oy+5+(y-top)*ratio
        for wire in self.scene.wires:
            c.create_line(*[v for p in wire.points for v in point(*p)],fill=theme.color('branch'),tags='minimap')
        for symbol in self.scene.symbols.values():
            if symbol.kind == 'bus':
                x,y = point(symbol.x,symbol.y)
                c.create_line(x-2,y,x+2,y,fill=theme.color('bus'),tags='minimap')
        x1,y1 = point(-self.offset[0]/(self.scale*self.x_factor),-self.offset[1]/self.scale)
        x2,y2 = point((c.winfo_width()-self.offset[0])/(self.scale*self.x_factor),(c.winfo_height()-self.offset[1])/self.scale)
        c.create_rectangle(x1,y1,x2,y2,outline=theme.color('accent'),tags='minimap')

    def _destroy(self, event):
        if event.widget is self:
            self._release_icons()
            self._toolbox_images.clear()
            for token in self._subscriptions:
                self.bus.unsubscribe(token)
            self._subscriptions.clear()
        super()._destroy(event)
        if event.widget is self:
            self._layer_vars.clear()
            for name, value in tuple(vars(self).items()):
                if isinstance(value, tk.Variable):
                    setattr(self, name, None)
