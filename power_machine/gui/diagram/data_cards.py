"""Element-attached result data and one reusable hover card, without calculations."""
from dataclasses import dataclass
import math
import tkinter as tk

from ..i18n import tr
from ..results_tables import display_value
from ..theme import theme
from .layout import clean_label


@dataclass(frozen=True)
class DataRow:
    label: str
    value: str
    token: str = 'text'


@dataclass(frozen=True)
class ElementData:
    title: str
    subtitle: str
    note: str
    rows: tuple[DataRow, ...]


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def element_data(view, key):
    """Read the scene's last calculation or explicitly supplied cursor signals.

    Missing cursor signals stay missing. Steady-state values are never presented
    as instantaneous values, and setpoints are never substituted for results.
    """
    symbol = view.scene.symbols.get(key)
    item = view.index.get(key, symbol.item if symbol else {})
    identity = item.get('id',key)
    kind = symbol.kind if symbol else 'branch'
    group = item.get('group')
    kind = dict(buses='bus', branches='branch', branches3='transformer',
                loads='load', gens='generator', shunts='shunt', switches='switch').get(group, kind)
    kind = kind if kind in ('bus', 'branch', 'transformer', 'generator', 'load', 'shunt', 'switch', 'group') else 'element'
    t = getattr(view, 'cursor_t', None)
    result = getattr(view.scene, 'results', view.results).get(identity, {})
    if t is not None:
        state = getattr(view, 'cursor_buses', {}).get(identity,
                    getattr(view, 'cursor_branches', {}).get(identity, {}))
        result = dict(state) if isinstance(state, dict) else {'loading_pct': state}
        nominal=item.get('un_kv',getattr(symbol,'un_kv',None))
        if kind=='bus' and not finite(result.get('u_kv')) and finite(result.get('u_pu')) and finite(nominal) and nominal>0:
            result['u_kv']=result['u_pu']*nominal
        note = tr('v3.diagram.time', value=display_value(t, precision=3))
        note += ' · ' + tr('v3.fix.temporal_missing')
    else:
        note = tr('v3.fix.last_lf') if result else tr('v3.diagram.no_results')
    rows = []
    def add(label, field, unit, precision=2):
        value = result.get(field)
        token = 'text'
        if finite(value):
            text = display_value(value, field, precision=precision) + ' ' + unit
            if field == 'u_pu':
                low, high = view.voltage_limits
                token = 'v_low' if value < low else 'v_high' if value > high else 'v_ok'
            elif field.startswith('loading'):
                token = 'load_' + str(min(120, max(0, round(value))))
                if value>getattr(view,'loading_bands',(80,100))[1]:label+=' !'
        else:
            text, token = '— ' + unit, 'text_muted'
        rows.append(DataRow(label, text, token))
    if kind == 'bus':
        add('U', 'u_pu', 'p.u.', 4)
        add('U', 'u_kv', 'kV', 2)
        add(tr('v3.diagram.angle'), 'angle_deg', '°', 2)
        value=result.get('u_pu')
        if finite(value):
            low,high=view.voltage_limits
            if value<low or value>high:
                limit=low if value<low else high
                rows.append(DataRow(tr('v3.fix.voltage_limit'),
                    display_value(value,precision=6)+(' < ' if value<low else ' > ')+display_value(limit,precision=6)+' p.u.', 'error'))
    elif kind in ('branch', 'transformer', 'switch'):
        ends = [end for end in ('from', 'to', 'hv', 'mv', 'lv')
                if 'bus_' + end in item or 'bus_' + end in result or
                any(f'{field}_{end}_{unit}' in result for field, unit in (('p','mw'),('q','mvar'),('i','ka'),('loading','pct')))]
        for end in ends or ('from', 'to'):
            bus = result.get('bus_' + end, item.get('bus_' + end))
            end_name = tr('v3.diagram.end.' + end)
            rows.append(DataRow(end_name, clean_label(view.index.get(bus, {}).get('name', bus or '')), 'text_muted'))
            add('P', f'p_{end}_mw', 'MW')
            add('Q', f'q_{end}_mvar', 'Mvar')
            add('I', f'i_{end}_ka', 'kA', 3)
            add(tr('v3.diagram.loading'), f'loading_{end}_pct', '%', 1)
        if 'loading_pct' in result:
            add(tr('v3.diagram.loading'), 'loading_pct', '%', 1)
    elif kind in ('generator', 'load', 'shunt'):
        add('P', 'p_mw', 'MW')
        add('Q', 'q_mvar', 'Mvar')
        if kind == 'generator':
            # A solved terminal voltage is valid, a model voltage setpoint is not.
            if t is None and 'u_pu' not in result:
                terminal = view.results.get(item.get('bus'), {})
                result = dict(result, **{k: terminal[k] for k in ('u_pu', 'u_kv') if k in terminal})
            add('U', 'u_pu', 'p.u.', 4)
    return ElementData(clean_label(item.get('name', key)),
        clean_label(key) + ' · ' + tr('v3.diagram.type.' + kind), note, tuple(rows))


class DataCard(tk.Frame):
    """A small reusable card. It is a Canvas child, so native captures include it."""
    def __init__(self, parent):
        super().__init__(parent, borderwidth=0, highlightthickness=1, padx=10, pady=7)
        self.labels = []
        self._recolor_job=None
        self.bind('<Map>',self._schedule_colors)
        self.bind('<Destroy>',self._dispose)

    def _schedule_colors(self,event=None):
        # The shared Map hook normalizes native labels. Restore the explicit
        # diagram tokens afterwards, including voltage/loading violation colors.
        if self._recolor_job is None:
            self._recolor_job=self.after_idle(self._paint_colors)

    def _paint_colors(self):
        self._recolor_job=None
        self.configure(background=theme.color('surface'),highlightbackground=theme.color('border'))
        for pair in self.labels:
            for label in pair:
                label.configure(background=theme.color('surface'),
                    foreground=theme.color(getattr(label,'_card_token','text')))

    def _dispose(self,event):
        if event.widget is self and self._recolor_job:
            self.after_cancel(self._recolor_job)
            self._recolor_job=None

    def set_data(self, data):
        self.configure(background=theme.color('surface'), highlightbackground=theme.color('border'))
        contents = [(data.title, '', 'text', 'heading'),
                    (data.subtitle, '', 'text_muted', 'small'),
                    (data.note, '', 'text_muted', 'small')]
        contents += [(r.label, r.value, r.token, 'small') for r in data.rows]
        while len(self.labels) < len(contents):
            row = len(self.labels)
            pair = [tk.Label(self, anchor='w'), tk.Label(self, anchor='e')]
            for label in pair:label.bind('<Map>',self._schedule_colors)
            pair[0].grid(row=row, column=0, sticky='w', padx=(0,16))
            pair[1].grid(row=row, column=1, sticky='e')
            self.labels.append(pair)
        for i, pair in enumerate(self.labels):
            for label in pair:
                label.grid_remove()
            if i >= len(contents):
                continue
            left, right, token, font = contents[i]
            for label, text in zip(pair, (left,right)):
                label._card_token=token
                label.configure(text=text, background=theme.color('surface'),
                    foreground=theme.color(token), font=theme.font(font),
                    wraplength=320 if i<3 else 0,justify='left')
                label.grid()
            pair[0].grid_configure(columnspan=2 if not right else 1)
            if not right:
                pair[1].grid_remove()


class ElementCards:
    """Small UI state, independent of network history and electrical model."""
    def _init_cards(self):
        self.pinned_data = {}
        self._hover_job = None
        self._tooltip = None
        self._tooltip_key = None
        self._hover_id = None

    def _hide_tooltip(self):
        if self._hover_job:
            self.after_cancel(self._hover_job)
            self._hover_job = None
        if self._tooltip is not None:
            try:
                self._tooltip.place_forget()
            except tk.TclError:
                pass  # Tk destroys child widgets before delivering our Destroy.
        self._tooltip_key = self._hover_id = None

    def show_element_tooltip(self, key, x, y):
        """Show the single hover widget, also useful for deterministic UI checks."""
        self._hover_job = None
        if key not in self.index and key not in self.scene.symbols:
            return
        if self._tooltip is None:
            self._tooltip = DataCard(self.canvas)
        self._tooltip_key = self._hover_id = key
        self._tooltip.set_data(element_data(self, key))
        self._tooltip.update_idletasks()
        width, height = self._tooltip.winfo_reqwidth(), self._tooltip.winfo_reqheight()
        x = max(4, min(x + 18, self.canvas.winfo_width() - width - 6))
        y = max(4, min(y + 18, self.canvas.winfo_height() - height - 6))
        self._tooltip.place(x=round(x), y=round(y))
        self._tooltip.lift()

    def _hover(self, event):
        if self._drag:
            return
        key = self._hit(event)
        if key != self._hover_id:
            self._hide_tooltip()
            self._hover_id = key
            if key in self.index or key in self.scene.symbols:
                self._hover_job = self.after(300, lambda: self.show_element_tooltip(key,event.x,event.y))

    def pin_data(self, key):
        if key not in self.index and key not in self.scene.symbols:
            return
        if key not in self.pinned_data and len(self.pinned_data) >= 20:
            self.status.set(tr('v3.diagram.pin_limit', count=20))
            return
        if key not in self.pinned_data:
            anchor=self._element_anchor(key)
            if anchor:
                x,y=self._xy(*anchor)
                data=element_data(self,key)
                dx=24 if x+260<self.canvas.winfo_width() else -260
                height=75+len(data.rows)*20
                dy=18 if y+height+18<self.canvas.winfo_height() else -height-18
                self.pinned_data[key]=(dx/(self.scale*self.x_factor),dy/self.scale)
            else:
                self.pinned_data[key]=(24/self.scale,18/self.scale)
        self._hide_tooltip()
        self._draw_pins()

    def unpin_data(self, key):
        self.pinned_data.pop(key, None)
        getattr(self,'_pin_cache',{}).pop(key,None)
        self.canvas.delete('pin:' + key)

    def _pin_close_hit(self, event):
        for handle in self.canvas.find_overlapping(event.x-2,event.y-2,event.x+2,event.y+2):
            for tag in self.canvas.gettags(handle):
                if tag.startswith('pin_close:'):
                    self.unpin_data(tag[len('pin_close:'):])
                    return True
        return False

    def _element_anchor(self, key):
        symbol = self.scene.symbols.get(key)
        if symbol:
            return symbol.x, symbol.y
        wire = next((w for w in self.scene.wires if w.id == key), None)
        if wire and wire.points:
            a, b = wire.points[0], wire.points[-1]
            return (a[0]+b[0])/2, (a[1]+b[1])/2

    def _draw_pins(self):
        c = self.canvas
        cache=getattr(self,'_pin_cache',{})
        for key in set(cache)-set(self.pinned_data):
            c.delete('pin:'+key)
            cache.pop(key,None)
        self._pin_cache=cache
        for key, (dx,dy) in self.pinned_data.items():
            anchor = self._element_anchor(key)
            if anchor is None:
                continue
            ax, ay = self._xy(*anchor)
            x, y = self._xy(anchor[0]+dx, anchor[1]+dy)
            data = element_data(self, key)
            tags = ('pinned_data', 'pin:' + key, 'data_for:' + key)
            texts = [(data.title,'text','heading'),(data.subtitle,'text_muted','small'),
                     (data.note,'text_muted','small')]
            texts += [(r.label + '  ' + r.value,r.token,'small') for r in data.rows]
            cached=cache.get(key)
            geometry=(ax,ay,x,y,self.scale)
            if cached and len(cached['texts'])==len(texts):
                if cached['geometry']!=geometry:
                    c.coords(cached['connector'],ax,ay,x,y)
                    c.coords(cached['rectangle'],x,y,x+cached['size'][0],y+cached['size'][1])
                    c.coords(cached['close'],x+cached['size'][0]-12,y+12)
                    for handle,(ox,oy) in zip(cached['handles'],cached['offsets']):c.coords(handle,x+ox,y+oy)
                    cached['geometry']=geometry
                commands=[]
                for handle,old,new in zip(cached['handles'],cached['texts'],texts):
                    if old!=new:commands.extend([(c._w,'itemconfigure',handle,'-text',new[0],'-fill',theme.color(new[1])),';'])
                if commands:c.tk.call('eval',*commands)
                cached['texts']=texts
                continue
            c.delete('pin:'+key)
            connector=c.create_line(ax,ay,x,y,fill=theme.color('accent'),dash=(2,3),tags=tags)
            text_handles=[]
            top, right = y+8, x+150
            for text, token, font in texts:
                handle = c.create_text(x+10,top,text=text,anchor='nw',fill=theme.color(token),
                    font=theme.font(font),width=300,tags=tags)
                text_handles.append(handle)
                box = c.bbox(handle)
                top, right = box[3]+3, max(right,box[2]+28)
            rect = c.create_rectangle(x,y,right,top+5,fill=theme.color('surface'),
                outline=theme.color('accent'),tags=tags)
            c.tag_lower(rect, tags[1])
            close = c.create_text(right-12,y+12,text='×',font=theme.font('heading'),
                fill=theme.color('text_muted'),tags=tags+('pin_close:'+key,))
            # Fit newly created/resized cards using their measured bounds. Keep
            # their element anchor fixed, and leave room for the source badge.
            top_limit,bottom_limit=4,c.winfo_height()-4
            badge=c.bbox('time_cursor')
            if badge:
                if badge[1]>c.winfo_height()/2:bottom_limit=min(bottom_limit,badge[1]-4)
                else:top_limit=max(top_limit,badge[3]+4)
            width,height=right-x,top+5-y
            nx=max(4,min(x,c.winfo_width()-width-4))
            ny=max(top_limit,min(y,bottom_limit-height))
            sx,sy=nx-x,ny-y
            if sx or sy:
                c.move(tags[1],sx,sy)
                c.coords(connector,ax,ay,nx,ny)
                self.pinned_data[key]=(dx+sx/(self.scale*self.x_factor),dy+sy/self.scale)
                x,y,right,top=nx,ny,right+sx,top+sy
                geometry=(ax,ay,x,y,self.scale)
            cache[key]=dict(geometry=geometry,texts=texts,handles=text_handles,connector=connector,rectangle=rect,close=close,
                size=(right-x,top+5-y),offsets=[(c.coords(h)[0]-x,c.coords(h)[1]-y) for h in text_handles])
            c.tag_bind(close,'<Enter>',lambda e:c.configure(cursor='hand2'))
            c.tag_bind(close,'<Leave>',lambda e:c.configure(cursor=''))

    def _refresh_cards(self):
        self._draw_pins()
        if self._tooltip_key and self._tooltip is not None:
            self._tooltip.set_data(element_data(self, self._tooltip_key))

    def view_properties(self, key):
        if hasattr(self, 'select_ids'):
            self.select_ids([key])
        else:
            self.highlight(key)
            self.callbacks.on_select(key)

    def add_context_data(self, menu, key):
        card = DataCard(menu.body)
        card.set_data(element_data(self,key))
        card.pack(fill='x',pady=(0,4))
        menu.add_command(label=tr('v3.diagram.pin'),command=lambda:self.pin_data(key))
        if key in self.pinned_data:menu.add_command(label=tr('v3.fix.unpin'),command=lambda:self.unpin_data(key))
        menu.add_command(label=tr('v3.diagram.properties'),command=lambda:self.view_properties(key))
        menu.add_separator()
