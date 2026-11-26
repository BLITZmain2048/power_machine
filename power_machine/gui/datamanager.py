"""Administrador tabular de datos, trazable y transaccional.

Las ediciones se preparan como operaciones y sólo Document las publica.
"""
from __future__ import annotations

import csv
import io
import json
import math
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .document import operation_changes
from .events_editor import KINDS
from .presentation import element_label, station
from .results_tables import COLUMNS, format_value, display_value
from .bus import bus
from .i18n import tr
from .settings import settings
from .theme import theme


GROUPS = [
    ('buses', 'data.group.buses', None), ('branches', 'data.group.lines', 'ElmLne'),
    ('branches', 'data.group.transformers_2w', 'ElmTr2'),
    ('branches3', 'data.group.transformers_3w', None),
    ('branches', 'data.group.common_impedances', 'ElmZpu'), ('switches', 'data.group.switches', None),
    ('gens', 'data.group.synchronous_generators', 'ElmSym'), ('gens', 'data.group.static_generators', 'ElmGenstat'),
    ('gens', 'data.group.photovoltaic_systems', 'ElmPvsys'), ('loads', 'data.group.loads', None),
    ('shunts', 'data.group.shunts', None), ('controllers', 'data.group.controllers', None),
]
DATA_COLUMNS = {
    'buses': [('name', 'data.column.name'), ('substation', 'data.column.substation'), ('un_kv', 'data.column.un_kv'), ('outserv', 'data.column.outserv'), ('object_id', 'data.column.object_id')],
    'loads': [('name', 'data.column.name'), ('substation', 'data.column.substation'), ('un_kv', 'data.column.un_kv'), ('outserv', 'data.column.outserv'), ('p_mw', 'data.column.p_mw'), ('q_mvar', 'data.column.q_mvar'), ('load_scale', 'data.column.load_scale'), ('object_id', 'data.column.object_id')],
    'gens': [('name', 'data.column.name'), ('substation', 'data.column.substation'), ('un_kv', 'data.column.un_kv'), ('outserv', 'data.column.outserv'), ('gen_p_mw', 'data.column.p_mw'), ('q_mvar', 'data.column.q_mvar'), ('v_set_pu', 'data.column.v_set_pu'), ('object_id', 'data.column.object_id')],
    'branches': [('name', 'data.column.name'), ('substation', 'data.column.substation'), ('un_kv', 'data.column.un_kv'), ('outserv', 'data.column.outserv'), ('length_km', 'data.column.length_km'), ('r1_ohm', 'data.column.r1_ohm'), ('x1_ohm', 'data.column.x1_ohm'), ('loading_pct', 'data.column.loading_pct'), ('object_id', 'data.column.object_id')],
    'branches3': [('name', 'data.column.name'), ('substation', 'data.column.substation'), ('un_kv', 'data.column.un_kv'), ('outserv', 'data.column.outserv'), ('object_id', 'data.column.object_id')],
    'switches': [('name', 'data.column.name'), ('substation', 'data.column.substation'), ('un_kv', 'data.column.un_kv'), ('outserv', 'data.column.outserv'), ('closed', 'data.column.closed'), ('object_id', 'data.column.object_id')],
}
OPERATION_UNITS = {'p_mw':'MW','q_mvar':'Mvar','outserv': '1', 'closed': '1', 'load_scale': '1', 'gen_p_mw': 'MW', 'v_set_pu': 'p.u.'}
EDITABLE_BY_CLASS = {
    'ElmLod': {'outserv', 'load_scale', 'p_mw','q_mvar'}, 'ElmSym': {'outserv', 'gen_p_mw', 'v_set_pu'},
    'ElmGenstat': {'outserv', 'gen_p_mw', 'v_set_pu'}, 'ElmPvsys': {'outserv', 'gen_p_mw', 'v_set_pu'},
    'StaSwitch': {'outserv', 'closed'}, 'ElmCoup': {'outserv', 'closed'},
}


def filter_items(items, query):
    words = query.casefold().split()
    return [item for item in items if all(word in (item.get('name', '') + ' ' + item.get('id', '')).casefold() for word in words)]


def principal_value(item):
    for key, unit in (('un_kv', 'kV'), ('p_mw', 'MW')):
        if item.get(key) is not None:
            return f'{format_value(item[key])} {unit}'
    return '—'


def _value(item, *keys):
    for key in keys:
        if item.get(key) is not None:
            return item[key]
        if item.get('params', {}).get(key) is not None:
            return item['params'][key]
    return None


def _has_substation(item):
    """Distinguish an actual station from presentation's fallback text."""
    if item.get('substation') or item.get('station'):
        return True
    path = str(item.get('path', item.get('relative_path', '')))
    return '.ElmSubstat' in path


def data_rows(model, group, substation_of=station, result=None):
    """Crea filas de presentación sin cambiar el modelo."""
    result_index = {row.get('id', row.get('object_id')): row for row in (result or {}).get(group, [])}
    rows = []
    for item in model.get(group, []):
        ident = str(item.get('id', item.get('object_id', '')))
        has_station = _has_substation(item)
        row = dict(item, object_id=ident, substation=substation_of(item) if has_station else '', has_substation=has_station,
                   un_kv=_value(item, 'un_kv'), outserv=bool(item.get('outserv', False)))
        row.update(load_scale=_value(item, 'scale'), gen_p_mw=_value(item, 'p_mw'), v_set_pu=_value(item, 'v_set_pu'),
                   length_km=_value(item, 'length_km', 'dline'), r1_ohm=_value(item, 'r1_ohm', 'R1'), x1_ohm=_value(item, 'x1_ohm', 'X1'))
        flow = result_index.get(ident, {})
        row['loading_pct'] = flow.get('loading_pct', flow.get('loading_percent', flow.get('c:loading')))
        rows.append(row)
    return rows


def combined_filter(rows, text='', substation_name='', voltage_kv='', state=tr('integration.all')):
    """Filtro combinado, siempre sobre valores fuente."""
    needle, station_name, voltage = str(text).casefold().strip(), str(substation_name).casefold().strip(), str(voltage_kv).casefold().strip()
    output = []
    for row in rows:
        if needle and needle not in ' '.join(str(v) for v in row.values() if v is not None).casefold():
            continue
        if station_name and station_name != str(row.get('substation', '')).casefold():
            continue
        if voltage and voltage != str(row.get('un_kv', '')).casefold():
            continue
        outserv, closed = bool(row.get('outserv')), row.get('closed')
        # State values are canonical.  The two Spanish values keep the public
        # helper backward compatible with existing saved filters and tests.
        if state in ('in_service', 'En servicio') and outserv or state in ('out_of_service', 'Fuera de servicio') and not outserv:
            continue
        if state in ('closed', tr('integration.closed')) and (outserv or closed is not True) or state in ('open', tr('integration.open')) and (outserv or closed is not False):
            continue
        output.append(row)
    return output


def sort_rows(rows, key, reverse=False):
    present, missing = [r for r in rows if r.get(key) is not None], [r for r in rows if r.get(key) is None]
    def order(row):
        value = row[key]
        return (0, float(value)) if isinstance(value, (int, float)) and not isinstance(value, bool) else (1, str(value).casefold())
    return sorted(present, key=order, reverse=reverse) + missing


def parse_cell_value(field, value):
    if value is None or isinstance(value, str) and not value.strip():
        raise ValueError(tr('data.error.empty_cell'))
    if field in ('outserv', 'closed'):
        if isinstance(value, bool):
            return value
        text = str(value).strip().casefold()
        if text in ('1', 'sí', 'si', 'true', 'verdadero', 'yes'):
            return True
        if text in ('0', 'no', 'false', 'falso'):
            return False
        raise ValueError(tr('data.error.boolean'))
    if isinstance(value, bool):
        raise ValueError(tr('data.error.numeric_boolean'))
    raw = str(value).strip().replace(',', '.')
    unit = OPERATION_UNITS.get(field, '')
    if unit != '1' and raw.casefold().endswith(unit.casefold()):
        raw = raw[:-len(unit)].strip()
    if any(letter.isalpha() for letter in raw):
        raise ValueError(tr('data.error.unit', unit=unit))
    number = float(raw)
    if not math.isfinite(number):
        raise ValueError(tr('data.error.finite'))
    if field == 'load_scale' and number < 0:
        raise ValueError(tr('data.error.nonnegative'))
    return number


def editable_fields(row):
    return EDITABLE_BY_CLASS.get(row.get('class'), set())


def build_operations(rows, field, value, *, factor=False):
    """Construye una única propuesta de lote con precondiciones por fila."""
    if field not in OPERATION_UNITS:
        raise ValueError(tr('data.error.unsupported_field'))
    parsed = parse_cell_value(field, value)
    if factor and field in ('outserv', 'closed'):
        raise ValueError(tr('data.error.factor_boolean'))
    edits = []
    for row in rows:
        if field not in editable_fields(row):
            raise ValueError(tr('data.error.not_editable', name=str(row.get('name', row.get('object_id')))))
        current = row.get(field)
        if factor:
            if current is None or isinstance(current, bool):
                raise ValueError(tr('data.error.factor_value'))
            value_to_apply = float(current) * parsed
        else:
            value_to_apply = parsed
        edits.append(dict(object_id=row['object_id'], field=field, value=value_to_apply, unit=OPERATION_UNITS[field], expected_value=current))
    return edits


def paste_operations(text, rows, field):
    """Convierte un pegado TSV o CSV en operaciones previsualizables."""
    if not str(text).strip():
        raise ValueError(tr('data.error.empty_clipboard'))
    dialect = csv.excel_tab if '\t' in text else csv.excel
    records = [r for r in csv.reader(io.StringIO(text), dialect=dialect) if any(v.strip() for v in r)]
    by_id = {row['object_id']: row for row in rows}
    if all(len(r) == 1 for r in records):
        if len(records) != len(rows):
            raise ValueError(tr('data.error.paste_row_count'))
        pairs = zip(rows, (r[0] for r in records))
    elif all(len(r) >= 2 for r in records):
        pairs = []
        for ident, raw, *_ in records:
            if ident not in by_id:
                raise ValueError(tr('data.error.unknown_id', ident=ident))
            pairs.append((by_id[ident], raw))
    else:
        raise ValueError(tr('data.error.paste_columns'))
    edits, seen = [], set()
    for row, raw in pairs:
        if row['object_id'] in seen:
            raise ValueError(tr('data.error.duplicate_id', ident=row['object_id']))
        seen.add(row['object_id'])
        edits.extend(build_operations([row], field, raw))
    return edits


class DataManager(ttk.Frame):
    """Árbol por clase y tabla sincronizada con el unifilar.

    Los textos y las opciones de columna se reconstruyen desde claves estables
    para poder cambiar idioma y tema sin recrear la ventana de datos.
    """
    def __init__(self, master, model, substation_of, on_open, on_select, on_node=None,
                 *, event_bus=bus, settings_store=settings):
        super().__init__(master, padding=(8, 8))
        self.model, self.substation_of = model, substation_of
        self.on_open, self.on_select, self.on_node = on_open, on_select, on_node
        self.event_bus, self.settings_store = event_bus, settings_store
        self.document, self.result = None, None
        self.case_name, self.events, self.runs, self.index = tr('data.new_case'), [], [], {}
        self.current_group, self.current_class = 'loads', None
        self.sort_reverse, self.sort_key, self.column_visible = {}, None, {}
        self.query, self.station_filter, self.voltage_filter = tk.StringVar(), tk.StringVar(), tk.StringVar()
        self.state_filter, self.batch_field, self.batch_value = tk.StringVar(value='all'), tk.StringVar(), tk.StringVar()
        self.batch_mode = tk.StringVar(value='assign')
        self.heading = ttk.Label(self, text=tr('data.title'), style='Section.TLabel')
        self.heading.pack(anchor='w', pady=(0, 6))
        self._build_filters(); self._build_body(); self._build_batch_bar()
        for variable in (self.query, self.station_filter, self.voltage_filter, self.state_filter):
            variable.trace_add('write', self.schedule_filter)
        self._search_job = None
        self._subscriptions = [
            self.event_bus.subscribe('selection.changed', self._selection_from_bus),
            self.event_bus.subscribe('language.changed', self._language_changed),
            self.event_bus.subscribe('theme.changed', self._theme_changed),
        ]
        self.bind('<Destroy>', self._dispose, add='+')
        self.populate()

    def _build_filters(self):
        bar = ttk.Frame(self); bar.pack(fill='x', pady=(0, 6))
        self.filter_labels = {}
        for key, variable, width in (('text', self.query, 24), ('substation', self.station_filter, 20), ('kv', self.voltage_filter, 9)):
            label = ttk.Label(bar, text=tr('data.filter.' + key)); label.pack(side='left')
            self.filter_labels[key] = label
            widget = ttk.Entry(bar, textvariable=variable, width=width) if key == 'text' else ttk.Combobox(bar, textvariable=variable, width=width, state='readonly')
            widget.pack(side='left', padx=(3, 8))
            if key == 'substation': self.station_box = widget
            if key == 'kv': self.voltage_box = widget
        self.state_label = ttk.Label(bar, text=tr('data.filter.state')); self.state_label.pack(side='left')
        self.state_box = ttk.Combobox(bar, textvariable=self.state_filter, width=17, state='readonly'); self.state_box.pack(side='left', padx=3)
        self.clear_button = ttk.Button(bar, text=tr('data.clear_filters'), command=self.clear_filters); self.clear_button.pack(side='right')
        self._set_state_choices()

    def _build_body(self):
        pane = ttk.Panedwindow(self, orient='horizontal'); pane.pack(fill='both', expand=True)
        left, right = ttk.Frame(pane, padding=(0, 0, 8, 0)), ttk.Frame(pane)
        pane.add(left, weight=1); pane.add(right, weight=4)
        self.tree = ttk.Treeview(left, show='tree', selectmode='browse', height=18); self.tree.column('#0', width=245, minwidth=205)
        sy = ttk.Scrollbar(left, command=self.tree.yview); self.tree.configure(yscrollcommand=sy.set)
        left.rowconfigure(0, weight=1); left.columnconfigure(0, weight=1); self.tree.grid(row=0, column=0, sticky='nsew'); sy.grid(row=0, column=1, sticky='ns')
        self.tree.bind('<<TreeviewSelect>>', self._tree_selected); self.tree.bind('<Double-1>', self.open_selected)
        table_bar = ttk.Frame(right); table_bar.pack(fill='x', pady=(0, 4)); self.table_title = ttk.Label(table_bar, text=''); self.table_title.pack(side='left')
        self.export_button = ttk.Button(table_bar, text=tr('data.export_csv'), command=self.export_visible_csv); self.export_button.pack(side='right')
        self.columns_button = ttk.Menubutton(table_bar, text=tr('data.columns')); self.columns_menu = tk.Menu(self.columns_button, tearoff=False); self.columns_button.configure(menu=self.columns_menu); self.columns_button.pack(side='right', padx=(0, 6))
        body = ttk.Frame(right); body.pack(fill='both', expand=True)
        self.table = ttk.Treeview(body, show='headings', selectmode='extended'); sy = ttk.Scrollbar(body, command=self.table.yview); sx = ttk.Scrollbar(body, orient='horizontal', command=self.table.xview); self.table.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        body.rowconfigure(0, weight=1); body.columnconfigure(0, weight=1); self.table.grid(row=0, column=0, sticky='nsew'); sy.grid(row=0, column=1, sticky='ns'); sx.grid(row=1, column=0, sticky='ew')
        self.table.bind('<<TreeviewSelect>>', self._table_selected); self.table.bind('<Double-1>', self._edit_cell); self.table.bind('<Button-3>', self._context_menu)

    def _build_batch_bar(self):
        self.batch_frame = ttk.LabelFrame(self, text=tr('data.batch.title'), padding=(8, 5)); self.batch_frame.pack(fill='x', pady=(6, 0))
        self.batch_field_label = ttk.Label(self.batch_frame, text=tr('data.batch.field')); self.batch_field_label.pack(side='left'); self.batch_field_box = ttk.Combobox(self.batch_frame, textvariable=self.batch_field, state='readonly', width=23); self.batch_field_box.pack(side='left', padx=(4, 8))
        self.batch_mode_box = ttk.Combobox(self.batch_frame, textvariable=self.batch_mode, state='readonly', width=15); self.batch_mode_box.pack(side='left')
        self.batch_value_label = ttk.Label(self.batch_frame, text=tr('data.batch.value')); self.batch_value_label.pack(side='left', padx=(8, 3)); ttk.Entry(self.batch_frame, textvariable=self.batch_value, width=16).pack(side='left')
        self.apply_button = ttk.Button(self.batch_frame, text=tr('data.batch.apply'), command=self.apply_batch); self.apply_button.pack(side='left', padx=6)
        self.paste_button = ttk.Button(self.batch_frame, text=tr('data.batch.paste'), command=self.paste_preview); self.paste_button.pack(side='left')
        self.batch_status = ttk.Label(self.batch_frame, text=tr('data.batch.no_document'), style='Muted.TLabel'); self.batch_status.pack(side='right')
        self._set_batch_modes()

    def set_document(self, document):
        """Conecta el Document que posee las transacciones reversibles."""
        self.document = document; self.populate()

    def set_result(self, result):
        self.result = result; self.populate_table()

    def clear_filters(self):
        self.query.set(''); self.station_filter.set(''); self.voltage_filter.set(''); self.state_filter.set(self._state_values['all'])

    def schedule_filter(self, *_):
        if self._search_job: self.after_cancel(self._search_job)
        self._search_job = self.after(120, self.populate_table)

    def _class_rows(self):
        rows = data_rows(self.model, self.current_group, self.substation_of, self.result)
        if self.current_class:
            rows = [row for row in rows if row.get('class') == self.current_class]
        if self.sort_key:
            rows = sort_rows(rows, self.sort_key, self.sort_reverse.get(self.sort_key, False))
        return rows

    def _refresh_filter_values(self, rows):
        stations = sorted({str(r['substation']) for r in rows if r.get('substation')})
        voltages = sorted({str(r['un_kv']) for r in rows if r.get('un_kv') is not None}, key=float)
        self.station_box.configure(values=[''] + stations); self.voltage_box.configure(values=[''] + voltages)
        if self.station_filter.get() not in stations: self.station_filter.set('')
        if self.voltage_filter.get() not in voltages: self.voltage_filter.set('')

    def _set_state_choices(self):
        selected = self._state_key(self.state_filter.get())
        self._state_values = {key: tr('data.state.' + key) for key in ('all', 'in_service', 'out_of_service', 'closed', 'open')}
        self.state_box.configure(values=tuple(self._state_values.values()))
        self.state_filter.set(self._state_values[selected])

    def _state_key(self, value):
        value = str(value)
        for key in ('all', 'in_service', 'out_of_service', 'closed', 'open'):
            if value in (key, tr('data.state.' + key)):
                return key
        return 'all'

    def _set_batch_modes(self):
        current = self._batch_mode_key(self.batch_mode.get())
        self._batch_modes = {'assign': tr('data.batch.assign'), 'factor': tr('data.batch.factor')}
        self.batch_mode_box.configure(values=tuple(self._batch_modes.values()))
        self.batch_mode.set(self._batch_modes[current])

    def _batch_mode_key(self, value):
        for key in ('assign', 'factor'):
            if value in (key, tr('data.batch.' + key)):
                return key
        return 'assign'

    def _column_preferences(self):
        options = self.settings_store.get('panel_options', {}) if self.settings_store else {}
        data = options.get('data', {}) if isinstance(options, dict) else {}
        return data.get('columns', {}) if isinstance(data, dict) else {}

    def _save_column_preferences(self):
        if not self.settings_store:
            return
        options = self.settings_store.get('panel_options', {})
        options = dict(options) if isinstance(options, dict) else {}
        data = dict(options.get('data', {})) if isinstance(options.get('data', {}), dict) else {}
        data['columns'] = {key: bool(variable.get()) for key, variable in self.column_visible.items()}
        options['data'] = data
        self.settings_store.set('panel_options', options)
        try:
            self.settings_store.save()
        except OSError:
            pass

    def _build_tree(self):
        self.tree.delete(*self.tree.get_children()); self.tree.insert('', 'end', iid='network', text=tr('data.tree.network'), open=True)
        for group, title_key, cls in GROUPS:
            count = sum(1 for item in self.model.get(group, []) if cls is None or item.get('class') == cls)
            self.tree.insert('network', 'end', iid=f'group:{group}:{cls or "all"}', text=tr('data.tree.count', name=tr(title_key), count=count))
        self.tree.insert('', 'end', iid='studies', text=tr('data.tree.studies'), open=True); self.tree.insert('studies', 'end', iid='active-case', text=tr('case.base') if self.case_name in ('IEEE 39 · base case','IEEE 39 · caso base') else self.case_name)
        self.tree.insert('', 'end', iid='scenarios', text=tr('data.tree.scenarios'), open=True); self.tree.insert('', 'end', iid='variants', text=tr('data.tree.variants'), open=True)
        if self.document:
            data = self.document.to_data()
            for row in data.get('operating_scenarios', []): self.tree.insert('scenarios', 'end', iid='scenario:' + row['scenario_id'], text=tr(row['default_name_key']) if row.get('default_name_key') else tr('scenario.base') if row['name'] in ('Base operation','Operación base') else row['name'])
            for variant in data.get('network_variants', []):
                parent = 'variant:' + variant['variant_id']; self.tree.insert('variants', 'end', iid=parent, text=variant['name'], open=True)
                for stage in variant.get('stages', []): self.tree.insert(parent, 'end', iid='stage:' + stage['stage_id'], text=stage['name'])
        self.tree.insert('', 'end', iid='events', text=tr('data.tree.events'), open=True)
        for number, event in enumerate(self.events):
            item = self.index.get(getattr(event, 'target_id', ''), {}); name = f'{getattr(event, "time_s", 0):.3f} s · {tr("events.kind." + getattr(event, "kind", ""))}'
            self.tree.insert('events', 'end', iid=f'event:{number}', text=name, values=(element_label(item, self.index),))
        self.tree.insert('', 'end', iid='results', text=tr('data.tree.results'), open=True)
        for number, run in enumerate(self.runs): self.tree.insert('results', 'end', iid=f'run:{number}', text=run['name'])
        self.tree.insert('results', 'end', iid='results-compare', text=tr('data.tree.compare_runs'))

    def populate(self): self._build_tree(); self.populate_table()

    def _set_columns(self):
        columns = DATA_COLUMNS.get(self.current_group, DATA_COLUMNS['branches']); self.columns_menu.delete(0, 'end')
        previous, stored = self.column_visible, self._column_preferences()
        any_station = any(row.get('has_substation') for row in self._class_rows())
        self.column_visible = {key: previous.get(key, tk.BooleanVar(value=stored.get(key, key != 'substation' or any_station))) for key, _ in columns}
        if 'substation' in self.column_visible and not any_station and 'substation' not in stored:
            self.column_visible['substation'].set(False)
        for key, label_key in columns:
            self.columns_menu.add_checkbutton(label=tr(label_key), variable=self.column_visible[key], command=self._columns_changed)
        visible = [key for key, _ in columns if self.column_visible[key].get()]; self.table.configure(columns=visible)
        for key, label_key in columns:
            if self.column_visible[key].get():
                self.table.heading(key, text=tr(label_key), command=lambda candidate=key: self.sort(candidate)); self.table.column(key, width=230 if key == 'name' else 145 if key in ('substation', 'object_id') else 115, minwidth=80, anchor='w' if key in ('name', 'substation', 'object_id') else 'e')

    def _columns_changed(self):
        self._save_column_preferences()
        self.populate_table()

    def populate_table(self):
        self._search_job = None; rows = self._class_rows(); self._refresh_filter_values(rows)
        visible_rows = combined_filter(rows, self.query.get(), self.station_filter.get(), self.voltage_filter.get(), self._state_key(self.state_filter.get())); self._set_columns(); self.table.delete(*self.table.get_children())
        columns = [key for key, _ in DATA_COLUMNS.get(self.current_group, DATA_COLUMNS['branches']) if self.column_visible[key].get()]
        for row in visible_rows: self.table.insert('', 'end', iid=row['object_id'], values=[display_value(row.get(key), key) for key in columns])
        editable = sorted({field for row in visible_rows for field in editable_fields(row)})
        self.batch_field_box.configure(values=editable)
        if self.batch_field.get() not in editable: self.batch_field.set(editable[0] if editable else '')
        title_key = next((text for group, text, cls in GROUPS if group == self.current_group and cls == self.current_class), 'data.group.' + self.current_group)
        self.table_title.configure(text=tr('data.table_title', name=tr(title_key), count=len(visible_rows)))
        self.batch_status.configure(text=tr('data.batch.connected') if self.document and editable else tr('data.batch.no_document'))

    def export_visible_csv(self, path=None):
        """Exporta exactamente las filas y columnas visibles, sin modificar el modelo."""
        if path is None:
            path = filedialog.asksaveasfilename(parent=self, title=tr('data.export_csv'),
                defaultextension='.csv', filetypes=[(tr('data.csv_file'), '*.csv')])
        if not path:
            return None
        columns = [key for key, _ in DATA_COLUMNS.get(self.current_group, DATA_COLUMNS['branches']) if self.column_visible[key].get()]
        labels = {key: tr(label_key) for key, label_key in DATA_COLUMNS.get(self.current_group, DATA_COLUMNS['branches'])}
        try:
            with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow([labels[key] for key in columns])
                for item in self.table.get_children():
                    writer.writerow(self.table.item(item, 'values'))
        except OSError as exc:
            messagebox.showerror(tr('data.export_error_title'), str(exc), parent=self)
            return None
        return path

    def _context_menu(self, event):
        ident = self.table.identify_row(event.y)
        if not ident:
            return
        self.table.selection_set(ident)
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label=tr('data.context.open'), command=self.open_selected)
        menu.add_command(label=tr('data.context.locate'), command=lambda: self.event_bus.publish('selection.changed', ids=[ident], source='data'))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _selection_from_bus(self, ids, source=None):
        if source == 'data':
            return
        selected = [str(ident) for ident in ids if self.table.exists(str(ident))]
        if selected:
            self.table.selection_set(selected)
            self.table.focus(selected[0])
            self.table.see(selected[0])
        elif ids:
            self.locate(str(ids[0]))

    def _language_changed(self, **_):
        self.retranslate()

    def _theme_changed(self, **_):
        # ttk styles belong to Theme.  This hook makes explicitly created
        # native menus follow it when a theme changes at runtime.
        theme.apply(self.winfo_toplevel())

    def _dispose(self, event):
        if event.widget is not self:
            return
        for token in getattr(self, '_subscriptions', []):
            self.event_bus.unsubscribe(token)
        self._subscriptions = []

    def retranslate(self):
        """Refresh all visible labels while preserving selected source values."""
        state = self._state_key(self.state_filter.get())
        self.heading.configure(text=tr('data.title'))
        for key, label in self.filter_labels.items():
            label.configure(text=tr('data.filter.' + key))
        self.state_label.configure(text=tr('data.filter.state'))
        self.clear_button.configure(text=tr('data.clear_filters'))
        self.columns_button.configure(text=tr('data.columns'))
        self.export_button.configure(text=tr('data.export_csv'))
        self.batch_frame.configure(text=tr('data.batch.title'))
        self.batch_field_label.configure(text=tr('data.batch.field'))
        self.batch_value_label.configure(text=tr('data.batch.value'))
        self.apply_button.configure(text=tr('data.batch.apply'))
        self.paste_button.configure(text=tr('data.batch.paste'))
        self._set_state_choices()
        self.state_filter.set(self._state_values[state])
        self._set_batch_modes()
        self.populate()

    def sort(self, key):
        if self.sort_key == key:
            self.sort_reverse[key] = not self.sort_reverse.get(key, False)
        else:
            self.sort_key = key
            self.sort_reverse[key] = False
        self.populate_table()

    def _tree_selected(self, _=None):
        selection = self.tree.selection()
        if not selection: return
        node = selection[0]
        if node.startswith('group:'):
            _prefix, self.current_group, selected_class = node.split(':', 2)
            self.current_class = None if selected_class == 'all' else selected_class
            self.sort_key = None
            self.populate_table()
        elif self.on_node: self.on_node(node)

    def _table_selected(self, _=None):
        selected = self.table.selection()
        if selected:
            self.event_bus.publish('selection.changed', ids=list(selected), source='data')
            self.on_select(selected[0])

    def selected_rows(self):
        by_id = {row['object_id']: row for row in self._class_rows()}; return [by_id[i] for i in self.table.selection() if i in by_id]

    def _edit_cell(self, event):
        ident, column_id = self.table.identify_row(event.y), self.table.identify_column(event.x)
        if not ident or not column_id: return
        fields = [key for key, _ in DATA_COLUMNS.get(self.current_group, []) if self.column_visible[key].get()]; field = fields[int(column_id[1:]) - 1]
        row = next((candidate for candidate in self._class_rows() if candidate['object_id'] == ident), None)
        if not row or field not in editable_fields(row): return
        bbox = self.table.bbox(ident, column_id)
        if not bbox: return
        value = tk.StringVar(value=str(row.get(field, ''))); entry = ttk.Entry(self.table, textvariable=value); entry.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3]); entry.focus_set(); entry.select_range(0, 'end')
        completed = [False]
        def finish(commit=True):
            if completed[0]:return
            completed[0]=True
            entry.destroy()
            if commit:
                try: self.apply_edits(build_operations([row], field, value.get()), tr('data.edit_cell'))
                except ValueError as exc: messagebox.showerror(tr('data.invalid_edit'), str(exc), parent=self)
        entry.bind('<Return>', lambda _event: finish()); entry.bind('<Escape>', lambda _event: finish(False)); entry.bind('<FocusOut>', lambda _event: finish())

    def apply_edits(self, edits, label):
        """Llama una sola vez a Document.apply_operations para el lote completo."""
        if not self.document:
            messagebox.showwarning(tr('data.edit_unavailable'), tr('data.connect_document'), parent=self); return None
        result = self.document.apply_operations(edits, self.document.revision, label)
        if not result.get('accepted'):
            messagebox.showerror(tr('data.operation_rejected'), '\n'.join(result.get('diagnostics', [tr('data.no_diagnostic')])), parent=self); return result
        self.event_bus.publish('network.edited', ids=result.get('changed_ids', []))
        self.populate_table(); return result

    def apply_batch(self):
        rows = self.selected_rows()
        if not rows:
            messagebox.showwarning(tr('data.no_selection'), tr('data.select_rows'), parent=self); return
        try: edits = build_operations(rows, self.batch_field.get(), self.batch_value.get(), factor=self._batch_mode_key(self.batch_mode.get()) == 'factor')
        except ValueError as exc: messagebox.showerror(tr('data.invalid_edit'), str(exc), parent=self); return
        self.apply_edits(edits, tr('data.batch_edit'))

    def paste_preview(self):
        rows, field = self.selected_rows(), self.batch_field.get()
        if not rows or not field:
            messagebox.showwarning(tr('data.no_selection'), tr('data.select_rows_field'), parent=self); return
        window = tk.Toplevel(self); window.title(tr('data.paste_preview')); window.transient(self.winfo_toplevel()); window.geometry('720x420')
        ttk.Label(window, text=tr('data.paste_instruction')).pack(anchor='w', padx=10, pady=(10, 4)); source = tk.Text(window, height=8, width=80, font=theme.font('mono')); source.pack(fill='x', padx=10)
        preview = ttk.Treeview(window, columns=('id', 'field', 'value', 'unit'), show='headings')
        for key, label_key in (('id', 'data.column.object_id'), ('field', 'data.batch.field'), ('value', 'data.batch.value'), ('unit', 'data.unit')): preview.heading(key, text=tr(label_key)); preview.column(key, width=160)
        preview.pack(fill='both', expand=True, padx=10, pady=8); state = {'edits': None}
        def refresh():
            try: edits = paste_operations(source.get('1.0', 'end-1c'), rows, field)
            except ValueError as exc: state['edits'] = None; messagebox.showerror(tr('data.invalid_paste'), str(exc), parent=window); return
            state['edits'] = edits; preview.delete(*preview.get_children())
            for edit in edits: preview.insert('', 'end', values=(edit['object_id'], edit['field'], edit['value'], edit['unit']))
        def apply():
            if state['edits'] is None: refresh()
            if state['edits']:
                result = self.apply_edits(state['edits'], tr('data.paste_batch'))
                if result and result.get('accepted'): window.destroy()
        buttons = ttk.Frame(window); buttons.pack(fill='x', padx=10, pady=(0, 10)); ttk.Button(buttons, text=tr('data.refresh_preview'), command=refresh).pack(side='right'); ttk.Button(buttons, text=tr('data.apply_transaction'), command=apply).pack(side='right', padx=6)

    def open_selected(self, _=None):
        selected = self.table.selection()
        if selected: self.on_open(selected[0]); return
        selected = self.tree.selection()
        if selected and self.on_node: self.on_node(selected[0])

    def select(self, _=None): self._table_selected()

    def locate(self, ident):
        """Recibe selección del unifilar y la refleja usando object_id."""
        for group, _title, cls in GROUPS:
            if any(str(item.get('id')) == ident for item in self.model.get(group, [])):
                item = next(item for item in self.model[group] if str(item.get('id')) == ident)
                self.current_group = group
                self.current_class = item.get('class') if cls is not None else None
                self.populate_table()
                if self.table.exists(ident): self.table.selection_set(ident); self.table.focus(ident); self.table.see(ident)
                return


def readonly_table(master, rows):
    frame = ttk.Frame(master, padding=10); frame.pack(fill='both', expand=True)
    tree = ttk.Treeview(frame, columns=('value', 'unit'), show='tree headings'); tree.heading('#0', text=tr('data.parameter')); tree.heading('value', text=tr('data.batch.value')); tree.heading('unit', text=tr('data.unit')); tree.column('#0', width=210); tree.column('value', width=360); tree.column('unit', width=80)
    scroll = ttk.Scrollbar(frame, command=tree.yview); horizontal = ttk.Scrollbar(frame, orient='horizontal', command=tree.xview); tree.configure(yscrollcommand=scroll.set, xscrollcommand=horizontal.set); frame.rowconfigure(0, weight=1); frame.columnconfigure(0, weight=1); scroll.grid(row=0, column=1, sticky='ns'); horizontal.grid(row=1, column=0, sticky='ew'); tree.grid(row=0, column=0, sticky='nsew')
    for label, value, unit in rows:
        if isinstance(value, (dict, list)): value = json.dumps(value, ensure_ascii=False)
        tree.insert('', 'end', text=label, values=(format_value(value), unit))
    return tree


class PropertiesDialog(tk.Toplevel):
    def __init__(self, master, item, document, result, on_apply, editable=True):
        super().__init__(master); self.title(tr('data.properties_title', name=item.get('name', item['id']))); self.geometry('800x540'); self.minsize(620, 400); self.transient(master); self.grab_set()
        book = ttk.Notebook(self); book.pack(fill='both', expand=True, padx=10, pady=10); basic, operation, results = [ttk.Frame(book) for _ in range(3)]
        for frame, name_key in ((basic, 'data.properties.basic'), (operation, 'data.properties.operation'), (results, 'data.tree.results')): book.add(frame, text=tr(name_key))
        rows = [(tr('data.column.object_id'), item['id'], ''), (tr('data.properties.path'), item.get('path', item.get('relative_path')), ''), (tr('data.properties.class'), item.get('class'), '')]
        for block, units_key in (('params', 'units'), ('type_params', 'type_units')):
            for key, value in sorted(item.get(block, {}).items()): rows.append(((tr('data.properties.type_prefix') if block == 'type_params' else '') + key, value, item.get(units_key, {}).get(key, tr('data.properties.unknown_unit'))))
        readonly_table(basic, rows); result_rows = []
        for group, columns in COLUMNS.items():
            for row in (result or {}).get(group, []):
                if row.get('id') == item['id']: result_rows.extend((label, row.get(key), '') for key, label in columns)
        readonly_table(results, result_rows or [(tr('data.properties.last_loadflow'), tr('data.properties.no_results'), '')]); ttk.Label(operation, text=tr('data.properties.saved_changes'), style='Section.TLabel').pack(anchor='w', padx=18, pady=18); self.fields = {}
        defaults = [('outserv', tr('data.column.outserv'), bool(item.get('outserv', False)))]
        if item.get('group') == 'controllers': defaults = []
        if item.get('class') in ('StaSwitch', 'ElmCoup'): defaults.append(('closed', tr('data.properties.switch_closed'), bool(item.get('closed', True))))
        if item.get('class') == 'ElmLod': defaults.append(('load_scale', tr('data.column.load_scale'), item.get('scale', 1)))
        if item.get('class') in ('ElmSym', 'ElmGenstat', 'ElmPvsys'): defaults.extend([('gen_p_mw', tr('data.column.p_mw'), item.get('p_mw', 0)), ('v_set_pu', tr('data.column.v_set_pu'), item.get('v_set_pu', 1))])
        for key, label, default in defaults:
            value = document.study.scenario.get(key, {}).get(item['id'], default); row = ttk.Frame(operation, padding=(18, 6)); row.pack(fill='x')
            if isinstance(default, bool): var = tk.BooleanVar(value=value); widget = ttk.Checkbutton(row, text=label, variable=var); widget.pack(anchor='w')
            else: var = tk.StringVar(value=str(value)); ttk.Label(row, text=label, width=32).pack(side='left'); widget = ttk.Entry(row, textvariable=var, width=18); widget.pack(side='left')
            if not editable: widget.configure(state='disabled')
            self.fields[key] = var
        def apply():
            try: document.operation(item, operation_changes(item, {key: value.get() for key, value in self.fields.items()})); on_apply(); self.destroy()
            except (ValueError, RuntimeError) as exc: messagebox.showerror(tr('data.invalid_edit'), str(exc), parent=self)
        buttons = ttk.Frame(self, padding=(10, 0, 10, 10)); buttons.pack(fill='x'); ttk.Button(buttons, text=tr('data.close'), command=self.destroy).pack(side='right'); ttk.Button(buttons, text=tr('data.apply'), command=apply, state='normal' if editable and defaults else 'disabled').pack(side='right', padx=8)
