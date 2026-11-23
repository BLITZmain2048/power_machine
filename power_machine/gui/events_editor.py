"""Editor de contingencias con validación local y del motor."""
from copy import deepcopy
import math
import tkinter as tk
from tkinter import ttk, messagebox
from .presentation import target_labels, station, element_label
from .i18n import tr
from .theme import theme

# Compatibilidad con el administrador de datos mientras migra sus propios rótulos.
KINDS = {
    'short_circuit': tr('integration.three_phase_short_circuit'), 'clear_short_circuit': tr('integration.clear_short_circuit'),
    'switch_element': tr('integration.element_switching'), 'switch': tr('integration.breaker_switching'),
    'load_step': tr('integration.load_step'), 'gen_trip': tr('integration.generator_trip'),
}
TARGET_CLASSES = {
    'short_circuit': {'ElmTerm'}, 'clear_short_circuit': {'ElmTerm'},
    'switch_element': {'ElmLne', 'ElmTr2', 'ElmTr3', 'ElmZpu'},
    'switch': {'StaSwitch', 'ElmCoup'}, 'load_step': {'ElmLod'},
    'gen_trip': {'ElmSym', 'ElmGenstat', 'ElmPvsys'},
}


def kind_label(kind):
    return tr('events.kind.' + kind)


def validate_events(events, options, index, energized=None, gen_trip_supported=False):
    errors = []
    active_faults = set()
    for event in sorted(events, key=lambda e: float(e.get('time_s', 0))):
        kind, ident = event.get('kind'), event.get('target_id')
        time_s = event.get('time_s')
        if not isinstance(time_s, (int, float)) or not math.isfinite(time_s) or not options.t_start_s <= time_s <= options.t_end_s:
            errors.append(tr('integration.event_time_outside_the_simulation_s'))
        item = index.get(ident)
        if not item:
            errors.append(tr('integration.target_does_not_exist'))
            continue
        cls = item.get('class', 'ElmTerm' if item.get('group') == 'buses' else '')
        if cls not in TARGET_CLASSES.get(kind, set()):
            errors.append(tr('integration.event_type_is_incompatible_with_the_target'))
        if item.get('outserv') or energized is not None and ident in energized and not energized[ident]:
            errors.append(tr('integration.the_target_is_out_of_service_or'))
        params = event.get('params', {})
        if kind == 'gen_trip' and not gen_trip_supported:
            errors.append(tr('integration.generator_disconnection_unavailable_without_demonstrated_engine_support'))
        if kind == 'short_circuit':
            if ident in active_faults:
                errors.append(tr('integration.a_fault_is_already_active_on_this'))
            active_faults.add(ident)
            for key in ('r_ohm', 'x_ohm'):
                value = params.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or key == 'r_ohm' and value < 0:
                    errors.append(tr('integration.fault_impedance_must_be_finite_and_rf'))
        if kind == 'clear_short_circuit':
            if ident not in active_faults:
                errors.append(tr('integration.fault_clearing_without_a_preceding_fault_on'))
            active_faults.discard(ident)
        if kind in ('switch', 'switch_element') and params.get('action') not in ('open', 'close'):
            errors.append(tr('integration.select_open_or_close'))
        if kind == 'load_step':
            for key in ('dp_pct', 'dq_pct'):
                value = params.get(key)
                if not isinstance(value, (int, float)) or not math.isfinite(value) or value < -100:
                    errors.append(tr('integration.the_step_must_be_finite_and_at'))
    return list(dict.fromkeys(errors))


class EventsEditor(tk.Toplevel):
    def __init__(self, master, document, api, root, model, index, on_apply, energized=None, preset=None):
        super().__init__(master)
        self.title(tr('events.title'))
        self.geometry('1080x690')
        self.minsize(880, 600)
        self.transient(master)
        self.grab_set()
        self._applied_rows = None
        self._form_baseline = None
        self._editing_index = None
        self._kind_code = (preset or {}).get('kind', 'short_circuit')
        self.rows = [event.to_dict() for event in document.study.events]
        self.index = index
        self.tree = ttk.Treeview(self, columns=('time', 'kind', 'target', 'params'), show='headings', height=9)
        self._heading_keys = {'time': 'events.time', 'kind': 'events.type', 'target': 'events.target', 'params': 'events.parameters'}
        for key, width in [('time', 90), ('kind', 220), ('target', 280), ('params', 320)]:
            self.tree.heading(key, text=tr(self._heading_keys[key]))
            if key == 'time':
                self.tree.heading(key, command=self.sort)
            self.tree.column(key, width=width)
        self.tree.pack(fill='both', expand=True, padx=12, pady=12)
        form = ttk.LabelFrame(self, text=tr('events.definition'), padding=theme.pad_l)
        form.pack(fill='x', padx=12)
        self.form = form
        self.time = tk.StringVar(value='1.0')
        self.kind = tk.StringVar(value=kind_label((preset or {}).get('kind', 'short_circuit')))
        self.target = tk.StringVar()
        self.time_label = ttk.Label(form, text=tr('events.time'))
        self.time_label.grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=self.time, width=12).grid(row=1, column=0, padx=(0, 10))
        self.kind_label_widget = ttk.Label(form, text=tr('events.type'))
        self.kind_label_widget.grid(row=0, column=1, sticky='w')
        self.kind_box = ttk.Combobox(form, textvariable=self.kind, values=[kind_label(k) for k in KINDS if k != 'gen_trip'], state='readonly', width=29)
        self.kind_box.grid(row=1, column=1, padx=(0, 10))
        self.target_label_widget = ttk.Label(form, text=tr('events.target_detail'))
        self.target_label_widget.grid(row=0, column=2, sticky='w')
        self.target_box = ttk.Combobox(form, textvariable=self.target, state='readonly', width=65)
        self.target_box.grid(row=1, column=2, sticky='ew')
        form.columnconfigure(2, weight=1)
        self.param_frame = ttk.Frame(form)
        self.param_frame.grid(row=3, column=0, columnspan=3, sticky='ew', pady=(12, 0))
        self.clearing_frame = ttk.Frame(form)
        self.clearing_frame.grid(row=4, column=0, columnspan=3, sticky='ew', pady=8)
        self.include_clearing = tk.BooleanVar(self, False)
        self.clearing_time = tk.StringVar(self, '')
        self.clearing_check = ttk.Checkbutton(self.clearing_frame, text=tr('int2.propose_clear'),
                                             variable=self.include_clearing)
        self.clearing_check.pack(side='left')
        self.clearing_entry = ttk.Entry(self.clearing_frame, textvariable=self.clearing_time, width=12)
        self.clearing_entry.pack(side='left', padx=8)
        self.clearing_hint = ttk.Label(self.clearing_frame, text=tr('int2.clear_hint'), wraplength=480)
        self.clearing_hint.pack(side='left')
        self.pending_text = tk.StringVar(self)
        ttk.Label(self, textvariable=self.pending_text, wraplength=1000).pack(fill='x', padx=12, pady=(6,0))
        filters = ttk.Frame(form)
        filters.grid(row=2, column=0, columnspan=3, sticky='ew', pady=8)
        self.query = tk.StringVar()
        self.station_filter = tk.StringVar(value=tr('events.all_stations'))
        self.search_label = ttk.Label(filters, text=tr('events.search_target'))
        self.search_label.pack(side='left')
        ttk.Entry(filters, textvariable=self.query, width=28).pack(side='left', padx=8)
        self.station_label = ttk.Label(filters, text=tr('events.station'))
        self.station_label.pack(side='left')
        self.station_box = ttk.Combobox(filters, textvariable=self.station_filter, values=[tr('events.all_stations')] + sorted({station(i) for i in index.values() if station(i)}), state='readonly', width=40)
        self.station_box.pack(side='left', padx=8)
        self.query.trace_add('write', self.filter_targets)
        self.station_filter.trace_add('write', self.filter_targets)
        self.params = {}
        self.kind_box.bind('<<ComboboxSelected>>', self.change_kind)
        self.tree.bind('<<TreeviewSelect>>', self.load_selection)
        self.change_kind()
        if preset:
            self.target.set(self.target_labels.get(preset.get('target_id'), ''))
            for key, value in preset.get('params', {}).items():
                if key in self.params: self.params[key].set(str(value))
        bar = ttk.Frame(self, padding=12)
        bar.pack(fill='x')
        self.add_button = ttk.Button(bar, text=tr('events.add'), command=lambda: self.store(False))
        self.add_button.pack(side='left')
        self.add_button.configure(style='Accent.TButton')
        self.update_button = ttk.Button(bar, text=tr('events.update'), command=lambda: self.store(True))
        self.update_button.pack(side='left', padx=6)
        self.delete_button = ttk.Button(bar, text=tr('events.delete'), command=self.delete)
        self.delete_button.pack(side='left')
        self.cancel_button = ttk.Button(bar, text=tr('events.cancel'), command=self.destroy)
        self.cancel_button.pack(side='right')
        def apply():
            try:
                if self.form_pending() and not self.store(self._editing_index is not None):
                    return
                errors = validate_events(self.rows, document.study.options, index, energized)
                candidate = type(document.study).from_json(document.snapshot())
                candidate.events = [api.SimEvent.from_dict(row) for row in self.rows]
                errors.extend(x['message'] for x in api.validate_study(root, candidate, model=model) if x['level'].lower() == 'error')
                if errors:
                    raise ValueError('\n'.join(dict.fromkeys(errors)))
                document.edit(lambda study: setattr(study, 'events', candidate.events))
                on_apply()
                self._form_baseline = self.form_signature()
                self._applied_rows = deepcopy(self.rows)
                self.update_pending()
            except (ValueError, TypeError) as exc:
                messagebox.showerror(tr('events.invalid'), str(exc).replace(';', ','), parent=self)
        self.apply_button = ttk.Button(bar, text=tr('events.apply'), command=apply)
        self.apply_button.pack(side='right', padx=6)
        self._language_token = getattr(master, 'bus', None).subscribe('language.changed', lambda **_: self.retranslate()) if getattr(master, 'bus', None) else None
        self.bind('<Destroy>', self._dispose, add='+')
        self.refresh()
        self._form_baseline = None if preset else self.form_signature()
        for variable in (self.time, self.target, self.kind, self.include_clearing, self.clearing_time):
            variable.trace_add('write', self.update_pending)
        self.update_pending()

    def form_signature(self):
        return (self.time.get(), self.kind_key(), self.target.get(),
                tuple((k, v.get()) for k, v in self.params.items()),
                self.include_clearing.get(), self.clearing_time.get())

    def form_pending(self):
        return self._form_baseline != self.form_signature()

    def update_pending(self, *_):
        if hasattr(self, 'pending_text'):
            key = 'int2.event_pending' if self.form_pending() else (
                'int2.events_applied' if self._applied_rows == self.rows else 'int2.events_ready')
            self.pending_text.set(tr(key, count=len(self.rows)))

    def kind_key(self):
        return next((k for k in KINDS if kind_label(k) == self.kind.get()), self._kind_code)

    def change_kind(self, _=None):
        kind = self.kind_key()
        self._kind_code = kind
        if hasattr(self, 'clearing_frame'):
            self.clearing_frame.grid() if kind == 'short_circuit' else self.clearing_frame.grid_remove()
        self.target_labels = target_labels(self.index, TARGET_CLASSES[kind])
        self.filter_targets()
        for widget in self.param_frame.winfo_children():
            widget.destroy()
        self.params = {}
        fields = {'short_circuit': [('r_ohm', 'events.rf', '0'), ('x_ohm', 'events.xf', '0')],
                  'load_step': [('dp_pct', 'events.dp', '10'), ('dq_pct', 'events.dq', '0')],
                  'switch_element': [('action', 'events.action', 'open')], 'switch': [('action', 'events.action', 'open')]}.get(kind, [])
        for key, label, default in fields:
            ttk.Label(self.param_frame, text=tr(label)).pack(side='left', padx=(0, 6))
            var = tk.StringVar(value=default)
            self.params[key] = var
            var.trace_add('write', self.update_pending)
            widget = ttk.Combobox(self.param_frame, textvariable=var, values=['open', 'close'], state='readonly', width=12) if key == 'action' else ttk.Entry(self.param_frame, textvariable=var, width=12)
            widget.pack(side='left', padx=(0, 16))

    def filter_targets(self, *_):
        labels = sorted(label for ident, label in getattr(self, 'target_labels', {}).items()
                        if all(w in label.casefold() for w in self.query.get().casefold().split())
                        and (self.station_filter.get() == tr('events.all_stations') or station(self.index[ident]) == self.station_filter.get()))
        self.target_box.configure(values=labels)
        if self.target.get() not in labels:
            self.target.set(labels[0] if labels else '')

    def store(self, update):
        try:
            ident = next(k for k, v in self.target_labels.items() if v == self.target.get())
            params = {k: ('open' if v.get() in ('open', tr('integration.open')) else 'close') if k == 'action' else float(v.get()) for k, v in self.params.items()}
            row = dict(time_s=float(self.time.get()), kind=self.kind_key(), target_id=ident, params=params)
            if not math.isfinite(row['time_s']):
                raise ValueError(tr('integration.time_must_be_finite'))
            selection = self.tree.selection()
            index = self._editing_index if self._editing_index is not None else int(selection[0]) if selection else None
            clearing = None
            if row['kind'] == 'short_circuit' and self.include_clearing.get():
                clearing_s = float(self.clearing_time.get())
                if not math.isfinite(clearing_s) or clearing_s <= row['time_s']:
                    raise ValueError(tr('int2.clear_after_fault'))
                clearing = dict(time_s=clearing_s, kind='clear_short_circuit', target_id=ident, params={})
            if update:
                if index is None:
                    raise ValueError(tr('integration.select_an_event'))
                self.rows[index] = row
            else:
                self.rows.append(row)
            if clearing and clearing not in self.rows:
                self.rows.append(clearing)
            self._editing_index = None
            self._form_baseline = self.form_signature()
            self.sort()
            self.update_pending()
            return True
        except (ValueError, StopIteration) as exc:
            messagebox.showerror(tr('events.invalid'), str(exc) or tr('events.select_target'), parent=self)
            return False

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            units = {'r_ohm': tr('events.rf'), 'x_ohm': tr('events.xf'), 'dp_pct': tr('events.dp'), 'dq_pct': tr('events.dq'), 'action': tr('events.action')}
            params = ', '.join(f'{units.get(k, k)}: {tr("events." + str(v)) if k == "action" else v}' for k, v in row.get('params', {}).items())
            self.tree.insert('', 'end', iid=str(i), values=(row['time_s'], kind_label(row['kind']), element_label(self.index.get(row['target_id'], {}), self.index), params))

    def sort(self):
        self.rows.sort(key=lambda row: row['time_s'])
        self.refresh()

    def delete(self):
        for index in sorted(map(int, self.tree.selection()), reverse=True):
            self.rows.pop(index)
        self.refresh()

    def load_selection(self, _=None):
        selection = self.tree.selection()
        if not selection:
            return
        self._editing_index = int(selection[0])
        row = self.rows[self._editing_index]
        self.include_clearing.set(False)
        self.query.set('')
        self.station_filter.set(tr('events.all_stations'))
        self.kind.set(kind_label(row['kind']))
        self.change_kind()
        self.time.set(str(row['time_s']))
        self.target.set(self.target_labels.get(row['target_id'], ''))
        for key, value in row.get('params', {}).items():
            if key in self.params:
                self.params[key].set(str(value))
        self._form_baseline = self.form_signature()
        self.update_pending()

    def retranslate(self):
        """Actualiza etiquetas sin alterar filas ni el evento que se está editando."""
        current_kind = self.kind_key()
        all_stations = self.station_filter.get() == self.station_box.cget('values')[0]
        self.title(tr('events.title'))
        self.form.configure(text=tr('events.definition'))
        for key, text_key in self._heading_keys.items():
            self.tree.heading(key, text=tr(text_key))
        self.time_label.configure(text=tr('events.time'))
        self.kind_label_widget.configure(text=tr('events.type'))
        self.target_label_widget.configure(text=tr('events.target_detail'))
        self.search_label.configure(text=tr('events.search_target'))
        self.station_label.configure(text=tr('events.station'))
        self.kind_box.configure(values=[kind_label(k) for k in KINDS if k != 'gen_trip'])
        self.station_box.configure(values=[tr('events.all_stations')] + sorted({station(i) for i in self.index.values() if station(i)}))
        if all_stations:
            self.station_filter.set(tr('events.all_stations'))
        self.add_button.configure(text=tr('events.add'))
        self.update_button.configure(text=tr('events.update'))
        self.delete_button.configure(text=tr('events.delete'))
        self.cancel_button.configure(text=tr('events.cancel'))
        self.apply_button.configure(text=tr('events.apply'))
        self.kind.set(kind_label(current_kind))
        self.clearing_check.configure(text=tr('int2.propose_clear'))
        self.clearing_hint.configure(text=tr('int2.clear_hint'))
        self.refresh()
        self.update_pending()

    def _dispose(self, _=None):
        bus = getattr(self.master, 'bus', None)
        if bus and self._language_token:
            bus.unsubscribe(self._language_token)
