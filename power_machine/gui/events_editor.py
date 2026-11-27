"""Editor de contingencias con validación local y del motor."""
from copy import deepcopy
import math
import tkinter as tk
from tkinter import ttk, messagebox
from .presentation import target_labels, station, element_label
from .i18n import tr, language
from .theme import theme
from .results_tables import display_value

def input_number(value):
    return float(str(value).strip().replace(',', '.'))


def screen_number(value):
    # Preserve round-trip precision in editable values, including sub-ms times.
    text=repr(input_number(value))
    if 'e' not in text.lower():
        whole,_,fraction=text.partition('.')
        text=whole+'.'+fraction.ljust(3,'0')
    return text.replace('.',',') if language()=='es' else text


def parameter_display(value):
    if isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value):
        return screen_number(value)
    return display_value(value)


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


class EventsForm:
    def build_form(self, master, document, api, root, model, index, on_apply, energized=None, preset=None, event_bus=None):
        self.document=document
        self.case_id=getattr(document,'active_case_id',None)
        self.model=model
        self.energized=energized
        self._event_bus=event_bus or getattr(master,'bus',None)
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
        self.tree.pack(fill='x', expand=False, padx=8, pady=4)
        from .panels.panel_widgets import ScrollForm
        self.form_scroll=ScrollForm(self)
        self.form_scroll.pack(fill='both',expand=True,padx=8)
        form = ttk.LabelFrame(self.form_scroll.content, text=tr('events.definition'), padding=8)
        form.pack(fill='x')
        self.form = form
        self.time = tk.StringVar(value=screen_number(1.))
        self.kind = tk.StringVar(value=kind_label((preset or {}).get('kind', 'short_circuit')))
        self.target = tk.StringVar()
        self.time_label = ttk.Label(form, text=tr('events.time'))
        self.time_label.grid(row=0, column=0, sticky='w')
        ttk.Entry(form, textvariable=self.time, width=12).grid(row=1, column=0, padx=(0, 10))
        self.kind_label_widget = ttk.Label(form, text=tr('events.type'))
        self.kind_label_widget.grid(row=0, column=1, sticky='w')
        self.kind_box = ttk.Combobox(form, textvariable=self.kind, values=[kind_label(k) for k in KINDS if k != 'gen_trip'], state='readonly', width=24)
        self.kind_box.grid(row=1, column=1, padx=(0, 10))
        self.target_label_widget = ttk.Label(form, text=tr('events.target_detail'))
        self.target_label_widget.grid(row=0, column=2, sticky='w')
        self.target_box = ttk.Combobox(form, textvariable=self.target, state='readonly', width=30)
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
        self.clearing_hint.pack(side='left',fill='x',expand=True)
        self.clearing_frame.bind('<Configure>',lambda e:self.clearing_hint.configure(wraplength=max(120,e.width-self.clearing_check.winfo_reqwidth()-130)))
        self.pending_text = tk.StringVar(self)
        self.pending_label=ttk.Label(self, textvariable=self.pending_text, wraplength=1000)
        self.pending_label.pack(side='bottom',fill='x',padx=8,pady=3,before=self.tree)
        self.bind('<Configure>',lambda e:self.pending_label.configure(wraplength=max(200,e.width-20)) if e.widget is self else None,add='+')
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
                if key in self.params: self.params[key].set(screen_number(value) if key != 'action' else str(value))
        bar = ttk.Frame(self, padding=6)
        bar.pack(side='bottom',fill='x',before=self.tree)
        self.action_bar=bar
        self.add_button = ttk.Button(bar, text=tr('events.add'), command=lambda: self.store(False))
        self.add_button.pack(side='left')
        self.add_button.configure(style='Toolbar.TButton')
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
                errors = validate_events(self.rows, document.study.options, self.index, self.energized)
                candidate = type(document.study).from_json(document.snapshot())
                candidate.events = [api.SimEvent.from_dict(row) for row in self.rows]
                errors.extend(x['message'] for x in api.validate_study(root, candidate, model=self.model) if x['level'].lower() == 'error')
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
        self._language_token=self._event_bus.subscribe('language.changed',lambda **_:self.retranslate()) if self._event_bus else None
        self.bind('<Destroy>', self._dispose, add='+')
        self.refresh()
        self._form_baseline = None if preset else self.form_signature()
        for variable in (self.time, self.target, self.kind, self.include_clearing, self.clearing_time):
            variable.trace_add('write', self.update_pending)
        self.update_pending()
        self.form_scroll.wire()
        self.bind('<FocusOut>',lambda e:self._format_numbers(),add='+')
        def wire_numeric(widget):
            if isinstance(widget,ttk.Entry):widget.bind('<FocusOut>',lambda e:self._format_numbers(),add='+')
            for child in widget.winfo_children():wire_numeric(child)
        wire_numeric(self.form)

    def _format_numbers(self):
        for variable in (self.time,self.clearing_time,*[v for k,v in self.params.items() if k!='action']):
            try:
                if variable.get().strip():
                    formatted=screen_number(variable.get())
                    if formatted!=variable.get():variable.set(formatted)
            except ValueError:pass

    def form_signature(self):
        def canonical(value):
            try:return input_number(value)
            except ValueError:return value
        return (canonical(self.time.get()), self.kind_key(), self.target.get(),
                tuple((k, v.get() if k=='action' else canonical(v.get())) for k, v in self.params.items()),
                self.include_clearing.get(), canonical(self.clearing_time.get()))

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
            var = tk.StringVar(value=default if key == 'action' else screen_number(default))
            self.params[key] = var
            var.trace_add('write', self.update_pending)
            widget = ttk.Combobox(self.param_frame, textvariable=var, values=['open', 'close'], state='readonly', width=12) if key == 'action' else ttk.Entry(self.param_frame, textvariable=var, width=12)
            widget.pack(side='left', padx=(0, 16))
            if key!='action':widget.bind('<FocusOut>',lambda e:self._format_numbers(),add='+')

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
            params = {k: ('open' if v.get() in ('open', tr('integration.open')) else 'close') if k == 'action' else input_number(v.get()) for k, v in self.params.items()}
            row = dict(time_s=input_number(self.time.get()), kind=self.kind_key(), target_id=ident, params=params)
            if not math.isfinite(row['time_s']):
                raise ValueError(tr('integration.time_must_be_finite'))
            selection = self.tree.selection()
            index = self._editing_index if self._editing_index is not None else int(selection[0]) if selection else None
            clearing = None
            if row['kind'] == 'short_circuit' and self.include_clearing.get():
                clearing_s = input_number(self.clearing_time.get())
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
        self.tree.configure(height=max(2,min(6,len(self.rows))))
        self.tree.delete(*self.tree.get_children())
        for i, row in enumerate(self.rows):
            units = {'r_ohm': tr('events.rf'), 'x_ohm': tr('events.xf'), 'dp_pct': tr('events.dp'), 'dq_pct': tr('events.dq'), 'action': tr('events.action')}
            params = ', '.join(f'{units.get(k, k)}: {tr("events." + str(v)) if k == "action" else parameter_display(v)}' for k, v in row.get('params', {}).items())
            self.tree.insert('', 'end', iid=str(i), values=(display_value(row['time_s'],'time_s'), kind_label(row['kind']), element_label(self.index.get(row['target_id'], {}), self.index), params))
        callback = getattr(self, 'on_rows_changed', None)
        if callback:
            callback(self.rows)

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
        self.time.set(screen_number(row['time_s']))
        self.target.set(self.target_labels.get(row['target_id'], ''))
        for key, value in row.get('params', {}).items():
            if key in self.params:
                self.params[key].set(screen_number(value) if key != 'action' else str(value))
        self._form_baseline = self.form_signature()
        self.update_pending()

    def retranslate(self):
        """Actualiza etiquetas sin alterar filas ni el evento que se está editando."""
        current_kind = self.kind_key()
        all_stations = self.station_filter.get() == self.station_box.cget('values')[0]
        if isinstance(self,tk.Toplevel):self.title(tr('events.title'))
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
        for variable in (self.time,self.clearing_time,*[v for k,v in self.params.items() if k!='action']):
            try:
                if variable.get().strip():
                    formatted=screen_number(variable.get())
                    if formatted!=variable.get():variable.set(formatted)
            except ValueError:pass
        self.refresh()
        self.update_pending()

    def _dispose(self, _=None):
        if _ is not None and _.widget is not self:return
        if self._event_bus and self._language_token:
            self._event_bus.unsubscribe(self._language_token)
            self._language_token=None


class EventsEditor(EventsForm,tk.Toplevel):
    def __init__(self,master,*args,**kwargs):
        tk.Toplevel.__init__(self,master)
        self.title(tr('events.title'))
        self.geometry('1080x690')
        self.minsize(880,600)
        self.transient(master)
        self.grab_set()
        self.build_form(master,*args,**kwargs)


class EmbeddedEventsEditor(EventsForm,ttk.Frame):
    def __init__(self,parent,*args,**kwargs):
        ttk.Frame.__init__(self,parent)
        self.build_form(parent,*args,**kwargs)
        self.tree.configure(height=max(2,min(6,len(self.rows))))
        for column in self.tree['columns']:
            self.tree.column(column, width=130, minwidth=60)
        self.cancel_button.configure(command=self.discard)

    def discard(self):
        self.rows=[event.to_dict() for event in self.document.study.events]
        self._editing_index=None
        self.change_kind()
        self.include_clearing.set(False)
        self.clearing_time.set('')
        self.refresh()
        self._form_baseline=self.form_signature()
        self.update_pending()

    def refresh_context(self,model,index,energized=None):
        target=next((key for key,label in self.target_labels.items() if label==self.target.get()),None)
        self.model,self.index,self.energized=model,index,energized
        self.target_labels=target_labels(index,TARGET_CLASSES[self.kind_key()])
        self.filter_targets()
        if target in self.target_labels:self.target.set(self.target_labels[target])


class EventCards(ttk.Frame):
    """Tarjetas temporales con el mismo índice que el editor, sin duplicar eventos."""
    def __init__(self, parent, on_seek, on_edit, *, compact=False):
        super().__init__(parent, padding=(8, 4), style='Panel.TFrame')
        self.on_seek, self.on_edit, self.compact = on_seek, on_edit, compact
        self.rows, self.index = [], {}
        self.widgets = []
        self._pending=None
        self.bind('<Destroy>',self._dispose_cards,add='+')

    def _schedule_seek(self,index):
        if self._pending:self.after_cancel(self._pending)
        def invoke():
            self._pending=None
            if index<len(self.rows):self.on_seek(index)
        self._pending=self.after(250,invoke)

    def _double_click(self,index):
        if self._pending:
            self.after_cancel(self._pending)
            self._pending=None
        self.on_edit(index)
        return 'break'

    def _dispose_cards(self,event):
        if event.widget is self and self._pending:
            self.after_cancel(self._pending)
            self._pending=None

    def set_events(self, rows, index):
        self.rows, self.index = [dict(row) for row in rows], index
        self.retranslate()

    def retranslate(self):
        from .time_cursor import number
        ttk.Style(self).configure('Panel.EventTime.TLabel',background=theme.color('panel'),
                                  foreground=theme.color('accent2'),font=theme.font('body'))
        if self._pending:
            self.after_cancel(self._pending)
            self._pending=None
        for child in self.winfo_children():
            child.destroy()
        self.widgets = []
        if not self.rows:
            ttk.Label(self, text=tr('v3.analysis.no_events'), style='Muted.TLabel').pack(anchor='w')
            return
        if self.compact:
            body = self
        else:
            canvas = tk.Canvas(self, height=min(3, len(self.rows))*64, highlightthickness=0,
                               background=theme.color('panel'))
            self.canvas=canvas
            scroll = ttk.Scrollbar(self, command=canvas.yview)
            canvas.configure(yscrollcommand=scroll.set)
            scroll.pack(side='right', fill='y')
            canvas.pack(side='left', fill='x', expand=True)
            body = ttk.Frame(canvas, style='Panel.TFrame')
            window = canvas.create_window(0, 0, window=body, anchor='nw')
            def layout(event):
                canvas.configure(scrollregion=canvas.bbox('all'))
                height=min(210,event.height)
                if int(canvas.cget('height'))!=height:canvas.configure(height=height)
            body.bind('<Configure>',layout)
            canvas.bind('<Configure>', lambda event: canvas.itemconfigure(window, width=event.width))
        for i, row in enumerate(self.rows):
            kind = tr('v3.analysis.event.'+row['kind'])
            target = self.index.get(row.get('target_id'), {}).get('name') or row.get('target_id', '')
            if self.compact:
                card = ttk.Button(body, text=number(row['time_s'])+' s · '+kind+' · '+target,
                                  style='Toolbar.TButton', command=lambda n=i: self._schedule_seek(n))
                card.pack(side='left', padx=(0, 6))
                card.bind('<Double-1>', lambda event, n=i: self._double_click(n))
            else:
                card = ttk.Frame(body, padding=(10, 8), style='Event.Card.TFrame',takefocus=True)
                card.grid(row=i//2,column=i%2,sticky='nsew',padx=3,pady=3)
                body.columnconfigure(i%2,weight=1,uniform='event-cards')
                time_label = ttk.Label(card, text=number(row['time_s'])+' s',
                                       style='Event.Time.TLabel', width=11)
                time_label.pack(side='left')
                ttk.Label(card, text=kind+' · '+target, style='Event.Card.TLabel').pack(side='left',fill='x',expand=True)
                def bind(widget, n=i):
                    widget.bind('<Button-1>', lambda event: self._schedule_seek(n))
                    widget.bind('<Double-1>', lambda event: self._double_click(n))
                    widget.bind('<Return>',lambda event:self.on_seek(n))
                    for child in widget.winfo_children():
                        bind(child, n)
                bind(card)
            self.widgets.append(card)
