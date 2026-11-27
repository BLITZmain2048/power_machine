"""Vista vectorial Tk del unifilar, sin cálculo eléctrico ni cambios al modelo."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import textwrap
import math
from power_machine.gui.theme import theme
import tkinter as tk
from tkinter import ttk
from typing import Callable

from .loading import diagram_loading
from .data_cards import ElementCards
from .navigation import CanvasNavigation
from power_machine.gui.i18n import tr
from power_machine.gui.results_tables import display_value

from .layout import (OFF_COLOR, VOLTAGE_COLORS, apply_positions, build_scene,
                     clean_label, element_index, load_positions, save_positions,
                     voltage_color)


@dataclass
class DiagramCallbacks:
    on_select: Callable[[str], None]
    on_open_properties: Callable[[str], None]
    on_context_action: Callable[[str, str], None]


def context_actions(item, gen_trip_supported=False):
    """La desconexión de generador exige soporte explícito del motor."""
    actions = [(tr('diagram.properties'), "properties")]
    cls = item.get("class")
    if cls == "ElmTerm" or item.get("group") == "buses":
        actions.append((tr('diagram.fault'), "short_circuit"))
    if cls in ("ElmLne", "ElmTr2", "ElmTr3", "ElmZpu", "ElmCoup", "StaSwitch"):
        actions.extend(((tr('diagram.open'), "switch_open"), (tr('diagram.close'), "switch_close")))
    if cls == "ElmLod":
        actions.append((tr('diagram.load_step'), "load_step"))
    if cls in ("ElmSym", "ElmGenstat", "ElmPvsys") and gen_trip_supported:
        actions.append((tr('diagram.gen_trip'), "gen_trip"))
    actions.extend(((tr("diagram.outserv"), "outserv"),
                    (tr("diagram.disconnect"), "disconnect"),
                    (tr("diagram.results"), "results"),
                    (tr('diagram.plot'), "add_to_plot"),
                    (tr('diagram.locate'), "locate")))
    return actions


def _result_text(symbol, result, *, item=None, digits=None, include_loading=False, boxes=None):
    """Valores del motor. P y Q positivos salen de la barra del extremo indicado."""
    if not result:
        return ""
    item = symbol.item if item is None else item
    if (item.get('outserv') or result.get('energized') is False or
            result.get('status') in ('sin_energia_pasiva', 'ambos_puertos_abiertos',
                                     'sin_referencia', 'fuera_de_servicio')):
        return '—'
    def value(key, digits=3):
        number = result.get(key)
        precision = (format_digits or {}).get(key, digits)
        return "—" if number is None or not math.isfinite(float(number)) else display_value(float(number),key,precision=precision)
    format_digits = digits
    if boxes is not None:
        fields = boxes.get(item.get('class', 'ElmTerm' if symbol.kind == 'bus' else ''), [])
        lines = []
        for field in fields:
            end = field['end']
            if end == 'local':
                local_bus = symbol.item.get('_local_bus')
                end = next((e for e in ('from', 'to', 'hv', 'mv', 'lv')
                            if local_bus and local_bus == result.get('bus_' + e)),
                           next((e for e in ('from', 'hv', 'to', 'mv', 'lv') if 'bus_' + e in result), 'from'))
            variable = field['variable']
            key = (f'{variable}_{end}_' + {'p': 'mw', 'q': 'mvar', 'loading': 'pct'}[variable]
                   if variable in ('p', 'q', 'loading') and end != 'none' else variable)
            if variable == 'loading' and end == 'none':
                key = 'loading_pct'
            number = result.get(key)
            text = '—' if number is None or not math.isfinite(float(number)) else display_value(float(number),key,precision=field['precision'])
            lines.append(f'{field.get("label", variable)} {text} {field["unit"]}')
        return '\n'.join(lines)
    if symbol.kind == "bus":
        if symbol.item.get('_curated'):
            return f"{value('u_pu', 3)} p.u.\n{value('u_kv', 1)} kV · {value('angle_deg', 1)}°"
        return f"U {value('u_kv')} kV\nu {value('u_pu', 4)} p.u.\n{tr('diagram.angle')} {value('angle_deg')} °"
    if any('p_' + end + '_mw' in result or 'q_' + end + '_mvar' in result for end in ('from', 'to', 'hv', 'mv', 'lv')):
        local_bus = symbol.item.get('_local_bus')
        end = next((e for e in ('from', 'to', 'hv', 'mv', 'lv') if local_bus and local_bus == result.get('bus_' + e)), 'from')
        text = f"P {value('p_' + end + '_mw', 1)} MW\nQ {value('q_' + end + '_mvar', 1)} Mvar"
        if include_loading:
            loading_key = 'loading_' + end + '_pct'
            text += f"\n{tr('diagram.loading')} {value(loading_key if loading_key in result else 'loading_pct', 1)} %"
        return text
    if 'p_mw' in result or 'q_mvar' in result:
        return f"P {value('p_mw', 1)} MW\nQ {value('q_mvar', 1)} Mvar"
    return ""


def result_text(symbol, result, *, layers=None, **kwargs):
    if layers is None:
        return _result_text(symbol, result, **kwargs)
    boxes = kwargs.get('boxes')
    if boxes is not None:
        from copy import deepcopy
        boxes = deepcopy(boxes)
        names = {'u_pu':'U', 'u_kv':'U', 'angle_deg':'angle', 'p':'P', 'q':'Q', 'loading':'loading'}
        for cls, fields in boxes.items():
            boxes[cls] = [f for f in fields if layers.get(names.get(f['variable'], f['variable']), True)]
        kwargs['boxes'] = boxes
        return _result_text(symbol, result, **kwargs)
    # Split the compact bus box so voltage and angle remain independent layers.
    if symbol.kind == 'bus' and result and not result.get('energized') is False:
        lines = []
        for key, layer, label, unit, precision in [('u_pu','U','U','p.u.',3),
                ('u_kv','U','U','kV',1), ('angle_deg','angle',tr('diagram.angle'),'°',1)]:
            value = result.get(key)
            if layers.get(layer) and value is not None and math.isfinite(float(value)):
                lines.append(f'{label} {display_value(value,key,precision=precision)} {unit}')
        return '\n'.join(lines) if not kwargs.get('item',symbol.item).get('outserv') else '—'
    lines = _result_text(symbol, result, **kwargs).splitlines()
    return '\n'.join(line for line in lines if
        layers.get('P' if line.startswith('P ') else 'Q' if line.startswith('Q ') else 'loading', True))


class DiagramView(CanvasNavigation, ElementCards, ttk.Frame):
    """En el general, doble clic entrega el ID de grupo a on_open_properties.

    diagram view puede reconocer esos IDs con diagram_groups y abrir su pestaña.
    También se emite <<DiagramOpenGroup>> con selected_group disponible.
    """

    def __init__(self, master, model: dict, layout_path: Path,
                 callbacks: DiagramCallbacks, group: str | None = None):
        super().__init__(master)
        self.model = model
        self.layout_path = Path(layout_path)
        self.callbacks = callbacks
        self.group = group
        self.selected_group = None
        self.gen_trip_supported = bool(model.get("capabilities", {}).get("gen_trip", False))
        self.index = element_index(model)
        self.scene = build_scene(model, group)
        self.positions = {}
        self.layout_error = None
        try:
            self.positions = load_positions(self.layout_path, group)
        except (OSError, ValueError, TypeError) as exc:
            self.layout_error = str(exc)
        apply_positions(self.scene, self.positions)
        self.scale = 1.0
        self.x_factor = 1.0
        self.offset = (30.0, 30.0)
        self.results = {}
        self.color_mode = 'voltage'
        self.voltage_limits = (.95, 1.05)
        self.result_digits = {}
        self.box_formats = None
        self.loading_bands = (80, 100)
        self.historical = False
        self._presentation = None
        self.detail_zoom = .65
        self.result_detail_zoom = .80
        from ..settings import settings
        self._settings = getattr(getattr(self, 'app', None), 'settings', settings)
        self.show_results = self._settings.get('diagram_result_labels', False) is True
        self.selected_id = None
        self._drag = None
        self._paint_job = None
        self._initial_fit = None
        self._menu = None
        self._user_zoom = False
        self._tooltip = None
        self._hover_id = None
        self._fonts = {}
        self.zoom_text = tk.StringVar(self, "100 %")
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text=tr('diagram.fit'), command=self.fit).pack(side="left", padx=4, pady=3)
        ttk.Button(toolbar, textvariable=self.zoom_text, command=self._actual_size).pack(side="left")
        self._result_var = tk.BooleanVar(self, self.show_results)
        ttk.Checkbutton(toolbar, text=tr('v3.diagram.result_labels'), variable=self._result_var,
                        command=lambda: self.set_result_boxes_visible(self._result_var.get())).pack(side="left", padx=8)
        self.status = tk.StringVar(self, self.scene.title)
        ttk.Label(toolbar, textvariable=self.status, width=25).pack(side="left", padx=8)
        options = ttk.Frame(self)
        options.pack(fill='x')
        self._color_var = tk.StringVar(self, tr('diagram.color.voltage'))
        choices = ttk.Combobox(options, textvariable=self._color_var, state='readonly', width=21,
                              values=(tr('diagram.color.voltage'), tr('diagram.color.voltage_band'), tr('diagram.color.loading'), tr('diagram.color.state')))
        choices.pack(side='left', padx=4)
        choices.bind('<<ComboboxSelected>>', lambda e: self.set_color_mode(
            dict(zip(choices['values'], ('voltage', 'voltage_band', 'loading', 'state')))[self._color_var.get()]))
        self._search_var = tk.StringVar(self)
        search = ttk.Entry(options, textvariable=self._search_var, width=24)
        search.pack(side='left', padx=4)
        search.bind('<Return>', lambda e: self.find_element(self._search_var.get()))
        ttk.Button(options, text=tr('diagram.search'), command=lambda: self.find_element(self._search_var.get())).pack(side='left')
        canvas_parent = self._canvas_parent()
        self.canvas = tk.Canvas(canvas_parent, background=theme.color('diagram_bg'), highlightthickness=0,
                                width=1000, height=650, takefocus=True)
        self.canvas.pack(fill="both", expand=True)
        self._init_cards()
        self.scene.results = self.results
        self._labels_job = None
        self._moving_wires = None
        legend = ttk.Frame(self)
        legend.pack(fill="x", before=self.canvas if canvas_parent is self else canvas_parent)
        self._legend_var = tk.StringVar(self)
        ttk.Label(legend, textvariable=self._legend_var).pack(side='left', padx=5)
        self._update_legend()
        if self.layout_error:
            self.status.set(tr('diagram.read_error') + clean_label(self.layout_error))
        self.canvas.bind("<Configure>", self._configure)
        self.canvas.bind("<Leave>", lambda e: self._hide_tooltip())
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Button-4>", lambda e: self._zoom(e.x, e.y, 1.15))
        self.canvas.bind("<Button-5>", lambda e: self._zoom(e.x, e.y, 1 / 1.15))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonPress-2>", self._pan_press)
        self.canvas.bind("<B2-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-2>", self._release)
        self.canvas.bind("<Double-Button-1>", self._double)
        self.canvas.bind("<Button-3>", self._context)
        self.canvas.bind('<Shift-F10>',self._keyboard_context)
        self.canvas.bind('<Key-Menu>',self._keyboard_context)
        self.canvas.bind("<Motion>", self._hover)
        self.bind("<Destroy>", self._destroy, add=True)
        self.draw()
        self._initial_fit = self.after_idle(self._fit_initial)

    def _canvas_parent(self):
        return self

    def _configure(self, event):
        if not self._user_zoom:
            if self._initial_fit:
                self.after_cancel(self._initial_fit)
            self._initial_fit = self.after_idle(self._fit_initial)

    def _destroy(self, event):
        if event.widget is self:
            self._hide_tooltip()
            for job in (self._paint_job, self._initial_fit, self._labels_job):
                if job:
                    self.after_cancel(job)

    def _fit_initial(self):
        self._initial_fit = None
        self.fit()

    def _actual_size(self):
        self._user_zoom = True
        self.x_factor = 1.0
        self.scale = 1.0
        self.offset = (30.0, 30.0)
        self.draw()

    def _xy(self, x, y):
        return (x * self.scale * self.x_factor + self.offset[0], y * self.scale + self.offset[1])

    def _line(self, points, **kwargs):
        coords = [v for p in points for v in self._xy(*p)]
        return self.canvas.create_line(*coords, **kwargs)

    def _color(self, symbol):
        item = self.index.get(symbol.item.get("id", symbol.id), symbol.item)
        result = self.results.get(symbol.id, {})
        return self.element_color(item, result, symbol.un_kv)

    @property
    def is_dense(self):
        if getattr(self, '_density_scene', None) is not self.scene:
            self._density_scene = self.scene
            self._dense = sum(s.kind == 'bus' for s in self.scene.symbols.values()) >= 20
        return self._dense

    def detail_visible(self, key):
        """Las mallas muestran detalle al acercarse o seleccionar un equipo."""
        return not self.is_dense or self.scale >= self.detail_zoom or getattr(self, '_exporting', False)

    def result_visible(self, symbol):
        """Evita cajas de resultado superpuestas en mallas a poco zoom."""
        if not self.show_results or not symbol.result_box:
            return False
        if not getattr(self, '_exporting', False):
            x1,y1 = self._xy(*symbol.result_box[:2])
            x2,y2 = self._xy(*symbol.result_box[2:])
            if x2 < 0 or y2 < 0 or x1 > self.canvas.winfo_width() or y1 > self.canvas.winfo_height():
                return False
        if (not self.is_dense or self.scale >= self.result_detail_zoom or
                getattr(self, '_exporting', False)):
            return True
        return symbol.kind == 'bus' or symbol.id == self.selected_id

    def set_detail_zoom(self, minimum=.65):
        if not math.isfinite(minimum) or minimum <= 0:
            raise ValueError(tr('integration.the_detail_threshold_must_be_positive'))
        self.detail_zoom = minimum
        self.draw()

    def set_result_format(self, digits=None):
        """Configura decimales por clave del resultado del motor."""
        if any(not isinstance(v, int) or not 0 <= v <= 6 for v in (digits or {}).values()):
            raise ValueError(tr('integration.decimal_places_must_be_between_0_and'))
        self.result_digits = dict(digits or {})
        self.draw()

    def get_presentation(self):
        """Exporta copia JSON independiente. No incluye resultados eléctricos."""
        from copy import deepcopy
        from power_machine.gui.presentation_specs import default_presentation
        data = deepcopy(self._presentation) if self._presentation else default_presentation()
        data['diagram'].update(color_mode=self.color_mode, voltage_limits=list(self.voltage_limits),
            loading_bands=list(self.loading_bands), digits=dict(self.result_digits),
            boxes=deepcopy(self.box_formats or {}), show_results=self.show_results, historical=self.historical,
            legacy_boxes=self.box_formats is None)
        data['diagram']['positions'] = {key: [s.x, s.y] for key, s in self.scene.symbols.items()}
        data['diagram']['viewport'] = dict(scale=self.scale, x_factor=self.x_factor, offset=list(self.offset))
        return data

    def apply_presentation(self, data):
        """Aplica formatos después de validar todo el objeto, sin editar el modelo."""
        from power_machine.gui.presentation_specs import validate_presentation
        data = validate_presentation(data)
        spec = data['diagram']
        self._presentation = data
        self.box_formats = None if spec.get('legacy_boxes', False) else spec['boxes']
        self.result_digits = spec.get('digits', {})
        self.voltage_limits = tuple(spec['voltage_limits'])
        self.loading_bands = tuple(spec['loading_bands'])
        self.color_mode = spec['color_mode']
        self.historical = spec['historical']
        self.show_results = spec['show_results']
        self._result_var.set(self.show_results)
        self._rebuild_scene()
        if 'positions' in spec:
            self.positions = spec['positions']
            apply_positions(self.scene, self.positions)
        if 'viewport' in spec:
            self.scale = spec['viewport']['scale']
            self.x_factor = spec['viewport']['x_factor']
            self.offset = tuple(spec['viewport']['offset'])
            self._user_zoom = True
            if self._initial_fit:
                self.after_cancel(self._initial_fit)
                self._initial_fit = None
        self._color_var.set(dict(voltage=tr('diagram.color.voltage'), voltage_band=tr('diagram.color.voltage_band'),
                                 loading=tr('diagram.color.loading'), state=tr('diagram.color.state'))[self.color_mode])
        self._update_legend()
        self.draw()

    def set_color_mode(self, mode='voltage', *, voltage_limits=None):
        if mode not in ('voltage', 'voltage_band', 'loading', 'state'):
            raise ValueError(tr('integration.unknown_colour_mode'))
        if voltage_limits is not None:
            low, high = voltage_limits
            if not (math.isfinite(low) and math.isfinite(high) and 0 < low < high):
                raise ValueError(tr('integration.invalid_voltage_band'))
            self.voltage_limits = (low, high)
        self.color_mode = mode
        self._update_legend()
        self.draw()

    def _update_legend(self):
        low, high = self.voltage_limits
        warning, limit = self.loading_bands
        self._legend_var.set({
            'voltage': tr('diagram.legend.voltage'),
            'voltage_band': tr('v3.diagram.legend.voltage_band',
                low=display_value(low,precision=2), high=display_value(high,precision=2)),
            'loading': tr('v3.diagram.legend.loading',
                warning=display_value(warning), limit=display_value(limit)),
            'state': tr('diagram.legend.state'),
        }[self.color_mode])
        self._legend_var.set(self._legend_var.get()+' · '+tr('v3.fix.consult_help'))
        if self.historical:
            self._legend_var.set(tr('diagram.historical') + self._legend_var.get())

    def element_color(self, item, result, un_kv):
        if item.get('outserv') or result.get('energized') is False:
            return theme.color('deenergized')
        if self.color_mode == 'voltage_band':
            value = result.get('u_pu')
            return theme.color('deenergized') if value is None or not math.isfinite(value) else theme.color('ok') if self.voltage_limits[0] <= value <= self.voltage_limits[1] else theme.color('error')
        if self.color_mode == 'loading':
            value = result.get('loading_pct')
            bands = getattr(self, 'loading_bands', (80, 100))
            return theme.color('deenergized') if value is None or not math.isfinite(value) else theme.color('ok') if value < bands[0] else theme.color('warn') if value <= bands[1] else theme.color('error')
        return theme.color('ok') if self.color_mode == 'state' else voltage_color(un_kv)

    def find_element(self, query):
        """Busca por nombre o ID y centra sin modificar posiciones guardadas."""
        query = query.strip().casefold()
        if not query:
            return None
        matches = [key for key, s in self.scene.symbols.items() if query in key.casefold() or query in s.item.get('name', '').casefold()]
        if not matches:
            self.status.set(tr('diagram.not_found'))
            return None
        key = next((k for k in matches if k.casefold() == query), matches[0])
        self.center_on(key)
        return key

    def center_on(self, element_id):
        symbol = self.scene.symbols.get(element_id)
        if symbol is None:
            return False
        self._user_zoom = True
        self.scale = max(self.scale, self.detail_zoom)
        self.x_factor = 1.0
        self.offset = (self.canvas.winfo_width() / 2 - symbol.x * self.scale,
                       self.canvas.winfo_height() / 2 - symbol.y * self.scale)
        self.selected_id = element_id
        self.draw()
        return True

    def draw_selected_result(self):
        self.canvas.delete('selected_detail')
        symbol = self.scene.symbols.get(self.selected_id)
        if not symbol or not self.show_results or not self.is_dense:
            return
        item = self.index.get(symbol.item.get('id', symbol.id), symbol.item)
        text = result_text(symbol, self.results.get(item.get('id')), item=item,
                           digits=self.result_digits, include_loading=True, boxes=self.box_formats, layers=getattr(self, 'layers', None))
        if not text:
            return
        row = self.results.get(item.get('id'), {})
        if symbol.kind != 'bus':
            for end in ('from', 'to', 'hv', 'mv', 'lv'):
                if 'bus_' + end in row:
                    bus = row['bus_' + end]
                    bus_name = self.index.get(bus, {}).get('name', bus)
                    local = type(symbol)(**{**symbol.__dict__, 'item': dict(symbol.item, _local_bus=bus)})
                    end_text = result_text(local, row, item=item, digits=self.result_digits, include_loading=True, boxes=self.box_formats, layers=getattr(self, 'layers', None))
                    text += '\n' + tr('diagram.end') + ' ' + clean_label(bus_name) + '\n' + end_text
        ident = self.canvas.create_text(12, 12, anchor='nw', text=clean_label(item.get('name', symbol.id)) + '\n' + text,
            font=theme.font('body'), fill=theme.color('text'), tags='selected_detail')
        box = self.canvas.bbox(ident)
        rect = self.canvas.create_rectangle(box[0]-4, box[1]-4, box[2]+4, box[3]+4,
            fill=theme.color('surface_alt'), outline=theme.color('accent'), tags='selected_detail')
        self.canvas.tag_lower(rect, ident)

    def draw(self):
        """Redibuja con detalle adaptativo sin cambiar datos ni topología."""
        if self._paint_job is not None:
            self.after_cancel(self._paint_job)
        self._paint_job = None
        from .render import draw
        draw(self)
        self._apply_lod()
        self._refresh_cards()

    def _draw_highlight(self):
        self.canvas.delete("selection")
        if self.selected_id:
            candidates = self.canvas.find_withtag("element:" + self.selected_id)
            symbols = [item for item in candidates if "symbol" in self.canvas.gettags(item)]
            bounds = self.canvas.bbox(*(symbols or candidates)) if candidates else None
            if bounds:
                self.canvas.create_rectangle(bounds[0] - 4, bounds[1] - 4, bounds[2] + 4, bounds[3] + 4,
                    outline=theme.color('accent'), width=2, dash=(3, 2), tags="selection")

    def set_operating_state(self, model: dict) -> None:
        self.model = model
        self.index = element_index(model)
        self.gen_trip_supported = bool(model.get("capabilities", {}).get("gen_trip", False))
        self.draw()

    def set_loadflow_results(self, result: dict | None) -> None:
        self.results = {row["id"]: row for category in ("buses", "branches", "gens", "loads")
                        for row in (result or {}).get(category, [])}
        for element_id, summary in diagram_loading(self.model, result or {}).items():
            row = self.results.get(element_id)
            if row is None:
                continue
            decorated = dict(row, loading_pct=summary['loading_pct'],
                             loading_cause=summary.get('cause'), loading_rating=summary['rating'])
            for end, values in summary['ends'].items():
                for quantity, unit in (('loading','pct'),('i','ka'),('s','mva')):
                    key = quantity + '_' + end + '_' + unit
                    supplied = row.get(key)
                    decorated[key] = supplied if isinstance(supplied,(int,float)) and math.isfinite(supplied) else values.get(quantity+'_'+unit)
            self.results[element_id] = decorated
        self._rebuild_scene()
        if not self._user_zoom:
            self.fit()
        else:
            self.draw()

    def set_result_boxes_visible(self, visible: bool) -> None:
        self.show_results = bool(visible)
        self._result_var.set(self.show_results)
        self._settings.set('diagram_result_labels', self.show_results)
        self._settings.save()
        if 'result_labels' in getattr(self,'_icon_buttons',{}):
            self._refresh_icon('result_labels')
        self._rebuild_scene()
        if not self._user_zoom:
            self.fit()
        else:
            self.draw()

    def _rebuild_scene(self):
        self.scene = build_scene(self.model, self.group,
                                 result_boxes=bool(self.results) and self.show_results)
        self.scene.results = self.results
        apply_positions(self.scene, self.positions)

    def highlight(self, element_id: str | None) -> None:
        if element_id not in self.scene.symbols and self.group is None:
            element_id = next((key for key, sym in self.scene.symbols.items()
                               if element_id in sym.item.get("bus_ids", [])), element_id)
        self.selected_id = element_id
        self._draw_highlight()
        self.draw_selected_result()

    def fit(self) -> None:
        self._user_zoom = False
        left, top, right, bottom = self.scene.bounds
        w, h = max(self.canvas.winfo_width(), 200), max(self.canvas.winfo_height(), 150)
        self.scale = min(1.0, (w - 40) / max(right - left, 1), (h - 40) / max(bottom - top, 1))
        self.x_factor = max(1.0, (w - 40) / max((right - left) * self.scale, 1)) if self.group else 1.0
        self.offset = ((w - (right - left) * self.scale * self.x_factor) / 2 - left * self.scale * self.x_factor,
                       (h - (bottom - top) * self.scale) / 2 - top * self.scale)
        self.draw()

    def export_postscript(self, path: Path, *, fitted: bool = False) -> None:
        """Exporta el plano completo a escala legible, independiente del zoom."""
        if fitted:
            self.canvas.postscript(file=str(path), colormode="color", x=0, y=0,
                                   width=self.canvas.winfo_width(), height=self.canvas.winfo_height())
            return
        old_scale, old_offset, old_factor = self.scale, self.offset, self.x_factor
        try:
            self._exporting = True
            self.scale, self.offset = 1.0, (30.0, 30.0)
            self.x_factor = 1.0
            self.draw()
            bounds = self.canvas.bbox("all") or (0, 0, 900, 500)
            self.canvas.postscript(file=str(path), colormode="color", x=bounds[0] - 10,
                y=bounds[1] - 10, width=bounds[2] - bounds[0] + 20,
                height=bounds[3] - bounds[1] + 20)
        finally:
            self._exporting = False
            self.scale, self.offset, self.x_factor = old_scale, old_offset, old_factor
            self.draw()

    def _hit(self, event):
        wire_key = None
        for item_id in reversed(self.canvas.find_overlapping(event.x - 3, event.y - 3, event.x + 3, event.y + 3)):
            tags = self.canvas.gettags(item_id)
            for tag in tags:
                if tag.startswith('data_for:'):
                    return tag[len('data_for:'):]
                if tag.startswith("element:"):
                    key = tag[len("element:"):]
                    if "symbol" in tags or "result" in tags or "label" in tags:
                        return key
                    wire_key = wire_key or key
        # El interior de un símbolo hueco también es seleccionable.
        x = (event.x - self.offset[0]) / (self.scale * self.x_factor)
        y = (event.y - self.offset[1]) / self.scale
        for key, symbol in reversed(tuple(self.scene.symbols.items())):
            left, top, right, bottom = symbol.bbox
            if left <= x <= right and top <= y <= bottom:
                return key
        return wire_key

    def _press(self, event):
        if self._pin_close_hit(event):
            return
        self._hide_tooltip()
        self.canvas.focus_set()
        key = self._hit(event)
        self.highlight(key)
        if key:
            self.callbacks.on_select(key)
        self._drag = (key if key in self.scene.symbols else None, event.x, event.y, False)

    def _pan_press(self, event):
        self._hide_tooltip()
        self._drag = (None, event.x, event.y, False)

    def _motion(self, event):
        if not self._drag:
            return
        key, x, y, moved = self._drag
        dx, dy = event.x - x, event.y - y
        if key:
            self._move_symbols([key], dx/(self.scale*self.x_factor), dy/self.scale)
        else:
            self._pan_pixels(dx,dy)
        self._drag = (key, event.x, event.y, moved or bool(dx or dy))

    def _release(self, event):
        panned = self._drag and not self._drag[0] and self._drag[3]
        if self._drag and self._drag[0] and self._drag[3]:
            try:
                save_positions(self.layout_path, self.group, self.positions)
                self.layout_error = None
            except (OSError, ValueError) as exc:
                self.layout_error = str(exc)
                self.status.set(tr('diagram.write_error') + clean_label(exc))
        self._drag = None
        self._moving_wires = None
        if panned:
            self._finish_pan()

    def _wheel(self, event):
        if event.delta:
            self._zoom(event.x, event.y, 1.15 ** (event.delta / 120))
        return "break"

    def _double(self, event):
        key = self._hit(event)
        if key:
            symbol = self.scene.symbols.get(key)
            if symbol and symbol.remote_group:
                key = symbol.remote_group
                self.selected_group = key
                self.event_generate("<<DiagramOpenGroup>>")
            if key in self.scene.symbols and self.scene.symbols[key].kind == "group":
                self.selected_group = key
                self.event_generate("<<DiagramOpenGroup>>")
            self.callbacks.on_open_properties(key)

    def _keyboard_context(self,event=None):
        from types import SimpleNamespace
        key=self.selected_id
        symbol=self.scene.symbols.get(key)
        if symbol:
            x,y=self._xy(symbol.x,symbol.y)
            self._context(SimpleNamespace(x=x,y=y,x_root=self.canvas.winfo_rootx()+int(x),y_root=self.canvas.winfo_rooty()+int(y),object_id=key))
        return 'break'

    def _context(self, event):
        from .context_menu import ElementContextMenu
        key = getattr(event,'object_id',None) or self._hit(event)
        if not key:
            return
        self.highlight(key)
        if self._menu is not None:
            self._menu.destroy()
        self._hide_tooltip()
        menu = self._menu = ElementContextMenu(self,self.index.get(key,{}).get('name',key))
        self.add_context_data(menu,key)
        if key in self.scene.symbols and self.scene.symbols[key].kind == "group":
            menu.add_command(label=tr('diagram.open_group'), command=lambda: self.callbacks.on_open_properties(key))
        else:
            item = self.index.get(key, {})
            for label, action in context_actions(item, self.gen_trip_supported):
                menu.add_command(label=label, command=lambda a=action: self.callbacks.on_context_action(key, a))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
