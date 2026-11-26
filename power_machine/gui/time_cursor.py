"""Transporte temporal y lectura de muestras reales, sin cálculos eléctricos nuevos."""
import math
import time
import weakref
import tkinter as tk
from tkinter import ttk
import numpy as np
from .i18n import tr, language
from .theme import theme


def number(value, digits=3):
    text = f'{float(value):.{digits}f}'
    return text.replace('.', ',') if language() == 'es' else text


class TemporalResult:
    """Instantánea de señales con interpolación local y saltos de evento preservados.

    Un valor ausente no se interpola a través de un hueco. En un tiempo repetido
    se toma la última muestra, que representa el estado posterior al evento.
    La energización solo se publica cuando existe un canal explícito.
    """
    def __init__(self, buffer, model=None):
        self.keys = list(buffer.keys)
        rows = np.asarray(buffer.rows, dtype=float)
        time_key=next((key for key in ('time|b:tnow|s','time_s') if key in self.keys),None)
        if rows.ndim != 2 or len(rows) < 2 or time_key is None:
            raise ValueError('Temporal samples required')
        column = self.keys.index(time_key)
        valid = np.isfinite(rows[:, column])
        rows = rows[valid]
        rows = rows[np.argsort(rows[:, column], kind='stable')]
        self.times = rows[:, column]
        if len(self.times) < 2 or self.times[-1] <= self.times[0]:
            raise ValueError('A temporal interval is required')
        self.start, self.end = float(self.times[0]), float(self.times[-1])
        self.columns = {key: rows[:, i] for i, key in enumerate(self.keys)}
        self.channels = []
        lookup = {}
        for group in ('buses', 'branches', 'branches3'):
            for item in (model or {}).get(group, []):
                for path in (item.get('id'), item.get('object_id'), item.get('path'), item.get('relative_path')):
                    if path:
                        lookup[str(path)] = (str(item['id']), group)
        for key in self.keys:
            if key.count('|') < 2:
                continue
            path, variable, unit = key.rsplit('|', 2)
            if path not in lookup:
                continue
            ident, group = lookup[path]
            if group == 'buses' and variable in ('m:u', 'u_pu','classical:u_pu') and unit == 'p.u.':
                self.channels.append((ident, 'u_pu', key))
            elif group == 'buses' and variable in ('energized', 'm:energized'):
                self.channels.append((ident, 'energized', key))
            elif group != 'buses' and variable in ('c:loading', 'm:loading', 'loading_pct') and unit == '%':
                self.channels.append((ident, 'loading_pct', key))

    def sample(self, key, t, *, discrete=False):
        values = self.columns.get(key)
        if values is None or not math.isfinite(t) or not self.start <= t <= self.end:
            return None
        i = int(np.searchsorted(self.times, t, side='right') - 1)
        first = float(values[i])
        if not math.isfinite(first):
            return None
        if discrete or self.times[i] == t or i == len(self.times)-1:
            return first
        last = float(values[i+1])
        if not math.isfinite(last):
            return None
        return first + (last-first) * (t-self.times[i]) / (self.times[i+1]-self.times[i])

    def state(self, t):
        buses, branches = {}, {}
        for ident, field, key in self.channels:
            value = self.sample(key, t, discrete=field == 'energized')
            if value is None:
                continue
            if field == 'loading_pct':
                branches[ident] = value
            else:
                buses.setdefault(ident, {})[field] = bool(value) if field == 'energized' else value
        return buses, branches


class TimeCursor(ttk.Frame):
    """Cursor compartido por las páginas de Curvas y el unifilar por bus."""
    def __init__(self, parent, app, *, on_ready=None):
        super().__init__(parent, padding=(8, 5), style='Toolbar.TFrame')
        self.app, self.event_bus, self.on_ready = app, app.bus, on_ready
        self.pages = weakref.WeakSet()
        self.data = None
        self.t = 0.
        self.playing = False
        self.source_buffer = getattr(app, 'buffer', None)
        self._signature = None
        self._case = self._case_token()
        self._poll_id = self._tick_id = None
        self.frames = []
        self.time_text = tk.StringVar(value=number(0))
        self.speed = tk.StringVar(value='1×')
        controls=ttk.Frame(self,style='Toolbar.TFrame')
        controls.pack(fill='x')
        self.play_button = ttk.Button(controls, command=self.toggle, style='Toolbar.TButton')
        self.play_button.pack(side='left')
        self.stop_button = ttk.Button(controls, command=self.stop, style='Toolbar.TButton')
        self.stop_button.pack(side='left', padx=4)
        self.scale = ttk.Scale(controls, from_=0, to=1, command=self._slide,style='Horizontal.TScale')
        self.scale.pack(side='left', fill='x', expand=True, padx=8)
        self.entry = ttk.Entry(controls, textvariable=self.time_text, width=8, justify='right')
        self.entry.pack(side='left')
        self.entry.bind('<Return>', self._entered)
        ttk.Label(controls, text='s', style='Toolbar.TLabel').pack(side='left', padx=3)
        self.speed_box = ttk.Combobox(controls, state='readonly', textvariable=self.speed, width=6)
        self.speed_box.pack(side='left', padx=5)
        self.note = ttk.Label(self, style='Toolbar.TLabel')
        self.note.pack(fill='x', pady=(4,0))
        self._tokens = [self.event_bus.subscribe('results.stale', self.invalidate),
                        self.event_bus.subscribe('language.changed', self.retranslate),
                        self.event_bus.subscribe('theme.changed',self._theme_changed)]
        self.bind('<Destroy>', self._dispose, add='+')
        self.retranslate()
        self._controls(False)
        self._theme_changed()
        self._poll_id = self.after(100, self._poll)

    def _theme_changed(self,**_):
        self.scale.configure(style='Horizontal.TScale')

    def _case_token(self):
        doc = getattr(self.app, 'document', None)
        return id(doc), getattr(doc, 'active_case_id', None)

    def attach_page(self, page):
        self.check_case()
        self.pages.add(page)
        page.bind('<Destroy>', lambda event: self.pages.discard(page) if event.widget is page else None, add='+')
        page.time_transport = self
        if hasattr(page, 'enable_time_cursor'):
            page.enable_time_cursor()
        self.use_page(page)
        if self.data is not None:page.set_time_cursor(self.t)

    def use_page(self,page):
        """La página activa determina la corrida publicada al diagrama."""
        if self.source_buffer is not page.buffer:
            self.pause()
            self.source_buffer=page.buffer
        self._read_source()

    def _controls(self, enabled):
        for control in (self.scale, self.entry, self.play_button, self.stop_button, self.speed_box):
            control.state(['!disabled'] if enabled else ['disabled'])
        self.note.configure(text='' if enabled else tr('v3.analysis.no_temporal'))
        if enabled:self.note.pack_forget()
        else:self.note.pack(fill='x',pady=(4,0))

    def _read_source(self):
        buffer = self.source_buffer
        signature = (id(buffer), getattr(buffer, 'version', None))
        if signature == self._signature:
            return
        self._signature = signature
        try:
            data = TemporalResult(buffer, getattr(self.app, 'model', {}))
        except (ValueError, TypeError, AttributeError):
            if self.data is not None:
                self.invalidate()
            return
        self.set_data(data)

    def set_data(self, data):
        self.data = data
        self.scale.configure(to=data.end)
        self._controls(True)
        if self.on_ready:
            self.on_ready()
        self.seek(min(self.t, data.end))

    def invalidate(self, **_):
        self.pause()
        self.data = None
        self._signature = (id(self.source_buffer), getattr(self.source_buffer, 'version', None))
        self._controls(False)
        self.t = 0.
        self.time_text.set(number(0))
        for page in list(self.pages):
            page.set_time_cursor(None)
        self.event_bus.publish('time.cursor', t=None, bus_state={}, branch_state={}, source='analysis')

    def check_case(self):
        current = self._case_token()
        if current != self._case:
            self._case = current
            self.invalidate()
            self.source_buffer = getattr(self.app, 'buffer', None)
            self._signature = (id(self.source_buffer), getattr(self.source_buffer, 'version', None))

    def _poll(self):
        self._poll_id = None
        self.check_case()
        self._read_source()
        self._poll_id = self.after(100, self._poll)

    def seek(self, t):
        if self.data is None:
            return False
        t = float(t)
        if not math.isfinite(t):
            return False
        self.t = max(0., min(self.data.end, t))
        self._updating = True
        self.scale.set(self.t)
        self._updating = False
        self.time_text.set(number(self.t))
        for page in list(self.pages):
            page.set_time_cursor(self.t)
        buses, branches = self.data.state(self.t)
        self.event_bus.publish('time.cursor', t=self.t, bus_state=buses, branch_state=branches, source='analysis')
        return True

    def _slide(self, value):
        if not getattr(self, '_updating', False):
            self.pause()
            self.seek(float(value))

    def _entered(self, _=None):
        try:
            self.pause()
            self.seek(float(self.time_text.get().replace(',', '.')))
        except ValueError:
            self.time_text.set(number(self.t))

    def pause(self):
        self.playing = False
        if self._tick_id:
            self.after_cancel(self._tick_id)
            self._tick_id = None
        self.play_button.configure(text=tr('v3.analysis.play'))

    def toggle(self):
        if self.playing:
            self.pause()
            return
        if self.data is None:
            return
        if self.t >= self.data.end:
            self.seek(0.)
        self.playing = True
        self._clock = time.perf_counter()
        self.play_button.configure(text=tr('v3.analysis.pause'))
        self._tick_id = self.after(1, self._tick)

    def _tick(self):
        self._tick_id = None
        if not self.playing or self.data is None:
            return
        now = time.perf_counter()
        speed = float(self.speed.get().replace('×', '').replace(',', '.'))
        self.seek(self.t + (now-self._clock) * speed)
        self._clock = now
        self.frames.append(time.perf_counter())
        self.frames = self.frames[-1000:]
        if self.t >= self.data.end:
            self.pause()
        else:
            elapsed = (time.perf_counter()-now)*1000
            self._tick_id = self.after(max(1, round(20-elapsed)), self._tick)

    def stop(self):
        self.pause()
        self.seek(0.)

    def retranslate(self, **_):
        self.play_button.configure(text=tr('v3.analysis.pause' if self.playing else 'v3.analysis.play'))
        self.stop_button.configure(text=tr('v3.analysis.stop'))
        self.speed_box.configure(values=tuple((str(v).replace('.', ',') if language()=='es' else str(v))+'×'
                                             for v in (.25, .5, 1, 2, 4)))
        speed=self.speed.get().replace('×','').replace(',','.')
        self.speed.set((speed.replace('.',',') if language()=='es' else speed)+'×')
        self.time_text.set(number(self.t))
        self.note.configure(text='' if self.data else tr('v3.analysis.no_temporal'))

    def _dispose(self, event):
        if event.widget is self:
            self.playing = False
            if self._tick_id:
                self.after_cancel(self._tick_id)
            if self._poll_id:
                self.after_cancel(self._poll_id)
            for token in self._tokens:
                self.event_bus.unsubscribe(token)
