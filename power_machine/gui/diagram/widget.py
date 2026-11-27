"""Reusable diagram host. Electrical actions are requests to the application."""
from pathlib import Path
import math
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
        self.cursor_t=None
        self.cursor_buses={}
        self.cursor_branches={}
        self._time_corner=None
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
        self._edit_bar = ttk.Frame(self)
        # Editing is grouped with selection tools, not on an isolated third row.
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
            ('time.cursor',self._time_cursor),
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
        for bar in (self._toolbar, self._options_bar, self._edit_bar):
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
        ttk.Button(self._toolbar, textvariable=self.zoom_text, width=7, command=self._actual_size).pack(side='left')
        self._icon_button(self._toolbar,'result_labels',
            lambda:self.set_result_boxes_visible(not self.show_results))
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
        from tkinter import font as tkfont
        font=tkfont.Font(self,font=theme.font('body'))
        choice_width=math.ceil(max(font.measure(label) for label in labels)/max(1,font.measure('0')))+2
        self._choices = ttk.Combobox(self._options_bar, state='readonly', width=choice_width, values=labels)
        self._choices.current(modes.index(self.color_mode))
        self._choices.pack(side='left', padx=4)
        self._choices.bind('<<ComboboxSelected>>', lambda e: self.set_color_mode(modes[self._choices.current()]))
        search = ttk.Entry(self._options_bar, textvariable=self._search_var, width=12)
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
        button._diagram_tooltip = Tooltip(button, tr('v3.diagram.result_labels' if name=='result_labels' else 'diagram.' + name))
        self._icon_buttons[name] = button
        self._refresh_icon(name)
        return button

    def _refresh_icon(self, name):
        button = self._icon_buttons[name]
        button._diagram_image = icon(button, name,
            active=name == self.mode or (name=='result_labels' and self.show_results))
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
        if self.cursor_t is not None or not self.detail_expanded or not symbol or not self.show_results:
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
        if self._pin_close_hit(event):return
        self._hide_tooltip()
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
            self._move_symbols(self.selected_ids,dx,dy)
            self._drag = (key, event.x, event.y, moved or bool(dx or dy))
        else:
            if self._drag:
                key, x, y, moved = self._drag
                dx, dy = event.x-x, event.y-y
                self._pan_pixels(dx,dy)
                self._drag = (key, event.x, event.y, moved or bool(dx or dy))

    def _release(self, event):
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
        self.scene.results = self.results
        self.cursor_t=None
        self.cursor_buses={}
        self.cursor_branches={}
        self.status.set(tr('diagram.stale', reason=reason))
        self.draw()

    def set_layer(self, name, visible):
        self.layers[name] = bool(visible)
        self.draw()
        self.bus.publish('diagram.overlay.changed', mode=name)

    def result_visible(self,symbol):
        # At overview scale, the inspector carries the detailed last calculation.
        if self.is_dense and self.scale<.35 and symbol.id!=self.selected_id:return False
        return super().result_visible(symbol)

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
        self._cursor_styles={}
        self._cursor_handles={}
        self._pin_cache={}
        super().draw()
        if getattr(self,'_status_scale',None)!=self.scale:
            self._set_status()
            self._status_scale=self.scale
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
                self.canvas.create_oval(x-12,y-12,x+12,y+12,outline=color,width=3,tags=('contingency','overlay:'+key))
                if key in self.ranking_ids:
                    self.canvas.create_text(x+14,y-14,text=str(self.ranking_ids.index(key)+1),
                        fill=color,font=theme.font('heading'),anchor='sw',tags=('contingency','ranking','overlay:'+key))
        if hasattr(self, '_footer'):
            self.canvas.delete('legend')
            self._draw_footer()
        elif not self.legend_visible:
            self.canvas.delete('legend')
        if self.minimap_visible:
            self._draw_minimap()
        self._draw_time_cursor()

    def _draw_time_cursor(self):
        c=self.canvas
        c.delete('time_cursor')
        if self.cursor_t is None:
            self._time_corner=None
            return
        from ..results_tables import display_value
        source=getattr(self,'cursor_source','RMS')
        text=(source if len(source)<36 else source[:33]+'…')+' · '+tr('v3.fix.temporal_layer',value=display_value(self.cursor_t,precision=3), count=sum(isinstance(v,dict) and isinstance(v.get('u_pu'),(int,float)) and math.isfinite(v['u_pu']) for v in self.cursor_buses.values()), total=len(self.model.get('buses',[])))
        ident=c.create_text(0,0,text=text,anchor='nw',font=theme.font('small'),width=max(100,c.winfo_width()-40),
            fill=theme.color('ink'),tags='time_cursor')
        box=c.bbox(ident)
        width,height=box[2]-box[0]+12,box[3]-box[1]+8
        size=(c.winfo_width(),c.winfo_height())
        corners=dict(ne=(size[0]-width-10,10),se=(size[0]-width-10,size[1]-height-10),
            nw=(10,10),sw=(10,size[1]-height-10))
        if self._time_corner is None or getattr(self,'_time_view_size',None)!=size:
            def occupied(corner):
                x,y=corners[corner]
                score=0
                for item in c.find_overlapping(x,y,x+width,y+height):
                    tags=c.gettags(item)
                    if 'symbol' in tags or 'pinned_data' in tags or 'minimap' in tags:score+=1000
                    elif 'label' in tags:score+=100
                    elif 'wire' in tags:score+=1
                return score
            self._time_corner=min(corners,key=occupied)
            self._time_view_size=size
        x,y=corners[self._time_corner]
        c.coords(ident,x+6,y+4)
        rect=c.create_rectangle(x,y,x+width,y+height,fill=theme.color('surface'),
            outline=theme.color('border'),tags='time_cursor')
        c.tag_lower(rect,ident)

    def _time_cursor(self,t=None,bus_state=None,branch_state=None,**_):
        """Consume only explicitly supplied, finite time-state values."""
        self.cursor_source=_.get('run_name',getattr(self,'cursor_source','RMS'))
        was_temporal=self.cursor_t is not None
        previous=set(self.index) if was_temporal != (t is not None) else set(self.cursor_buses)|set(self.cursor_branches)
        self.cursor_t=float(t) if isinstance(t,(int,float)) and math.isfinite(t) else None
        self.cursor_buses=dict(bus_state or {}) if self.cursor_t is not None else {}
        self.cursor_branches=dict(branch_state or {}) if self.cursor_t is not None else {}
        # Playback changes colors, not geometry. Keep wheel/pan and labels intact.
        styles=getattr(self,'_cursor_styles',{})
        handles=getattr(self,'_cursor_handles',{})
        for ident in previous|set(self.cursor_buses)|set(self.cursor_branches):
            item=self.index.get(ident)
            if item is None:continue
            symbol=self.scene.symbols.get(ident)
            color=self.element_color(item,self.results.get(ident,{}),symbol.un_kv if symbol else None)
            if self.cursor_t is None and (ident==self.contingency_id or ident in self.ranking_ids):
                color=theme.color('error' if ident==self.contingency_id else 'warn')
            state=self.cursor_buses.get(ident,{}) if self.cursor_t is not None else self.results.get(ident,{})
            off=item.get('outserv') or (isinstance(state,dict) and state.get('energized') is False)
            signature=(color,bool(off))
            if styles.get(ident)==signature:continue
            styles[ident]=signature
            if ident not in handles:
                handles[ident]=[(handle,self.canvas.type(handle),self.canvas.gettags(handle)) for handle in self.canvas.find_withtag('element:'+ident)]
            for handle,kind,tags in handles[ident]:
                if not any(tag in tags for tag in ('symbol','wire','flow_arrow')):continue
                if kind=='line':self.canvas.itemconfigure(handle,fill=color,dash=(5,3) if off else ())
                elif kind=='text':self.canvas.itemconfigure(handle,fill=color)
                elif kind in ('rectangle','polygon','oval'):self.canvas.itemconfigure(handle,outline=color)
        self._cursor_styles,self._cursor_handles=styles,handles
        if was_temporal != (self.cursor_t is not None):
            for tag in ('flow_arrow','result','selected_detail'):
                self.canvas.itemconfigure(tag,state='hidden' if self.cursor_t is not None else 'normal')
        if self.cursor_t is not None:self._detail_panel.pack_forget()
        elif was_temporal:self.draw()  # Restore LF items omitted by a temporal pan/zoom redraw.
        self._draw_time_cursor()
        self._refresh_cards()

    def element_color(self,item,result,un_kv):
        ident=item.get('id')
        state=getattr(self,'cursor_buses',{}).get(ident,{})
        if item.get('outserv') or (isinstance(state,dict) and state.get('energized') is False):
            return theme.color('deenergized')
        value=state.get('u_pu') if isinstance(state,dict) else None
        if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
            low,high=self.voltage_limits
            return theme.color('v_low' if value<low else 'v_high' if value>high else 'v_ok')
        state=getattr(self,'cursor_branches',{}).get(ident,{})
        value=state.get('loading_pct') if isinstance(state,dict) else state
        if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
            return theme.color('load_'+str(min(120,max(0,round(value)))))
        if getattr(self,'cursor_t',None) is not None:
            return theme.color('text_muted')
        return super().element_color(item,result,un_kv)

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
            from ..results_tables import display_value
            entries.extend((display_value(v)+' kV',voltage_color(v)) for v in levels)
        elif self.color_mode == 'loading':
            entries.extend([(tr('diagram.load_low'),theme.color('ok')),
                            (tr('diagram.load_mid'),theme.color('warn')),
                            (tr('diagram.load_high'),theme.color('error'))])
        elif self.color_mode == 'voltage_band':
            entries.extend([(tr('diagram.voltage_ok'),theme.color('ok')),
                            (tr('diagram.voltage_bad'),theme.color('error'))])
        entries.extend([(tr('v3.fix.no_signal'),theme.color('text_muted')),
                        (tr('v3.fix.offline'),theme.color('deenergized'))])
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
        c.delete('minimap')
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
