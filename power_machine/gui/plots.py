"""Gráficos RMS con reducción visual que conserva extremos y exportación completa."""
import csv
import math
import re
import time
import tkinter as tk
from tkinter import ttk, filedialog
import numpy as np
from .presentation import element_label, station
from .theme import theme
from .bus import bus
from .i18n import tr
from .results_tables import bind_translations


TIME_KEY = 'time|b:tnow|s'


def curve_label(curve):
    """Rótulo breve que conserva ubicación de barras y extremo de ramas."""
    path, variable, unit = curve['key'].rsplit('|', 2)
    parts = curve.get('label', path.replace('\\', '/').rsplit('/', 1)[-1]).split(' · ')
    name = parts[0].split('.Elm', 1)[0]
    if len(parts) >= 3:
        if name.isdigit() or name.casefold().startswith('terminal'):
            location = re.sub(r'^S/E\s*', '', parts[1], flags=re.I)
            location = re.sub(r'\s+\d+(?:[.,]\d+)?\s*kV(?:\(\d+\))?$', '', location, flags=re.I)
            if location.isupper():
                location = location.title()
            name = location + ' ' + name
        if parts[2] != 'kV no disponible' and parts[2] not in name:
            name += ' ' + parts[2]
    symbols = {'m:u': 'U', 'm:fehz': 'f', 'm:phiu': 'φ', 's:xspeed': 'ω', 's:speed': tr('plots.speed'),
               'classical:u_pu':'U','classical:omega_pu':'ω','classical:delta_deg':'δ',
               'classical:f_hz':'f','classical:coi_frequency':'f'}
    symbol = symbols.get(variable, variable.split(':', 1)[-1])
    if variable in ('m:P:bus1', 'm:Q:bus1'):
        symbol = variable.split(':')[1]
    return name + ' · ' + symbol


def validate_plot_specs(specs, known_keys):
    """Comprueba el formato persistido antes de reemplazar el documento abierto."""
    if not isinstance(specs, list):
        raise ValueError('Las páginas de gráficos deben ser una lista')
    for spec in specs:
        if not isinstance(spec, dict) or not isinstance(spec.get('name', ''), str):
            raise ValueError('Página de gráficos inválida')
        from .presentation_specs import _json
        _json(spec)
        if 'schema' in spec and (spec['schema'] != 'power_machine-plot-page-v1' or
                                 type(spec.get('version')) is not int or spec['version'] != 1):
            raise ValueError('Versión de página desconocida')
        count = spec.get('axes', 1)
        if 'axis_specs' in spec:
            from .presentation_specs import validate_template
            validate_template(dict(version=1, name=spec.get('name'), axes=count,
                                   axis_specs=spec['axis_specs'], signals=[], events=spec.get('events', [])))
        if 'template' in spec:
            from .presentation_specs import validate_template
            validate_template(spec['template'])
        if type(count) is not int or not 1 <= count <= 6:
            raise ValueError('Cada página admite entre 1 y 6 subgráficos')
        curves = spec.get('curves', [])
        if not isinstance(curves, list):
            raise ValueError('Las curvas deben ser una lista')
        units = {}
        for curve in curves:
            if not isinstance(curve, dict) or curve.get('key') not in known_keys:
                raise ValueError('Variable desconocida en una página de gráficos')
            axis = curve.get('axis', 0)
            if type(axis) is not int or not 0 <= axis < count:
                raise ValueError('Subgráfico de destino fuera de rango')
            unit = curve['key'].rsplit('|', 1)[-1]
            if axis in units and units[axis] != unit:
                raise ValueError('Use subgráficos distintos para unidades diferentes')
            units[axis] = unit
            if 'axis_specs' in spec and spec['axis_specs'][axis]['unit'] != unit:
                raise ValueError('Unidad de señal incompatible con eje')
            if curve.get('line_style', '-') not in ('-', '--', '-.', ':'):
                raise ValueError('Estilo de línea inválido')
            if curve.get('marker', '') not in ('', 'o', 's', '^', '.', 'x', '+'):
                raise ValueError('Marcador inválido')
            width = curve.get('line_width', 1.15)
            if type(width) not in (int, float) or not math.isfinite(width) or width <= 0:
                raise ValueError('Grosor de línea inválido')
        if spec.get('frequency_mode', 'absolute') not in ('absolute', 'deviation'):
            raise ValueError('Modo de frecuencia inválido')
        base_hz = spec.get('frequency_base_hz', 50.0)
        if not isinstance(base_hz, (int, float)) or not math.isfinite(float(base_hz)):
            raise ValueError('Base de frecuencia inválida')
        for marker in spec.get('events', []):
            if (not isinstance(marker, dict) or not isinstance(marker.get('time_s'), (int, float))
                    or not math.isfinite(float(marker['time_s']))):
                raise ValueError('Marcador de evento inválido')
        scales = spec.get('axis_scales', {})
        if not isinstance(scales, dict):
            raise ValueError('Escalas manuales inválidas')
        for axis in scales:
            try:
                scale_axis = int(axis)
            except (TypeError, ValueError) as exc:
                raise ValueError('Subgráfico de escala inválido') from exc
            if not 0 <= scale_axis < count:
                raise ValueError('Subgráfico de escala fuera de rango')
            axis_scale(spec, axis)


def decimate(x, y, max_points=2400):
    """Conserva extremos locales y huecos, sin modificar las muestras exportadas."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if max_points < 8:
        raise ValueError('El límite de puntos debe ser al menos 8')
    if len(x) <= max_points:
        return x, y
    selected = {0, len(x) - 1}
    bins = max(1, (max_points - 2) // 4)
    for indexes in np.array_split(np.arange(len(x)), bins):
        values = y[indexes]
        valid = np.isfinite(values)
        selected.update((int(indexes[0]), int(indexes[-1])))
        if valid.any():
            finite_indexes = indexes[valid]
            selected.update((int(finite_indexes[np.argmin(y[finite_indexes])]), int(finite_indexes[np.argmax(y[finite_indexes])])))
        # No unir tramos a través de valores ausentes.
        for index in indexes[~valid]:
            selected.update(i for i in (int(index)-1, int(index), int(index)+1) if 0 <= i < len(x))
    indexes = sorted(selected)
    return x[indexes], y[indexes]


def series_statistics(x, y):
    """Resume una curva sin convertir vacíos en ceros."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    if not valid.any():
        return {'minimum': math.nan, 'maximum': math.nan, 'final': math.nan, 'samples': 0}
    values = y[valid]
    return {'minimum': float(np.min(values)), 'maximum': float(np.max(values)),
            'final': float(values[-1]), 'samples': int(values.size)}


def nearest_sample(x, y, time_s):
    """Devuelve la muestra finita más próxima al tiempo solicitado."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    valid = np.isfinite(x) & np.isfinite(y)
    if not valid.any() or not math.isfinite(float(time_s)):
        return None
    valid_x, valid_y = x[valid], y[valid]
    index = int(np.argmin(np.abs(valid_x - float(time_s))))
    return float(valid_x[index]), float(valid_y[index])


def cursor_difference(first, second):
    """Entrega diferencias de cursor con unidades explícitas."""
    if first is None or second is None:
        return None
    return {'delta_time_s': float(second['time_s']) - float(first['time_s']),
            'delta_values': {key: float(second['values'][key]) - float(first['values'][key])
                             for key in first['values'].keys() & second['values'].keys()
                             if math.isfinite(first['values'][key]) and math.isfinite(second['values'][key])}}


def xy_series(first_buffer, x_key, y_key):
    """Prepara un gráfico X-Y, conservando solo pares de muestras finitas."""
    tx, x = first_buffer.series(x_key)
    ty, y = first_buffer.series(y_key)
    if len(tx) != len(ty) or not np.array_equal(tx, ty):
        raise ValueError('Las señales X e Y deben compartir la misma base temporal')
    valid = np.isfinite(x) & np.isfinite(y)
    return x[valid], y[valid]


def xy_figure(first_buffer, x_key, y_key):
    """Construye una figura X-Y monocromática con escalado automático."""
    from matplotlib.figure import Figure
    x, y = xy_series(first_buffer, x_key, y_key)
    figure = Figure(figsize=(7, 5), dpi=100, facecolor=theme.color('surface'), layout='constrained')
    axis = figure.subplots()
    axis.plot(x, y, color=theme.color('text'), linestyle='-', linewidth=1.15)
    axis.grid(True, color=theme.color('grid'), linewidth=.6)
    axis.set_xlabel(x_key.rsplit('|', 2)[1] + ' [' + x_key.rsplit('|', 1)[1] + ']')
    axis.set_ylabel(y_key.rsplit('|', 2)[1] + ' [' + y_key.rsplit('|', 1)[1] + ']')
    return figure


def reference_availability(reference, keys):
    """Indica si la referencia contiene al menos una curva seleccionada."""
    if reference is None:
        return False, 'No se cargó una curva de referencia externa'
    available = set(getattr(reference, 'keys', ()))
    missing = [key for key in keys if key not in available]
    if len(missing) == len(keys):
        return False, 'La referencia externa no contiene las señales de esta página'
    if missing:
        return True, 'La referencia externa no contiene todas las señales de esta página'
    return True, ''


def display_series(key, x, y, frequency_mode='absolute', frequency_base_hz=50.0):
    """Aplica solo la presentación elegida para frecuencia, sin alterar los datos."""
    if frequency_mode not in ('absolute', 'deviation'):
        raise ValueError('Modo de frecuencia inválido')
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    if key.rsplit('|', 2)[1] != 'm:fehz' or frequency_mode == 'absolute':
        return x, y, key.rsplit('|', 1)[-1]
    return x, y - float(frequency_base_hz), 'Hz de desviación'


def break_at_events(x, y, events=()):
    """Separa pre y posevento, incluso tiempos duplicados, sin interpolación."""
    x, y = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    times = [e['time_s'] for e in events]
    cuts = [i for i in range(1, len(x)) if x[i] <= x[i-1] or
            any(x[i-1] < t <= x[i] for t in times)]
    return np.insert(x, cuts, np.nan), np.insert(y, cuts, np.nan)


def axis_scale(spec, axis):
    """Lee una escala manual opcional sin aceptar límites degenerados."""
    index = int(axis)
    scale = spec.get('axis_scales', {}).get(str(index), spec.get('axis_scales', {}).get(index))
    if not scale:
        return None, None
    def limits(name):
        value = scale.get(name)
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise ValueError('Escala manual inválida')
        start, end = (float(value[0]), float(value[1]))
        if not math.isfinite(start) or not math.isfinite(end) or start >= end:
            raise ValueError('Los límites manuales deben ser finitos y crecientes')
        return start, end
    return limits('xlim_s') if 'xlim_s' in scale else None, limits('ylim') if 'ylim' in scale else None


class SignalBuffer:
    def __init__(self):
        self.keys = []
        self.rows = []
        self.version = 0
        self.generation = 0

    def reset(self, keys):
        self.keys = list(keys)
        self.rows.clear()
        self.generation += 1
        self.version += 1

    def append(self, rows):
        for row in rows:
            if len(row) != len(self.keys):
                raise ValueError('Cantidad de columnas de señales incompatible')
            self.rows.append([float(value) if value is not None and value != '' else math.nan for value in row])
        self.version += 1

    def series(self, key):
        if key not in self.keys or not self.rows:
            return np.array([]), np.array([])
        t = self.keys.index(TIME_KEY) if TIME_KEY in self.keys else 0
        k = self.keys.index(key)
        return np.fromiter((row[t] for row in self.rows), float), np.fromiter((row[k] for row in self.rows), float)

    def export(self, path, keys):
        keys = list(dict.fromkeys(key for key in keys if key in self.keys))
        time_key = TIME_KEY if TIME_KEY in self.keys else self.keys[0] if self.keys else TIME_KEY
        columns = list(dict.fromkeys([time_key] + keys))
        indices = [self.keys.index(key) for key in columns] if self.keys else []
        with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(columns)
            for row in self.rows:
                writer.writerow([row[i] if math.isfinite(row[i]) else '' for i in indices])


class RefreshGate:
    def __init__(self, interval=.25):
        self.interval = interval
        self.last = -math.inf

    def ready(self, now=None):
        now = time.monotonic() if now is None else now
        if now - self.last < self.interval:
            return False
        self.last = now
        return True


def export_visible(path, buffer, keys, reference=None, source_label='Modelo', reference_label='REF', overlays=()):
    """CSV largo, sin interpolar ni alinear artificialmente los tiempos de REF."""
    keys = list(dict.fromkeys(keys))
    with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['Fuente', tr('plots.time_axis'), 'Señal', 'Valor', tr('plots.unit')])
        for source, samples in [(source_label, buffer), (reference_label, reference), *overlays]:
            if samples is None:
                continue
            for key in keys:
                x, y = samples.series(key)
                unit = key.rsplit('|', 1)[-1]
                writer.writerows([source, t, key, value if math.isfinite(value) else '', unit] for t, value in zip(x, y))


class PlotPage(ttk.Frame):
    """Página RMS con comparación, lectura y exportación sin alterar el motor."""
    _LINE_STYLES = ('-', '--', '-.', ':')

    def __init__(self, master, spec, buffer, reference=None, on_reference_toggle=None, source_label='Modelo', reference_label='REF'):
        super().__init__(master, padding=6)
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg, NavigationToolbar2Tk
        class ToolbarToggle(ttk.Checkbutton):
            def select(button):button.var.set(1)
            def deselect(button):button.var.set(0)

        class SpanishToolbar(NavigationToolbar2Tk):
            toolitems = tuple((text, {
                'Home': tr('plots.home'), 'Back': tr('plots.back'), 'Forward': tr('plots.forward'),
                'Pan': tr('plots.pan'), 'Zoom': tr('plots.zoom'), 'Save': tr('plots.save_image'),
            }.get(text, tooltip), icon, callback) for text, tooltip, icon, callback in NavigationToolbar2Tk.toolitems if text != 'Subplots')

            def set_message(toolbar, text):
                for english, key in (('pan/zoom', 'plots.pan'), ('zoom rect', 'plots.zoom')):
                    text = str(text).replace(english, tr(key))
                super().set_message(text.replace(';', ','))

            def save_figure(toolbar, *args):
                self.export_png()

            def _Button(toolbar,text,image_file,toggle,command):
                if toggle:
                    variable=tk.IntVar(toolbar)
                    button=ToolbarToggle(toolbar,text=text,command=command,variable=variable,style='PlotNav.Toolbutton')
                    button.var=variable
                else:
                    button=ttk.Button(toolbar,text=text,command=command,style='PlotNav.TButton')
                button._image_file=image_file
                toolbar._set_image_for_button(button)
                button.pack(side='left')
                return button

            def _set_image_for_button(toolbar,button):
                from PIL import Image,ImageTk
                with Image.open(button._image_file) as original:
                    pixels=np.asarray(original.convert('RGBA')).copy()
                images=[]
                size=18
                for token in ('text','text_muted'):
                    pixels[:,:,:3]=tuple(channel//257 for channel in button.winfo_rgb(theme.color(token)))
                    images.append(ImageTk.PhotoImage(Image.fromarray(pixels).resize((size,size)),master=button))
                button._nav_images=images
                button.configure(image=(images[0],'disabled',images[1]))
        self.spec, self.buffer, self.reference = spec, buffer, reference
        self.source_label, self.reference_label = source_label, reference_label
        self._on_reference_toggle = on_reference_toggle
        self._cursor_points = []
        self._time_enabled = False
        self._time_value = None
        self._time_artists = []
        self._time_background = None
        self._time_connections = []
        self._time_samples = {}
        self._time_verticals = []
        self._time_markers = []
        self._time_regions = []
        self._toolbar_skin_id = None
        self.overlays = {}
        count = max(1, min(6, int(spec.get('axes', 1))))
        self.figure = Figure(figsize=(9, 6), dpi=100, facecolor=theme.color('surface'), layout='constrained')
        self.axes = self.figure.subplots(count, 1, sharex=True, squeeze=False).ravel().tolist()
        self.canvas = FigureCanvasTkAgg(self.figure, master=self)
        bar = ttk.Frame(self)
        bar.pack(side='top',fill='x')
        self.controls=bar
        navigation=bar
        self.toolbar = SpanishToolbar(self.canvas, navigation, pack_toolbar=False)
        self.toolbar.pack(side='left')
        self.toolbar.bind('<Map>',self._queue_toolbar_skin,add='+')
        for button in self.toolbar._buttons.values():button.bind('<Map>',self._queue_toolbar_skin,add='+')
        self.export_button=ttk.Menubutton(bar,text=tr('v3.int.export'),style='TMenubutton',width=0)
        self.export_menu=tk.Menu(self.export_button,tearoff=False)
        for kind in ('svg','png','pdf','csv'):
            self.export_menu.add_command(label=kind.upper(),command=getattr(self,'export_'+kind))
        self.export_button.configure(menu=self.export_menu)
        self.export_button.pack(side='right',padx=3)
        self.overlay_button=ttk.Button(bar,text=tr('v3.int.overlay'),command=self.import_run_csv,style='Toolbar.TButton')
        self.overlay_button.pack(side='right',padx=3)
        if 'Save' in self.toolbar._buttons:self.toolbar._buttons['Save'].pack_forget()
        if hasattr(self.toolbar,'_message_label'):self.toolbar._message_label.pack_forget()
        self.frequency_mode = tk.StringVar(value=spec.get('frequency_mode', 'absolute'))
        self.frequency_display = tk.StringVar(value=tr('plots.frequency_' + self.frequency_mode.get()))
        self.frequency_combo = ttk.Combobox(bar, width=14, state='readonly', textvariable=self.frequency_display,
                     values=(tr('plots.frequency_absolute'), tr('plots.frequency_deviation')))
        self.frequency_combo.pack(side='right', padx=3)
        self.frequency_combo.bind('<<ComboboxSelected>>', lambda event: self.frequency_mode.set(
            ('absolute', 'deviation')[self.frequency_combo.current()]))
        self.frequency_label = ttk.Label(bar, text=tr('plots.frequency'))
        self.frequency_label.pack(side='right')
        self.show_ref = tk.BooleanVar(value=spec.get('show_ref', True))
        self.reference_toggle = ttk.Checkbutton(bar, text=tr('plots.ref_reference') if reference_label == 'REF' else tr('plots.compare_run'),
                                                variable=self.show_ref, command=self.toggle_reference)
        self.reference_toggle.pack(side='right', padx=3)
        self.reference_note = tk.StringVar()
        self.reference_note_label = ttk.Label(bar, textvariable=self.reference_note, style='Muted.TLabel',width=16)
        from .app_shell import Tooltip
        Tooltip(self.reference_note_label,self.reference_note.get)
        self.reference_note_label.pack(side='right', padx=4)
        self.cursor = tk.StringVar(value=tr('plots.cursor_help'))
        self.statistics = tk.StringVar(value='')
        if source_label != 'Modelo':
            ttk.Label(self, text=tr('plots.first_run', label=source_label) + '\n' + tr('plots.second_run', label=reference_label) if reference else tr('plots.first_run', label=source_label), style='Muted.TLabel').pack(anchor='w')
        self.cursor_label = ttk.Label(self, textvariable=self.cursor, style='Muted.TLabel')
        self.cursor_label.pack(anchor='w')
        self.statistics_label = ttk.Label(self, textvariable=self.statistics, style='Muted.TLabel', wraplength=1100)
        self.statistics_label.pack(anchor='w')
        self.canvas.get_tk_widget().pack(fill='both', expand=True)
        self.canvas.mpl_connect('motion_notify_event', self.motion)
        self.canvas.mpl_connect('button_press_event', self.capture_cursor)
        self.canvas.mpl_connect('resize_event', lambda _event: self.canvas.draw_idle())
        self.canvas.mpl_connect('draw_event', self._cursor_drawn)
        self.frequency_mode.trace_add('write', lambda *_: self.retranslate())
        self.gate = RefreshGate()
        self.last_version = -1
        self._theme_subscription = None
        self._theme_changed()
        self._theme_subscription = bus.subscribe('theme.changed', self._theme_changed)
        self.bind('<Destroy>', self._dispose, add='+')
        bind_translations(self)

    def retranslate(self):
        self.export_button.configure(text=tr('v3.int.export'))
        self.overlay_button.configure(text=tr('v3.int.overlay'))
        self.frequency_combo.configure(values=(tr('plots.frequency_absolute'), tr('plots.frequency_deviation')))
        self.frequency_display.set(tr('plots.frequency_' + self.frequency_mode.get()))
        self.redraw(force=True)

    def _theme_changed(self, **_):
        style=ttk.Style(self)
        for name in ('PlotNav.TButton','PlotNav.Toolbutton'):
            style.configure(name,background=theme.color('surface'),foreground=theme.color('text'),padding=2,borderwidth=0)
            style.map(name,background=[('selected',theme.color('selection')),('active',theme.color('selection'))],
                      foreground=[('disabled',theme.color('text_muted'))])
        self.toolbar.configure(background=theme.color('surface'))
        for child in self.toolbar.winfo_children():
            try:
                child.configure(background=theme.color('surface'), foreground=theme.color('text'))
            except tk.TclError:
                pass
        for button in self.toolbar._buttons.values():
            self.toolbar._set_image_for_button(button)
        self.redraw(force=True)

    def _queue_toolbar_skin(self,event):
        # The shared theme styles newly mapped native widgets after their own
        # bindings. Restore this component's image treatment once mapping ends.
        if self._toolbar_skin_id:self.after_cancel(self._toolbar_skin_id)
        self._toolbar_skin_id=self.after_idle(self._restore_toolbar_skin)

    def _restore_toolbar_skin(self):
        self._toolbar_skin_id=None
        self.toolbar.configure(background=theme.color('surface'))
        for child in self.toolbar.winfo_children():
            try:child.configure(background=theme.color('surface'),foreground=theme.color('text'))
            except tk.TclError:pass
        self.toolbar.set_history_buttons()

    def _dispose(self, event):
        if event.widget is self:
            bus.unsubscribe(self._theme_subscription)
            if self._toolbar_skin_id:self.after_cancel(self._toolbar_skin_id)
            pending=getattr(self.canvas,'_idle_draw_id',None)
            if pending:
                try:self.after_cancel(pending)
                except tk.TclError:pass
                self.canvas._idle_draw_id=None
            for callback in self._time_connections:
                self.canvas.mpl_disconnect(callback)

    def enable_time_cursor(self):
        """Activa la presentación v3 sin modificar el documento persistido."""
        if self._time_enabled:
            return
        self._time_enabled = True
        if hasattr(self,'integration_toolbar'):
            self.integration_toolbar.pack_forget()
            self.toolbar.master.pack(side='bottom',fill='x',before=self.canvas.get_tk_widget())
        self.cursor_label.pack_forget()
        self.statistics_label.pack_forget()
        if self.reference is None:
            self.reference_toggle.pack_forget()
            self.reference_note_label.pack_forget()
        if not any(c['key'].endswith('|Hz') for c in self.spec.get('curves', [])):
            self.frequency_combo.pack_forget()
            self.frequency_label.pack_forget()
        self.redraw(force=True)

    def _cursor_drawn(self, event):
        if self._time_enabled and event.canvas is self.canvas and not self.canvas.is_saving() and self.canvas.supports_blit:
            self._time_background = self.canvas.copy_from_bbox(self.figure.bbox)
            self._paint_cursor()

    def _paint_cursor(self):
        if self._time_background is None or not self.canvas.supports_blit:
            return
        from matplotlib.transforms import Bbox
        renderer=self.canvas.get_renderer()
        regions=[]
        for ax,line in zip(self.axes,self._time_verticals):
            bounds=[]
            if line.get_visible():bounds.append(line.get_window_extent(renderer).padded(4))
            for dot,*_ in self._time_markers:
                if dot.axes is ax and dot.get_visible():bounds.append(dot.get_window_extent(renderer).padded(3))
            if bounds:regions.append(Bbox.union(bounds))
        # Legends are compact and have a fixed box. Refreshing their full box
        # also erases digits that disappear when a value becomes unavailable.
        for ax in self.axes:
            legend=ax.get_legend()
            if legend is not None:regions.append(legend.get_window_extent(renderer).padded(3))
        dirty=self._time_regions+regions
        self._time_regions=regions
        self.canvas.restore_region(self._time_background)
        for artist in self._time_artists:
            if artist.get_visible():
                self.figure.draw_artist(artist)
        for region in dirty:
            region=Bbox.intersection(region,self.figure.bbox)
            if region is not None:self.canvas.blit(region)

    def set_time_cursor(self, t):
        """Actualiza solo líneas, marcadores y textos animados de la leyenda."""
        if not self._time_enabled:
            return
        self._time_value = t
        for line in getattr(self, '_time_verticals', []):
            line.set_xdata([t, t] if t is not None else [0, 0])
            line.set_visible(t is not None)
        from .time_cursor import number
        for marker, text, label, key, samples in getattr(self, '_time_markers', []):
            # La instantánea se reconstruye solo cuando cambian las muestras.
            version = (id(samples), samples.version, key, self._frequency_options())
            cached = self._time_samples.get(version)
            if cached is None and t is not None:
                from .time_cursor import TemporalResult
                try:
                    from types import SimpleNamespace
                    x, y, unit = self._series(samples, key)
                    data = TemporalResult(SimpleNamespace(keys=[TIME_KEY,key],rows=np.column_stack((x,y))))
                    cached = (data, unit)
                    self._time_samples[version] = cached
                except (ValueError, TypeError):
                    pass
            value = cached[0].sample(key, float(t)) if cached and t is not None else None
            marker.set_data([t], [value]) if value is not None else marker.set_data([], [])
            marker.set_visible(value is not None)
            if text is not None:
                unit = cached[1] if cached else key.rsplit('|', 1)[-1]
                text.set_text(label + '  ' + (number(value, 4) if value is not None else '—') + ' ' + unit)
        if self.canvas.get_tk_widget().winfo_ismapped():
            if self._time_background is not None and self.canvas.supports_blit:
                self._paint_cursor()
            else:
                self.canvas.draw_idle()

    def set_events(self, events):
        self._analysis_events = list(events)
        self.redraw(force=True)

    def _frequency_options(self):
        return self.frequency_mode.get(), float(self.spec.get('frequency_base_hz', 50.0))

    def _source_name(self):
        return tr('plots.model') if self.source_label == 'Modelo' else self.source_label

    def add_run(self, result_id, buffer, label=None):
        """Superpone otra corrida conservando su base temporal independiente."""
        self.overlays[result_id] = (label or str(result_id), buffer)
        self.redraw(force=True)

    def remove_run(self, result_id):
        self.overlays.pop(result_id, None)
        self.redraw(force=True)

    def import_run_csv(self):
        """Lee el CSV de señales exportado por SignalBuffer sin interpolación."""
        path = filedialog.askopenfilename(parent=self, filetypes=[('CSV', '*.csv')])
        if not path:
            return
        from pathlib import Path
        from tkinter import messagebox
        try:
            samples = SignalBuffer()
            with open(path, encoding='utf-8-sig', newline='') as stream:
                reader = csv.reader(stream)
                keys = next(reader)
                if TIME_KEY not in keys or not any(c['key'] in keys for c in self.spec.get('curves', [])):
                    raise ValueError(tr('plots.incompatible_run'))
                samples.reset(keys)
                samples.append(reader)
            self.add_run(path, samples, Path(path).stem)
        except (ValueError, OSError, StopIteration) as exc:
            messagebox.showerror(tr('plots.add_run'), str(exc), parent=self)

    def _series(self, samples, key):
        mode, base_hz = self._frequency_options()
        x, y, unit = display_series(key, *samples.series(key), mode, base_hz)
        curve = next((c for c in self.spec.get('curves', []) if c['key'] == key), {})
        axes = self.spec.get('axis_specs', [])
        axis = curve.get('axis', 0)
        if axis < len(axes):
            options = axes[axis]
            x, y = samples.series(key)
            unit = options['unit']
            if options.get('reference') == 'deviation':
                y = np.asarray(y) - options['base']
                unit += ' de desviación'
        return x, y, unit

    def get_presentation(self):
        """Exporta página resuelta, plantilla original y faltantes de la corrida."""
        from copy import deepcopy
        data = deepcopy(self.spec)
        data.update(schema='power_machine-plot-page-v1', version=1,
                    frequency_mode=self.frequency_mode.get(), show_ref=self.show_ref.get())
        return data

    def apply_presentation(self, data, catalog=None, index=None):
        """Aplica plantilla v1 o página heredada y devuelve faltantes explícitos."""
        from copy import deepcopy
        from power_machine.gui.presentation_specs import resolve_template
        if catalog is not None and 'template' in data:
            data = dict(deepcopy(data), **resolve_template(data['template'], catalog, index))
        elif 'signals' in data:
            if catalog is None:
                raise ValueError('Se requiere catálogo de la corrida para resolver la plantilla')
            data = resolve_template(data, catalog, index)
        else:
            data = deepcopy(data)
        validate_plot_specs([data], set(self.buffer.keys) | {c['key'] for c in data.get('curves', [])})
        missing = list(data.get('missing_signals', []))
        retained = []
        for curve in data.get('curves', []):
            if curve['key'] in self.buffer.keys:
                retained.append(curve)
            else:
                missing.append(dict(key=curve['key'], reason='Señal ausente en la corrida'))
        data.update(curves=retained, missing_signals=missing, resolved_signals=deepcopy(retained))
        self.spec = data
        self.figure.clear()
        self.axes = self.figure.subplots(data.get('axes', 1), 1, sharex=True, squeeze=False).ravel().tolist()
        self.frequency_mode.set(data.get('frequency_mode', 'absolute'))
        self.show_ref.set(data.get('show_ref', True))
        self._cursor_points = []
        self.redraw(force=True)
        return deepcopy(missing)

    def toggle_reference(self):
        if self._on_reference_toggle:
            self._on_reference_toggle(self.show_ref.get())
        self.redraw(force=True)

    def set_axis_scale(self, axis, xlim_s=None, ylim=None):
        """Fija límites manuales para integración futura sin cambiar el documento externo."""
        if not 0 <= int(axis) < len(self.axes):
            raise ValueError('Subgráfico de escala fuera de rango')
        scale = self.spec.setdefault('axis_scales', {})
        value = {}
        if xlim_s is not None:
            value['xlim_s'] = list(xlim_s)
        if ylim is not None:
            value['ylim'] = list(ylim)
        scale[str(axis)] = value
        axis_scale(self.spec, axis)
        self.redraw(force=True)

    def reset_axis_scale(self, axis=None):
        """Restablece la escala automática de un eje o de toda la página."""
        scales = self.spec.setdefault('axis_scales', {})
        if axis is None:
            scales.clear()
        else:
            scales.pop(str(axis), None)
            scales.pop(axis, None)
        self.redraw(force=True)

    def xy_data(self, x_key, y_key):
        """Expone pares X-Y de la corrida principal para un diálogo de integración."""
        return xy_series(self.buffer, x_key, y_key)

    def xy_figure(self, x_key, y_key):
        """Crea el gráfico X-Y con escala automática para integración futura."""
        return xy_figure(self.buffer, x_key, y_key)

    def _reference_state(self):
        available, reason = reference_availability(self.reference, [curve['key'] for curve in self.spec.get('curves', [])])
        self.reference_toggle.configure(state='normal' if available else 'disabled')
        self.reference_note.set({'No se cargó una curva de referencia externa': tr('plots.no_reference'),'La referencia externa no contiene las señales de esta página': tr('plots.reference_missing'),'La referencia externa no contiene todas las señales de esta página': tr('plots.reference_partial')}.get(reason, reason))
        return available

    def redraw(self, force=False):
        if not force and (self.last_version == self.buffer.version or not self.gate.ready()):
            return
        self.last_version = self.buffer.version
        self._time_background = None
        self._time_regions = []
        self._time_samples.clear()
        self._time_artists, self._time_markers, self._time_verticals = [], [], []
        sources = {}
        self.figure.set_facecolor(theme.mpl_rc()['figure.facecolor'])
        self.figure.suptitle(tr(self.spec['name_key']) if self.spec.get('name_key') else self.spec.get('name', ''), fontsize=10, color=theme.color('text'))
        reference_available = self._reference_state()
        old_limits = [(ax.get_xlim(), ax.get_ylim()) for ax in self.axes] if self.toolbar.mode else None
        for ax in self.axes:
            ax.clear()
            rc = theme.mpl_rc()
            ax.set_facecolor(rc['axes.facecolor'])
            ax.tick_params(colors=rc['xtick.color'])
            ax.xaxis.label.set_color(rc['axes.labelcolor'])
            ax.yaxis.label.set_color(rc['axes.labelcolor'])
            for spine in ax.spines.values():
                spine.set_color(rc['axes.edgecolor'])
            ax.grid(True, color=theme.color('grid'), linewidth=.5, alpha=.5 if self._time_enabled else 1)
            ax.tick_params(labelsize=8 if len(self.axes) < 5 else 7)
        legends, stats = [[] for ax in self.axes], []
        for number, curve in enumerate(self.spec.get('curves', [])):
            key, axis = curve['key'], min(len(self.axes) - 1, max(0, int(curve.get('axis', 0))))
            ax = self.axes[axis]
            label = curve_label(curve)
            x, y, unit = self._series(self.buffer, key)
            x, y = decimate(*break_at_events(x, y, self.spec.get('events', [])))
            style = curve.get('line_style', self._LINE_STYLES[number % len(self._LINE_STYLES)])
            line, = ax.plot(x, y, linestyle=style, color=theme.color(curve.get('color_token', 'accent')), linewidth=curve.get('line_width', 1.15), marker=curve.get('marker', ''),
                            label=label + ' · ' + self._source_name() if len(x) else '_nolegend_')
            handle = line if len(x) else None
            sources[id(line)] = (key, self.buffer)
            for run_number, (run_label, samples) in enumerate(self.overlays.values()):
                ox, oy, _ = self._series(samples, key)
                ox, oy = decimate(*break_at_events(ox, oy, self.spec.get('events', [])))
                if len(ox):
                    overlay, = ax.plot(ox, oy, color=theme.color('kv_230'),
                                       linestyle=self._LINE_STYLES[(run_number + 1) % 4],
                                       linewidth=1.1, label=label + ' · ' + run_label)
                    legends[axis].append((overlay, label + ' · ' + run_label))
                    sources[id(overlay)] = (key, samples)
            if self.show_ref.get() and reference_available:
                rx, ry, _ = self._series(self.reference, key)
                rx, ry = decimate(*break_at_events(rx, ry, self.spec.get('events', [])))
                if len(rx):
                    reference_style = '--' if style == '-' else ':'
                    ref_line, = ax.plot(rx, ry, linestyle=reference_style, color=theme.color(curve.get('reference_color_token', 'warn')), linewidth=.9,
                                        label=label + ' · ' + self.reference_label)
                    sources[id(ref_line)] = (key, self.reference)
                    if self.spec.get('source_legend') or self._time_enabled:
                        legends[axis].append((ref_line,label+' · '+self.reference_label))
                    if handle is None:
                        handle = ref_line
            if handle is not None:
                legends[axis].append((handle,label+' · '+self._source_name() if self.spec.get('source_legend') else label))
            stat = series_statistics(*self._series(self.buffer, key)[:2])
            if stat['samples']:
                stats.append(tr('plots.statistics', label=label, unit=unit, **stat))
            ax.set_ylabel(unit or 'p.u.', fontsize=9 if len(self.axes) < 5 else 8)
        for axis, (ax, entries) in enumerate(zip(self.axes, legends)):
            axis_specs = self.spec.get('axis_specs', [])
            if axis < len(axis_specs):
                options = axis_specs[axis]
                ax.set_yscale(options.get('scale', 'linear'))
                for field, setter in (('xlim_s', ax.set_xlim), ('ylim', ax.set_ylim)):
                    if field in options:
                        setter(options[field])
            events = getattr(self, '_analysis_events', self.spec.get('events', []))
            faults = {}
            for marker in events:
                time_s = marker.get('time_s') if isinstance(marker, dict) else None
                if isinstance(time_s, (int, float)):
                    kind, target = marker.get('kind'), marker.get('target_id', '')
                    if kind == 'short_circuit':
                        faults[target] = time_s
                    elif kind == 'clear_short_circuit' and target in faults:
                        start=faults.pop(target)
                        ax.axvspan(start, time_s, color=theme.color('accent2'), alpha=.12, zorder=0)
                        if self._time_enabled:
                            ax.text((start+time_s)/2,.98,tr('v3.analysis.fault_band'),transform=ax.get_xaxis_transform(),
                                    ha='center',va='top',fontsize=8,color=theme.color('text_muted'))
                    ax.axvline(float(time_s), color=theme.color('text_muted'), linestyle='--', linewidth=.8)
                    if axis == 0 and not self._time_enabled:
                        label = tr('v3.analysis.event.'+kind) if self._time_enabled and kind else tr(marker['label_key']) if marker.get('label_key') else str(marker.get('label', tr('plots.event')))
                        ax.annotate(label, (float(time_s), 1), xycoords=('data', 'axes fraction'),
                                    xytext=(2, -3), textcoords='offset points', rotation=90, va='top', fontsize=7,
                                    color=theme.color('text_muted'))
            for point in self._cursor_points:
                ax.axvline(point['time_s'], color=theme.color('text_muted'), linestyle='--', linewidth=.8)
            xlim_s, ylim = axis_scale(self.spec, axis)
            if xlim_s:
                ax.set_xlim(xlim_s)
            if ylim:
                ax.set_ylim(ylim)
            if entries:
                title = (tr('plots.ref_legend') if self.source_label == 'Modelo' else tr('plots.runs_legend')) if self.reference is not None and self.show_ref.get() else tr('plots.single_run')
                if self.spec.get('source_legend'):title=tr('plots.source_legend')
                layout = dict(loc='lower right', bbox_to_anchor=(1,1.04), borderaxespad=0, ncol=max(1,len(entries))) if self._time_enabled else dict(loc='upper left', bbox_to_anchor=(1.01, 1), borderaxespad=0)
                labels = [(line, label+'  0,0000 '+sources[id(line)][0].rsplit('|',1)[-1]) for line,label in entries] if self._time_enabled else entries
                ax.legend(*zip(*labels), fontsize=8 if self._time_enabled else 7 if len(self.axes) < 5 else 6, title=None if self._time_enabled else title, title_fontsize=7,
                          **layout,
                          facecolor=theme.color('surface'), edgecolor=theme.color('border'), labelcolor=theme.color('text'))
                ax.get_legend().get_title().set_color(theme.color('text'))
                if self._time_enabled:
                    for (line, label), text in zip(entries, ax.get_legend().get_texts()):
                        key, samples = sources[id(line)]
                        dot, = ax.plot([], [], 'o', color=line.get_color(), markersize=5, animated=True, zorder=8)
                        text.set_animated(True)
                        self._time_markers.append((dot, text, label, key, samples))
                        self._time_artists.extend((dot, text))
            if self._time_enabled:
                from matplotlib.ticker import FuncFormatter
                from .i18n import language
                def decimal(value, position):
                    text = f'{value:g}'
                    return text.replace('.', ',') if language() == 'es' else text
                ax.xaxis.set_major_formatter(FuncFormatter(decimal))
                ax.yaxis.set_major_formatter(FuncFormatter(decimal))
                curves = [c for c in self.spec.get('curves', []) if min(len(self.axes)-1, int(c.get('axis',0))) == axis]
                kinds = {'m:u':'voltage','s:xspeed':'speed','s:speed':'speed','s:phi':'angle','m:phiu':'angle','m:fehz':'frequency',
                         'classical:u_pu':'voltage','classical:omega_pu':'speed','classical:delta_deg':'angle',
                         'classical:f_hz':'frequency','classical:coi_frequency':'frequency'}
                if curves:
                    variable = curves[0]['key'].rsplit('|',2)[-2]
                    ax.set_title(tr('v3.analysis.magnitude.'+kinds[variable]) if variable in kinds else variable, loc='left', fontsize=10, color=theme.color('text'), pad=9)
                vertical = ax.axvline(0, color=theme.color('accent2'), linewidth=1, animated=True, visible=False, zorder=7)
                self._time_verticals.append(vertical)
                self._time_artists.append(vertical)
        self.axes[-1].set_xlabel(tr('plots.time_axis'), fontsize=9)
        if old_limits:
            for ax, (xl, yl) in zip(self.axes, old_limits):
                ax.set_xlim(xl)
                ax.set_ylim(yl)
        notice = tr('plots.missing', count=len(self.spec.get('missing_signals', []))) if self.spec.get('missing_signals') else ''
        self.statistics.set(notice + ('  |  '.join(stats) if stats else tr('plots.empty_samples')))
        if self._time_enabled:
            from matplotlib.text import Text
            for text in self.figure.findobj(Text):text.set_fontfamily(theme.font('body')[0])
            self.set_time_cursor(self._time_value)
        self.canvas.draw_idle()

    def _cursor_point(self, axis, time_s):
        values = {}
        for curve in self.spec.get('curves', []):
            if int(curve.get('axis', 0)) != axis:
                continue
            sample = nearest_sample(*self._series(self.buffer, curve['key'])[:2], time_s)
            if sample is not None:
                values[curve['key']] = sample[1]
        return {'time_s': float(time_s), 'values': values}

    def motion(self, event):
        if event.inaxes not in self.axes or event.xdata is None:
            return
        axis = self.axes.index(event.inaxes)
        values = []
        for curve in self.spec.get('curves', []):
            if int(curve.get('axis', 0)) != axis:
                continue
            sample = nearest_sample(*self._series(self.buffer, curve['key'])[:2], event.xdata)
            if sample is not None:
                parts = curve['key'].rsplit('|', 2)
                values.append(f'{parts[1]} = {sample[1]:.6g} {self._series(self.buffer, curve["key"])[2]}')
            if self.show_ref.get() and self.reference is not None:
                sample = nearest_sample(*self._series(self.reference, curve['key'])[:2], event.xdata)
                if sample is not None:
                    values.append(f'{self.reference_label}: {sample[1]:.6g} {self._series(self.reference, curve["key"])[2]}')
        self.cursor.set(f't = {event.xdata:.6g} s   ' + '   |   '.join(values))

    def capture_cursor(self, event):
        if event.inaxes not in self.axes or event.xdata is None:
            return
        if self._time_enabled and hasattr(self, 'time_transport'):
            if not self.toolbar.mode:
                self.time_transport.pause()
                self.time_transport.seek(event.xdata)
            return
        point = self._cursor_point(self.axes.index(event.inaxes), event.xdata)
        self._cursor_points = (self._cursor_points + [point])[-2:]
        if len(self._cursor_points) == 2:
            delta = cursor_difference(*self._cursor_points)
            terms = [f'Δt = {delta["delta_time_s"]:.6g} s']
            terms.extend(f'Δ{key.rsplit("|", 2)[1]} = {value:.6g}' for key, value in delta['delta_values'].items())
            self.cursor.set('   |   '.join(terms))
        else:
            self.cursor.set(tr('plots.cursor_first', time=point['time_s']))
        self.redraw(force=True)

    def export_csv(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if path:
            export_visible(path, self.buffer, [c['key'] for c in self.spec.get('curves', [])],
                           self.reference if self.show_ref.get() else None, self.source_label, self.reference_label,
                           self.overlays.values())

    def export_png(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.png', filetypes=[(tr('plots.png_image'), '*.png')])
        if path:
            self.save_figure(path, dpi=180, facecolor=theme.color('surface'))

    def save_figure(self,path,**options):
        """Incluye cursor y lecturas en la exportación de un fotograma estático."""
        artists=list(self._time_artists)
        states=[artist.get_animated() for artist in artists]
        try:
            for artist in artists:artist.set_animated(False)
            self.figure.savefig(path,**options)
        finally:
            for artist,state in zip(artists,states):artist.set_animated(state)
            self._time_background=None
            self.canvas.draw_idle()

    def export_svg(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.svg', filetypes=[('SVG', '*.svg')])
        if path:
            self.save_figure(path, format='svg', facecolor=self.figure.get_facecolor())

    def export_pdf(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.pdf', filetypes=[('PDF', '*.pdf')])
        if path:
            self.save_figure(path, format='pdf', facecolor=theme.color('surface'))


class VariablesDialog(tk.Toplevel):
    def __init__(self, master, catalog, pages, on_add, element_id=None, index=None, live=False):
        super().__init__(master)
        self.title(tr('plots.select_signals'))
        self.geometry('880x540')
        self.transient(master)
        self.grab_set()
        if live:
            ttk.Label(self, text=tr('plots.running_simulation_signals_acquired_in_this_run'), padding=10).pack(anchor='w')
        index = index or {}
        self.catalog = [dict(row, element_label=element_label(index.get(row['element_id'], dict(name=row['element_name'])), index),
                             substation=station(index.get(row['element_id'], {})))
                        for row in catalog if not element_id or row['element_id'] == element_id]
        query = tk.StringVar()
        self.query = query
        self.station_filter = tk.StringVar(value=tr('plots.all_stations'))
        ttk.Label(self, text=tr('plots.search_element_signal_or_unit'), padding=10).pack(anchor='w')
        ttk.Entry(self, textvariable=query).pack(fill='x', padx=10)
        filters = ttk.Frame(self, padding=(10, 5))
        filters.pack(fill='x')
        ttk.Label(filters, text=tr('plots.substation')).pack(side='left', padx=(0, 10))
        ttk.Combobox(filters, textvariable=self.station_filter, values=[tr('plots.all_stations')] + sorted({r['substation'] for r in self.catalog}), state='readonly', width=45).pack(side='left')
        body = ttk.Frame(self, padding=10)
        body.pack(fill='both', expand=True)
        tree = ttk.Treeview(body, columns=('name', 'var', 'unit', 'description'), show='headings', selectmode='extended')
        self.tree = tree
        for key, title, width in [('name', tr('plots.name_station_kv'), 420), ('var', tr('plots.variable'), 110), ('unit', tr('plots.unit'), 60), ('description', tr('plots.description'), 200)]:
            tree.heading(key, text=title)
            tree.column(key, width=width)
        scroll = ttk.Scrollbar(body, command=tree.yview)
        tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        tree.pack(fill='both', expand=True)
        def filter_rows(*_):
            tree.delete(*tree.get_children())
            for i, row in enumerate(self.catalog):
                text = ' '.join(str(row.get(k, '')) for k in ('element_label', 'variable', 'unit', 'description')).casefold()
                if all(word in text for word in query.get().casefold().split()) and (self.station_filter.get() == tr('plots.all_stations') or row['substation'] == self.station_filter.get()):
                    tree.insert('', 'end', iid=str(i), values=[row.get(k, '') for k in ('element_label', 'variable', 'unit', 'description')])
        query.trace_add('write', filter_rows)
        self.station_filter.trace_add('write', filter_rows)
        filter_rows()
        bar = ttk.Frame(self, padding=10)
        bar.pack(fill='x')
        choices = {tr('plots.page_axis', page=p.get('name', tr('plots.page')), axis=axis + 1): (i, axis)
                   for i, p in enumerate(pages) for axis in range(p.get('axes', 1))}
        dest = tk.StringVar(value=next(iter(choices), ''))
        ttk.Label(bar, text=tr('plots.destination')).pack(side='left', padx=(0, 8))
        ttk.Combobox(bar, textvariable=dest, values=list(choices), state='readonly', width=45).pack(side='left')
        def add():
            if tree.selection() and dest.get() in choices:
                page, axis = choices[dest.get()]
                on_add(page, axis, [self.catalog[int(i)]['key'] for i in tree.selection()])
                self.destroy()
        ttk.Button(bar, text=tr('plots.add_signals'), command=add).pack(side='right')
