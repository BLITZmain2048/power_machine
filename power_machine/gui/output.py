"""Ventana de salida estructurada, compatible con el registro de texto heredado."""
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import tkinter as tk
from tkinter import filedialog, ttk

from .presentation import human_message
from .display import public_text
from .theme import theme
from .bus import bus
from .i18n import tr
from .results_tables import bind_translations



OUTPUT_SCHEMA = 'power_machine-output-entry-v1'
LEVELS = ('INFO', 'AVISO', 'ERROR')


def clean_text(text):
    """Normaliza texto que se muestra en controles Tk."""
    return str(text).replace(';', ',')


def normalize_level(level):
    """Canonical protocol levels are independent of the display language."""
    value = str(level or 'INFO').upper()
    return {'WARNING':'AVISO','WARN':'AVISO'}.get(value,value if value in LEVELS else 'INFO')


def normalize_element_ref(reference):
    """Normaliza una referencia trazable de PowerFactory sin inferir nombres."""
    if not isinstance(reference, dict):
        raise ValueError('La referencia de elemento debe ser un diccionario')
    object_id = reference.get('object_id', reference.get('id'))
    if not object_id:
        raise ValueError('La referencia de elemento requiere object_id')
    result = {
        'object_id': str(object_id),
        'relative_path': reference.get('relative_path', reference.get('path')),
        'origin': reference.get('origin', 'model'),
    }
    if reference.get('terminal') is not None:
        result['terminal'] = reference['terminal']
    return result


def make_output_entry(level, message, *, module='gui', code='legacy.text', case_id=None,
                      run_id=None, element_refs=None, details=None, timestamp_utc=None,
                      entry_id=None):
    """Crea una entrada ``power_machine-output-entry-v1`` con hora UTC explícita."""
    stamp = timestamp_utc or datetime.now(timezone.utc).isoformat()
    return {
        'schema': OUTPUT_SCHEMA,
        'entry_id': entry_id or str(uuid4()),
        'timestamp_utc': str(stamp),
        'level': normalize_level(level),
        'case_id': case_id,
        'run_id': run_id,
        'module': str(module or 'gui'),
        'code': str(code or 'legacy.text'),
        'message': clean_text(message),
        'element_refs': [normalize_element_ref(item) for item in (element_refs or ())],
        'details': dict(details or {}),
    }


def output_group_key(entry):
    """Identidad de repetición acotada a corrida, código y objetivo exacto."""
    targets = tuple(ref['object_id'] for ref in entry['element_refs'])
    return entry['run_id'], entry['code'], targets, entry['message']


def entry_matches(entry, level=tr('integration.all'), module='', text=''):
    """Indica si la entrada pasa los filtros visibles, sin alterar su contenido."""
    if level not in ('', tr('integration.all'), 'All', tr('output.all')) and entry['level'] != normalize_level(level):
        return False
    if module and module.casefold() not in entry['module'].casefold():
        return False
    needle = text.casefold().strip()
    if not needle:
        return True
    haystack = ' '.join((entry['message'], entry['code'], entry['module'], entry.get('case_id') or '',
                         entry.get('run_id') or '', *[ref['object_id'] for ref in entry['element_refs']]))
    return needle in haystack.casefold()


class OutputPanel(ttk.Frame):
    """Registro filtrable que mantiene el widget ``text`` de la interfaz previa."""
    def __init__(self, master, select_callback=None):
        super().__init__(master, padding=(8, 4))
        self.index = {}
        self.seen = set()
        self.model_warnings = {}
        self._summary_line = None
        self._records = []
        self._groups = {}
        self._line_entries = {}
        self._select_callback = select_callback
        self.level_filter = tk.StringVar(value=tr('output.all'))
        self.module_filter = tk.StringVar(value='')
        self.text_filter = tk.StringVar(value='')

        bar = ttk.Frame(self)
        bar.pack(fill='x')
        ttk.Label(bar, text=tr('output.output'), style='Section.TLabel').pack(side='left')
        ttk.Button(bar, text=tr('output.save'), command=self.save_dialog).pack(side='right')
        ttk.Button(bar, text=tr('output.clear'), command=self.clear).pack(side='right', padx=4)
        ttk.Button(bar, text=tr('output.copy'), command=self.copy).pack(side='right', padx=4)

        filters = ttk.Frame(self)
        filters.pack(fill='x', pady=(3, 4))
        ttk.Label(filters, text=tr('output.level')).pack(side='left')
        self.level_combo = ttk.Combobox(filters, textvariable=self.level_filter, values=(tr('output.all'), 'INFO', tr('output.warning'), 'ERROR'),
                     width=9, state='readonly')
        self.level_combo.pack(side='left', padx=(4, 8))
        self._level_labels = tuple(self.level_combo['values'])
        ttk.Label(filters, text=tr('output.module')).pack(side='left')
        ttk.Entry(filters, textvariable=self.module_filter, width=14).pack(side='left', padx=(4, 8))
        ttk.Label(filters, text=tr('output.text')).pack(side='left')
        ttk.Entry(filters, textvariable=self.text_filter).pack(side='left', padx=(4, 0), fill='x', expand=True)
        for variable in (self.level_filter, self.module_filter, self.text_filter):
            variable.trace_add('write', self._apply_filters)

        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        self.text = tk.Text(body, height=7, wrap='word', state='disabled', font=theme.font('mono'),
                            background=theme.color('surface'), foreground=theme.color('text'), relief='flat')
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.text.pack(fill='both', expand=True)
        self.text.tag_configure('ERROR', foreground=theme.color('accent'))
        self.text.tag_configure('AVISO', foreground=theme.color('accent'))
        self.text.tag_configure('INFO', foreground=theme.color('accent'))
        self.text.bind('<Double-1>', self._select_from_text, add='+')

        self._apply_theme()
        self._theme_subscription = bus.subscribe('theme.changed', self._apply_theme)
        self.bind('<Destroy>', self._dispose, add='+')
        bind_translations(self)

    @property
    def entries(self):
        """Copia de las entradas agrupadas, en orden de primera aparición."""
        return [dict(record['entry'], repetitions=record['repetitions']) for record in self._records]

    def retranslate(self):
        selected = self.level_filter.get()
        index = self._level_labels.index(selected) if selected in self._level_labels else 0
        self._level_labels = (tr('output.all'), 'INFO', tr('output.warning'), 'ERROR')
        self.level_combo.configure(values=self._level_labels)
        self.level_filter.set(self._level_labels[index])

    def _apply_theme(self, **_):
        self.text.configure(background=theme.color('surface'), foreground=theme.color('text'),
                            font=theme.font('mono'), selectbackground=theme.color('selection'))
        for level, token in [('INFO', 'text'), ('AVISO', 'warn'), ('ERROR', 'error')]:
            self.text.tag_configure(level, foreground=theme.color(token))

    def _dispose(self, event):
        if event.widget is self:
            bus.unsubscribe(self._theme_subscription)

    def set_select_callback(self, callback):
        """Configura la navegación de una referencia de elemento seleccionada."""
        self._select_callback = callback

    def log(self, level, message, *, module='gui', code='legacy.text', case_id=None,
            run_id=None, element_refs=None, details=None):
        """Registra texto heredado y permite aportar metadatos estructurados opcionales."""
        raw = clean_text(message)
        level = normalize_level(level)
        if level == 'AVISO':
            identity = 'frecuencia-10ms' if '10 ms' in raw and ('power_machineF' in raw or 'frecuencia' in raw.lower()) else raw
            if identity == 'frecuencia-10ms' and identity in self.seen:
                return
            if identity == 'frecuencia-10ms':
                self.seen.add(identity)
            known = raw.startswith(('ElmZpu ', 'Override ', 'Modelo congelado', tr('integration.staswitch_cubicle'), 'StaSwitch sin',
                                    'ElmLne ', 'ElmTr3 ', 'Transformador ', tr('integration.ideal_line'), 'Tap acotado ', 'TypLod:'))
            known = known or raw.startswith('Nodo ') and 'conexiones ideales en malla' in raw
            if known:
                self.model_warnings[raw] = human_message(raw, self.index)
                self.model_summary()
                return
        self.log_entry(make_output_entry(level, raw, module=module, code=code, case_id=case_id,
                                         run_id=run_id, element_refs=element_refs, details=details))

    def log_entry(self, entry):
        """Incorpora una entrada estructurada y condensa solo su repetición autorizada."""
        required = {'entry_id', 'timestamp_utc', 'level', 'module', 'code', 'message', 'element_refs'}
        if not isinstance(entry, dict) or not required <= set(entry):
            raise ValueError('Entrada de salida incompleta')
        normalized = make_output_entry(entry['level'], entry['message'], module=entry['module'], code=entry['code'],
                                       case_id=entry.get('case_id'), run_id=entry.get('run_id'),
                                       element_refs=entry.get('element_refs'), details=entry.get('details'),
                                       timestamp_utc=entry.get('timestamp_utc'), entry_id=entry.get('entry_id'))
        key = output_group_key(normalized)
        record = self._groups.get(key)
        if record is None:
            record = {'entry': normalized, 'repetitions': 1, 'timestamps': [normalized['timestamp_utc']]}
            self._groups[key] = record
            self._records.append(record)
        else:
            record['repetitions'] += 1
            record['timestamps'].append(normalized['timestamp_utc'])
            record['entry']['timestamp_utc'] = normalized['timestamp_utc']
        self._render_records()
        return record['entry']['entry_id']

    def _apply_filters(self, *_unused):
        self._render_records()

    def _render_records(self):
        if not hasattr(self, 'text'):
            return
        self.text.configure(state='normal')
        self.text.delete('1.0', 'end')
        self._line_entries.clear()
        for record in self._records:
            entry = record['entry']
            if not entry_matches(entry, self.level_filter.get(), self.module_filter.get(), self.text_filter.get()):
                continue
            full = public_text(human_message(entry['message'], self.index))
            visible = full.splitlines()[0]
            shortened = len(visible)>145 or '\n' in full
            if shortened:
                visible=visible[:142].rstrip()+'…'
            repetitions = '' if record['repetitions'] == 1 else f'  ×{record["repetitions"]}'
            try:
                stamp = datetime.fromisoformat(entry['timestamp_utc'].replace('Z', '+00:00')).astimezone().strftime('%H:%M:%S')
            except ValueError:
                stamp = entry['timestamp_utc']
            start = self.text.index('end-1c')
            level_label = tr('output.warning') if entry['level'] == 'AVISO' else entry['level']
            line = f"{stamp}  {level_label:<5}  [{entry['module']}] {visible}"
            self.text.insert('end', line + repetitions + '\n', entry['level'])
            if shortened:
                tag='detail_'+entry['entry_id']
                at=f'{start}+{len(line)}c'
                self.text.insert(at,'  ' + tr('output.detail'),tag)
                self.text.tag_configure(tag,foreground=theme.color('accent'),underline=True)
                self.text.tag_bind(tag,'<Button-1>',lambda event,value=full:self._show_detail(value))
            if repetitions:
                tag = f'repetitions_{entry["entry_id"]}'
                repeat_start = f'{start}+{len(line)}c'
                self.text.tag_add(tag, repeat_start, f'{repeat_start}+{len(repetitions)}c')
                self.text.tag_configure(tag, underline=True)
                self.text.tag_bind(tag, '<Button-1>', lambda _event, item=record: self._show_repetitions(item))
            self._line_entries[start.split('.')[0]] = entry
        filter_level = self.level_filter.get()
        filter_text = self.text_filter.get().casefold().strip()
        if (self.model_warnings and filter_level in ('', tr('integration.all'), 'All', tr('output.all'), 'AVISO', tr('output.warning')) and not self.module_filter.get()
                and (not filter_text or filter_text in 'avisos de modelado')):
            self._summary_line = self.text.index('end-1c')
            line = f'{datetime.now():%H:%M:%S}  ' + tr('output.warning') + '  ' + tr('output.warning_summary', count=len(self.model_warnings))
            self.text.insert('end', line + '\n', 'AVISO')
        self.text.configure(state='disabled')
        self.text.see('end')

    def _show_detail(self, value):
        window=tk.Toplevel(self)
        window.title(tr('output.diagnostic_details'))
        text=tk.Text(window,width=100,height=18,wrap='word')
        text.pack(fill='both',expand=True)
        text.insert('1.0',value)
        text.configure(state='disabled')

    def _show_repetitions(self, record):
        """Abre el detalle de horas de un grupo de mensajes repetidos."""
        window = tk.Toplevel(self)
        window.title(tr('output.repetitions', count=record['repetitions']))
        window.transient(self.winfo_toplevel())
        body = tk.Listbox(window, width=46, height=min(12, record['repetitions']))
        for stamp in record['timestamps']:
            body.insert('end', stamp)
        body.pack(fill='both', expand=True, padx=8, pady=8)

    def _select_from_text(self, event):
        line = self.text.index(f'@{event.x},{event.y}').split('.')[0]
        entry = self._line_entries.get(line)
        if not entry or not entry['element_refs'] or not self._select_callback:
            return None
        self._select_callback(entry['element_refs'][0]['object_id'])
        return 'break'

    def model_summary(self):
        """Mantiene el resumen heredado de avisos de modelado."""
        self._render_records()

    def show_model_warnings(self):
        window = tk.Toplevel(self)
        window.title(tr('output.model_warnings'))
        window.geometry('1000x560')
        body = tk.Text(window, wrap='word', padx=12, pady=12)
        scroll = ttk.Scrollbar(window, command=body.yview)
        scroll.pack(side='right', fill='y')
        body.configure(yscrollcommand=scroll.set)
        body.pack(fill='both', expand=True)
        body.insert('end', '\n\n'.join(self.model_warnings.values()) or tr('output.no_warnings'))
        body.configure(state='disabled')

    def clear(self):
        self.seen.clear()
        self.model_warnings.clear()
        self._summary_line = None
        self._records.clear()
        self._groups.clear()
        self._render_records()

    def copy(self):
        """Copia la selección visible, o todo el resultado filtrado si no hay selección."""
        try:
            text = self.text.get('sel.first', 'sel.last')
        except tk.TclError:
            text = self.text.get('1.0', 'end-1c')
        self.clipboard_clear()
        self.clipboard_append(text)

    def save(self, path):
        """Guarda las entradas actualmente visibles como texto UTF-8."""
        Path(path).write_text(self.text.get('1.0', 'end-1c'), encoding='utf-8')

    def save_dialog(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.txt',
                                            filetypes=[(tr('output.text_file'), '*.txt'), (tr('output.all_files'), '*.*')])
        if path:
            self.save(path)
