"""Tablas flexibles de resultados, con valores crudos para ordenar y exportar."""
import csv
import math
import tkinter as tk
from tkinter import filedialog, ttk

from .presentation import human_message, station
from .theme import theme
from .bus import bus
from .i18n import tr
from .settings import settings



COLUMNS = {
    'buses': [('name', tr('results.busbar')), ('substation', tr('results.substation')), ('un_kv', tr('results.un_kv')),
              ('u_pu', tr('results.u_p_u')), ('u_kv', tr('results.u_kv')), ('angle_deg', tr('results.angle')),
              ('energized', tr('results.energized')), ('object_id', tr('results.id_pf'))],
    'branches': [('name', tr('results.branch')), ('substation', tr('results.substation')), ('un_kv', tr('results.un_kv')), ('class', tr('results.class')),
                 ('bus_from', tr('results.from')), ('bus_to', tr('results.to')), ('p_from_mw', tr('results.p_from_mw')),
                 ('q_from_mvar', tr('results.q_from_mvar')), ('p_to_mw', tr('results.p_to_mw')),
                 ('q_to_mvar', tr('results.q_to_mvar')), ('i_from_ka', tr('results.i_from_ka')),
                 ('loading_pct', tr('results.loading')), ('object_id', tr('results.id_pf'))],
    'gens': [('name', tr('results.generator')), ('substation', tr('results.substation')), ('un_kv', tr('results.un_kv')),
             ('p_mw', tr('results.p_mw')), ('q_mvar', tr('results.q_mvar')), ('at_q_limit', tr('results.at_q_limit')), ('object_id', tr('results.id_pf'))],
    'loads': [('name', tr('results.load')), ('substation', tr('results.substation')), ('un_kv', tr('results.un_kv')),
              ('p_mw', tr('results.p_mw')), ('q_mvar', tr('results.q_mvar')), ('object_id', tr('results.id_pf'))],
}

DEFAULT_LIMITS = {'u_pu': (.95, 1.05), 'loading_pct': (None, 100.0)}


def localized_columns(columns):
    """Resuelve también encabezados creados antes de cambiar el idioma."""
    import json
    from pathlib import Path
    catalogues = [json.loads((Path(__file__).with_name('locale') / (code + '.json')).read_text('utf-8'))
                  for code in ('en', 'es')]
    keys = {value: key for catalogue in catalogues for key, value in catalogue.items()
            if key.startswith('results.')}
    return [(key, tr(keys[label]) if label in keys else label) for key, label in columns]


def bind_translations(widget):
    """Actualiza rótulos estáticos al cambiar el idioma y libera la suscripción."""
    import json
    from pathlib import Path
    labels = {}
    for code in ('en', 'es'):
        catalogue = json.loads((Path(__file__).with_name('locale') / (code + '.json')).read_text('utf-8'))
        labels.update({value: key for key, value in catalogue.items()
                       if key.startswith(('results.', 'plots.', 'output.', 'report.'))})
    controls = []
    def collect(parent):
        for child in parent.winfo_children():
            try:
                value = str(child.cget('text'))
                if value in labels:
                    controls.append((child, labels[value]))
            except tk.TclError:
                pass
            collect(child)
    collect(widget)
    def update(**_):
        for control, key in controls:
            control.configure(text=tr(key))
        if hasattr(widget, 'retranslate'):
            widget.retranslate()
        if isinstance(widget, ResultsTable):
            widget.columns = localized_columns(widget.columns)
            widget.populate()
    token = bus.subscribe('language.changed', update)
    widget.bind('<Destroy>', lambda event: bus.unsubscribe(token) if event.widget is widget else None, add='+')


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def alarm_tags(row, limits=None):
    """Marca valores fuera de límites sin convertir datos ausentes en cero."""
    limits = dict(DEFAULT_LIMITS if limits is None else limits)
    for key, bounds in limits.items():
        value = row.get(key)
        if not is_number(value):
            continue
        minimum, maximum = bounds
        if (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
            return ('alarm',)
    return ()


def format_value(value, key=None):
    if key == 'state':
        state_keys = {'Operación en islas': 'results.state_islanded', 'Sin convergencia': 'results.state_failed', 'Límite superado': 'results.state_limit', 'Generación desconectada': 'results.state_generation', 'Calculado': 'results.state_solved'}
        if value in state_keys:
            return tr(state_keys[value])
    if value is None:
        return '—'
    if isinstance(value, bool):
        return tr('results.yes') if value else tr('results.no')
    if is_number(value) and key:
        decimals = 3 if key=='time_s' else 4 if key.endswith('_pu') or key == 'voltage' else 3 if key.endswith('_ka') else 2
        return f'{value:.{decimals}f}'
    if isinstance(value, float):
        return f'{value:.6g}' if math.isfinite(value) else '—'
    return clean_display(value)


def display_value(value,key=None,*,precision=None):
    """Locale-aware screen value. CSV export retains the original raw numbers."""
    from .i18n import language
    text=f'{value:.{precision}f}' if precision is not None and is_number(value) and math.isfinite(value) else format_value(value,key)
    return text.replace('.',',') if is_number(value) and language()=='es' else text


def clean_display(value):
    return str(value).replace(';', ',')


def ranked_contingency_cases(cases):
    """Consume el orden público de la API, sin reproducir su criterio aquí."""
    from power_machine.api import ContingencyResult, StudyTable
    rows = [dict(case, unserved_mw=case.get('metrics', {}).get('unsupplied_p_mw'),
                 max_loading_pct=case.get('metrics', {}).get('max_loading_pct')) for case in cases]
    ranked = ContingencyResult(status='completed', cases=StudyTable('cases', tuple(rows))).rank().to_records()
    solved, unresolved = [], []
    for case in ranked:
        failed = case.get('metrics', {}).get('energized_converged') is False or case.get('status') in (
            'not_converged', 'numerical_failure', 'invalid_input', 'missing_reference')
        (unresolved if failed else solved).append(case)
    return solved, unresolved


def contingency_violation_text(case):
    """Expone los límites publicados, incluso sobretensiones y límites Q."""
    terms = []
    for item in case.get('violations', ()):
        ident = item.get('object_id', item.get('model_id', ''))
        kind = item.get('kind')
        if kind == 'voltage':
            terms.append(tr('results.violation_voltage', id=ident, value=item['u_pu'],
                            low=item['min_pu'], high=item['max_pu']))
        elif kind == 'loading':
            terms.append(tr('results.violation_loading', id=ident, value=item['loading_pct'], limit=item['limit_pct']))
        elif kind == 'q_limit':
            terms.append(tr('results.violation_q', id=ident, value=item['q_mvar'],
                            low=item['min_mvar'], high=item['max_mvar']))
        else:
            terms.append(str(kind) + ' ' + str(ident))
    return ' | '.join(terms) or '—'


def _worst_violation(case):
    items = case.get('violations', ())
    if not items:
        return None
    def excess(item):
        key, low, high = {'voltage': ('u_pu', 'min_pu', 'max_pu'),
                          'loading': ('loading_pct', None, 'limit_pct'),
                          'q_limit': ('q_mvar', 'min_mvar', 'max_mvar')}.get(item.get('kind'), (None, None, None))
        value = item.get(key)
        if not is_number(value):
            return 0
        bounds = [(item.get(low), -1), (item.get(high), 1)]
        return max([0] + [(value-bound)*direction/max(abs(bound), 1e-12)
                          for bound, direction in bounds if is_number(bound)])
    return max(items, key=excess)


def contingency_violation_summary(case):
    worst = _worst_violation(case)
    if worst is None:
        return '—'
    items = case.get('violations', ())
    kind = worst.get('kind')
    field, symbol, unit = {'voltage': ('u_pu', 'U', 'p.u.'), 'loading': ('loading_pct', tr('results.loading'), '%'),
                           'q_limit': ('q_mvar', 'Q', 'Mvar')}.get(kind, ('value', str(kind), ''))
    return tr('results.violation_summary', count=len(items), symbol=symbol,
              id=worst.get('object_id', worst.get('model_id', '')),
              value=format_value(worst.get(field), field), unit=unit)


class ContingencyRanking(ttk.Frame):
    """Ranking operativo con detalle de la fila y selección de elementos por bus."""
    def __init__(self, master, cases, index=None, on_select=None, detail_parent=None):
        super().__init__(master)
        self.configure(width=500, height=200)
        self.pack_propagate(False)
        self.index, self.on_select = index or {}, on_select
        self.selected_case = None
        self.criterion = ttk.Label(self, wraplength=1200)
        self.criterion.pack(fill='x', padx=8, pady=3)
        self.book = ttk.Notebook(self)
        self.book.pack(fill='both', expand=True)
        COLUMNS['contingencies'] = [(key, tr('results.rank_' + key)) for key in
                                   ('case', 'state', 'loading_pct', 'u_pu', 'max_u_pu', 'violation')]
        self.detail_group = ttk.LabelFrame(detail_parent if detail_parent is not None else self)
        if detail_parent is not None:
            self.detail_group.configure(width=500, height=180)
            self.detail_group.pack_propagate(False)
        self.detail_group.pack(side='bottom', fill='both' if detail_parent is not None else 'x', expand=detail_parent is not None, padx=8, pady=4)
        self.detail_hint = ttk.Label(self.detail_group, wraplength=1200)
        self.detail_hint.pack(fill='x')
        detail_body = ttk.Frame(self.detail_group)
        detail_body.pack(fill='both', expand=True)
        self.detail_tree = ttk.Treeview(detail_body, columns=('kind','element','value','lower','upper','unit'), show='headings', height=4)
        self.detail_tree.raw_rows = {}
        scroll = ttk.Scrollbar(detail_body, command=self.detail_tree.yview)
        self.detail_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.detail_tree.pack(side='left', fill='both', expand=True)
        self.unresolved_group = ttk.Frame(self.book)
        self.unresolved_table = ResultsTable(self.unresolved_group, 'contingencies', [], limits={}, compact=True)
        self.unresolved_table.tree.configure(height=1)
        self.unresolved_table.pack(fill='x')
        self.ranked_table = ResultsTable(self.book, 'contingencies', [], limits={})
        self.book.add(self.ranked_table, text=tr('results.rank_solved'))
        self.book.add(self.unresolved_group, text=tr('results.rank_unsolved'))
        for table in (self.ranked_table, self.unresolved_table):
            table.column_widths = dict(case=95, state=140, loading_pct=110, u_pu=90, max_u_pu=90, violation=320)
            table.summary_mode.set(tr('results.none'))
            table.tree.bind('<<TreeviewSelect>>', lambda event, table=table: self._select(table), add='+')
            for key in ('case', 'state', 'loading_pct', 'u_pu', 'max_u_pu'):
                table.tree.column(key, width=115 if key == 'state' else 90,
                                  minwidth=105 if key == 'loading_pct' else 80)
            table.tree.column('violation', width=370, minwidth=250, anchor='w')
        self.set_cases(cases)
        self.retranslate()
        token = bus.subscribe('language.changed', lambda **_: self.after_idle(lambda: self.retranslate() if self.winfo_exists() else None))
        self.bind('<Destroy>', lambda event: bus.unsubscribe(token) if event.widget is self else None, add='+')

    def set_cases(self, cases):
        import json
        self.cases = list(cases)
        self.case_by_id = {case['case_id']: case for case in self.cases}
        if self.selected_case and self.selected_case['case_id'] not in self.case_by_id:
            self.selected_case = None
            self.detail_tree.delete(*self.detail_tree.get_children())
            self.detail_hint.configure(text=tr('results.select_violation'))
            if self.on_select:
                self.on_select(None)
        solved, unresolved = ranked_contingency_cases(self.cases)
        self.unresolved_ids = {case['case_id'] for case in unresolved}
        self.book.hide(self.unresolved_group)
        if unresolved:
            self.book.add(self.unresolved_group, text=tr('results.rank_unsolved') + ' (' + str(len(unresolved)) + ')')
        for table, group in ((self.ranked_table, solved), (self.unresolved_table, unresolved)):
            table.rows = []
            table.selection_map = {}
            for case in group:
                metric = case.get('metrics', {})
                ids = [str(ident) for outage in case.get('outages', ()) for ident in
                       (outage.get('member_ids') or [outage.get('object_id', '')])]
                table.selection_map[case['case_id']] = ids
                state = {'limit_violation': 'limit', 'islanded': 'islanded', 'not_converged': 'failed',
                         'completed': 'solved', 'generation_disconnected': 'generation'}.get(case.get('status'))
                table.rows.append(dict(object_id=case['case_id'], case=' + '.join(ids),
                    state=tr('results.state_' + state) if state else case.get('status', ''),
                    loading_pct=metric.get('max_loading_pct'), u_pu=metric.get('min_u_pu'),
                    max_u_pu=metric.get('max_u_pu'), violation=contingency_violation_summary(case),
                    has_violation=bool(case.get('violations')) or case.get('status') == 'limit_violation',
                    violation_detail=json.dumps(case.get('violations', []), ensure_ascii=False)))
            table.populate()
        if self.selected_case and self.selected_case['case_id'] in self.case_by_id:
            self.select_case(self.selected_case['case_id'])

    def select_case(self, case_id):
        for table in (self.ranked_table, self.unresolved_table):
            if table.tree.exists(case_id):
                self.book.select(self.ranked_table if table is self.ranked_table else self.unresolved_group)
                table.tree.selection_set(case_id)
                table.tree.see(case_id)
                self._select(table)
                return

    def _select(self, table):
        selected = table.tree.selection()
        if not selected or selected[0] not in self.case_by_id:
            return
        case = self.case_by_id[selected[0]]
        table.tree.xview_moveto(0)
        self.selected_case = case
        self.detail_tree.delete(*self.detail_tree.get_children())
        self.detail_hint.configure(text=' + '.join(table.selection_map.get(selected[0], [])))
        self.detail_rows = []
        items = list(case.get('violations', ()))
        # La primera fila corresponde a la violación resumida, sin alterar el caso.
        if items:
            worst = _worst_violation(case)
            items.sort(key=lambda item: item != worst)
        for item in items:
            field, low, high, unit = {'voltage': ('u_pu','min_pu','max_pu','p.u.'),
                                      'loading': ('loading_pct',None,'limit_pct','%'),
                                      'q_limit': ('q_mvar','min_mvar','max_mvar','Mvar')}.get(item.get('kind'), ('value',None,None,''))
            values = [tr('results.violation_kind_' + item['kind']), item.get('object_id', item.get('model_id', '')),
                      format_value(item.get(field),field), format_value(item.get(low),field),
                      format_value(item.get(high),field), unit]
            self.detail_tree.insert('', 'end', values=values)
            self.detail_rows.append(dict(item))
        if not case.get('violations') and case['case_id'] in self.unresolved_ids:
            mismatch=max((i.get('max_mismatch_mva') or 0 for i in case.get('metrics', {}).get('islands', ())), default=0)
            self.detail_hint.configure(text=tr('results.rank_unsolved_reason', mismatch=mismatch))
        if self.on_select:
            self.on_select(case)

    def retranslate(self):
        self.criterion.configure(text=tr('results.rank_criterion'))
        self.detail_group.configure(text=tr('results.violation_detail'))
        self.book.tab(self.ranked_table, text=tr('results.rank_solved'))
        for key in self.detail_tree['columns']:
            self.detail_tree.heading(key, text=tr('results.detail_' + key))
            self.detail_tree.column(key, width=120, anchor='w')
        if not self.selected_case:
            self.detail_hint.configure(text=tr('results.select_violation'))
        self.set_cases(self.cases)


def sorted_rows(rows, key, reverse=False):
    present = [row for row in rows if row.get(key) is not None]
    missing = [row for row in rows if row.get(key) is None]

    def order(row):
        value = row[key]
        return (0, value) if is_number(value) else (1, str(value).casefold())
    return sorted(present, key=order, reverse=reverse) + missing


def filtered_rows(rows, text='', numeric_ranges=None):
    """Filtra texto y rangos inclusivos sobre valores crudos, no sobre su formato."""
    needle = text.casefold().strip()
    ranges = numeric_ranges or {}
    result = []
    for row in rows:
        if needle and needle not in ' '.join(clean_display(value) for value in row.values()).casefold():
            continue
        accepted = True
        for key, bounds in ranges.items():
            value = row.get(key)
            minimum, maximum = bounds
            if not is_number(value) or (minimum is not None and value < minimum) or (maximum is not None and value > maximum):
                accepted = False
                break
        if accepted:
            result.append(row)
    return result


def summary_row(rows, columns, mode='totals'):
    """Calcula totales o extremos solamente para columnas numéricas presentes."""
    if mode not in ('totals', 'extremes') or not rows:
        return None
    result = {'name': 'Total' if mode == 'totals' else tr('results.extremes')}
    for key, _label in columns:
        values = [row[key] for row in rows if is_number(row.get(key))]
        if not values:
            continue
        result[key] = sum(values) if mode == 'totals' else format_value(min(values), key) + ' … ' + format_value(max(values), key)
    return result


def export_rows(path, columns, rows):
    """Exporta valores fuente con precisión completa y encabezados con unidades."""
    visible = [(key, label) for key, label in columns if key != 'object_id']
    if any('violation_detail' in row for row in rows):
        visible.append(('violation_detail', tr('results.violation_detail')))
    with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow([tr('results.id_pf')] + [label for _, label in visible])
        writer.writerows([[row.get('object_id', row.get('id', ''))] + [row.get(key, '') for key, _ in visible]
                          for row in rows])


class ResultsTable(ttk.Frame):
    """Tabla de resultados con configuración local de columnas, filtros y límites."""
    def __init__(self, master, kind, rows, index=None, select_callback=None, limits=None, settings_store=None, compact=False):
        super().__init__(master, padding=8)
        self.columns = localized_columns(COLUMNS[kind])
        self.kind = kind
        self.settings_store = settings if settings_store is None else settings_store
        self._select_callback = select_callback
        self.limits = dict(DEFAULT_LIMITS if limits is None else limits)
        self.numeric_ranges = {}
        self.reverse = {}
        self.summary_mode = tk.StringVar(value=tr('results.totals'))
        self.text_filter = tk.StringVar(value='')
        self.violations_only = tk.BooleanVar(value=False)
        self.column_visible = {key: tk.BooleanVar(value=True) for key, _ in self.columns}
        index = index or {}
        self.rows = []
        for source in rows:
            row = dict(source)
            row['object_id'] = row.get('object_id', row.get('id', ''))
            row['substation'] = row.get('substation') or station(index.get(row['object_id'], row))
            if row['substation'] == 'Sin subestación':
                row['substation'] = tr('results.no_station')
            row.setdefault('un_kv', index.get(row['object_id'], row).get('un_kv'))
            for key in ('bus_from', 'bus_to'):
                if row.get(key):
                    row[key] = human_message(row[key], index)
            if 'loading_pct' not in row:
                row['loading_pct'] = row.get('loading_percent', row.get('c:loading'))
            self.rows.append(row)

        stored = self.settings_store.get('panel_options', {}).get('data', {}).get('columns', {})
        missing_stations = {'', 'Sin subestación', 'No substation', tr('results.no_station')}
        any_station = any(row.get('substation') not in missing_stations for row in self.rows)
        for key, variable in self.column_visible.items():
            variable.set(stored.get(key, key != 'substation' or any_station))

        bar = ttk.Frame(self)
        if not compact:
            bar.pack(fill='x', pady=(0, 6))
        ttk.Button(bar, text=tr('results.export_csv'), command=self.export).pack(side='right')
        ttk.Button(bar, text=tr('results.copy'), command=self.copy).pack(side='right', padx=4)
        if kind != 'contingencies':
            ttk.Button(bar, text=tr('results.limits'), command=self.configure_limits).pack(side='right', padx=4)
            ttk.Button(bar, text=tr('results.ranges'), command=self.configure_ranges).pack(side='right', padx=4)
        columns_button = ttk.Menubutton(bar, text=tr('results.columns'))
        columns_menu = tk.Menu(columns_button, tearoff=False)
        columns_button.configure(menu=columns_menu)
        for key, label in self.columns:
            columns_menu.add_checkbutton(label=label, variable=self.column_visible[key], command=self._columns_changed)
        columns_button.pack(side='left')
        ttk.Label(bar, text=tr('results.filter')).pack(side='left', padx=(10, 3))
        ttk.Entry(bar, textvariable=self.text_filter, width=24).pack(side='left')
        ttk.Checkbutton(bar, text=tr('results.violations_only'), variable=self.violations_only, command=self.populate).pack(side='left')
        if kind != 'contingencies':
            ttk.Label(bar, text=tr('results.summary')).pack(side='left', padx=(10, 3))
            ttk.Combobox(bar, textvariable=self.summary_mode, values=(tr('results.none'), tr('results.totals'), tr('results.extremes')),
                         width=10, state='readonly').pack(side='left')
        self.text_filter.trace_add('write', lambda *_: self.populate())
        self.summary_mode.trace_add('write', lambda *_: self.populate())

        body = ttk.Frame(self)
        body.pack(fill='both', expand=True)
        self.tree = ttk.Treeview(body, show='headings')
        # Este componente mantiene valores fuente y formato por magnitud propios.
        # Evita la instrumentación genérica de finish_panel sobre textos ya formateados.
        self.tree.raw_rows = {}
        self._apply_theme()
        sy = ttk.Scrollbar(body, orient='vertical', command=self.tree.yview)
        sx = ttk.Scrollbar(body, orient='horizontal', command=self.tree.xview)
        self.tree.configure(yscrollcommand=sy.set, xscrollcommand=sx.set)
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)
        self.tree.grid(row=0, column=0, sticky='nsew')
        sy.grid(row=0, column=1, sticky='ns')
        sx.grid(row=1, column=0, sticky='ew')
        self.tree.bind('<Double-1>', self._select_row, add='+')
        self.populate()
        self.tree.bind('<<TreeviewSelect>>', self._publish_selection, add='+')
        self._subscriptions = [bus.subscribe('theme.changed', self._apply_theme),
                               bus.subscribe('selection.changed', self._receive_selection)]
        self.bind('<Destroy>', self._dispose, add='+')
        bind_translations(self)

    def _apply_theme(self, **_):
        self.tree.tag_configure('alarm', background=theme.color('surface_alt'), foreground=theme.color('error'))
        self.tree.tag_configure('summary', background=theme.color('surface_alt'), foreground=theme.color('text'))

    def _columns_changed(self):
        from copy import deepcopy
        options = deepcopy(self.settings_store.get('panel_options', {}))
        columns = options.setdefault('data', {}).setdefault('columns', {})
        columns.update({key: bool(value.get()) for key, value in self.column_visible.items()})
        self.settings_store.set('panel_options', options)
        self.settings_store.save()
        self.populate()

    def _dispose(self, event):
        if event.widget is self:
            for token in self._subscriptions:
                bus.unsubscribe(token)

    def _publish_selection(self, _event=None):
        ids = [iid for iid in self.tree.selection() if iid != '__summary__']
        if ids:
            bus.publish('selection.changed', ids=list(dict.fromkeys(element for ident in ids for element in getattr(self, 'selection_map', {}).get(ident, [ident]))), source='results')

    def _receive_selection(self, ids, source=None):
        selected = [ident for ident in self.tree.get_children() if ident in {str(i) for i in ids} or any(element in ids for element in getattr(self, 'selection_map', {}).get(ident, []))]
        if tuple(selected) != self.tree.selection():
            self.tree.selection_set(selected)
            if selected:
                self.tree.see(selected[0])

    @property
    def visible_columns(self):
        return [(key, label) for key, label in self.columns if self.column_visible[key].get()]

    @property
    def visible_rows(self):
        rows = filtered_rows(self.rows, self.text_filter.get(), self.numeric_ranges)
        if self.kind == 'contingencies' and self.violations_only.get():
            return [row for row in rows if row.get('has_violation')]
        return [row for row in rows if alarm_tags(row, self.limits)] if self.violations_only.get() else rows

    def set_select_callback(self, callback):
        self._select_callback = callback

    def set_limits(self, limits):
        """Actualiza límites configurables, con pares ``(mínimo, máximo)`` explícitos."""
        self.limits = dict(limits)
        self.populate()

    def set_numeric_ranges(self, ranges):
        self.numeric_ranges = dict(ranges)
        self.populate()

    def populate(self):
        columns = self.visible_columns
        self.tree.configure(columns=[key for key, _ in columns])
        for key, label in columns:
            self.tree.heading(key, text=label, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=getattr(self, 'column_widths', {}).get(key, 230 if key == 'name' else 130),
                             minwidth=150 if key=='name' else 100,
                             anchor='w' if key in ('name', 'substation', 'case', 'state', 'violation') else 'e')
        self.tree.delete(*self.tree.get_children())
        for row in self.visible_rows:
            object_id = str(row.get('object_id', row.get('id', '')))
            self.tree.insert('', 'end', iid=object_id or None,
                             values=[display_value(row.get(key), key) for key, _ in columns],
                             tags=alarm_tags(row, self.limits))
        mode = {'Totales': 'totals', 'Extremos': 'extremes', tr('results.totals'): 'totals', tr('results.extremes'): 'extremes'}.get(self.summary_mode.get())
        summary = summary_row(self.visible_rows, columns, mode)
        if summary:
            self.tree.insert('', 'end', iid='__summary__',
                             values=[display_value(summary.get(key), key) for key, _ in columns], tags=('summary',))

    def sort(self, key):
        self.rows = sorted_rows(self.rows, key, self.reverse.get(key, False))
        self.reverse[key] = not self.reverse.get(key, False)
        self.populate()

    def _select_row(self, event):
        iid = self.tree.identify_row(event.y)
        if iid and iid != '__summary__' and self._select_callback:
            self._select_callback(iid)
            return 'break'
        return None

    def copy(self):
        selected = [self.tree.item(iid)['values'] for iid in self.tree.selection() if iid != '__summary__']
        values = selected or [[format_value(row.get(key), key) for key, _ in self.visible_columns]
                              for row in self.visible_rows]
        text = '\t'.join(label for _, label in self.visible_columns)
        if values:
            text += '\n' + '\n'.join('\t'.join(map(str, row)) for row in values)
        self.clipboard_clear()
        self.clipboard_append(text)

    def export(self):
        path = filedialog.asksaveasfilename(parent=self, defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if path:
            export_rows(path, self.visible_columns, self.visible_rows)

    def configure_ranges(self):
        self._configure_numeric_dialog(tr('results.numeric_ranges'), self.numeric_ranges, self.set_numeric_ranges)

    def configure_limits(self):
        self._configure_numeric_dialog(tr('results.highlight_limits'), self.limits, self.set_limits)

    def _configure_numeric_dialog(self, title, current, callback):
        window = tk.Toplevel(self)
        window.title(title)
        window.transient(self.winfo_toplevel())
        entries = {}
        numeric_keys = [key for key, _ in self.columns if any(is_number(row.get(key)) for row in self.rows)]
        for number, key in enumerate(numeric_keys):
            label = next(label for candidate, label in self.columns if candidate == key)
            ttk.Label(window, text=label).grid(row=number, column=0, sticky='w', padx=8, pady=3)
            bounds = current.get(key, (None, None))
            low = ttk.Entry(window, width=12)
            high = ttk.Entry(window, width=12)
            if bounds[0] is not None:
                low.insert(0, str(bounds[0]))
            if bounds[1] is not None:
                high.insert(0, str(bounds[1]))
            low.grid(row=number, column=1, padx=3, pady=3)
            high.grid(row=number, column=2, padx=3, pady=3)
            entries[key] = (low, high)

        def apply():
            values = {}
            for key, (low, high) in entries.items():
                try:
                    minimum = float(low.get()) if low.get().strip() else None
                    maximum = float(high.get()) if high.get().strip() else None
                except ValueError:
                    return
                if minimum is not None or maximum is not None:
                    values[key] = (minimum, maximum)
            callback(values)
            window.destroy()

        ttk.Button(window, text=tr('results.apply'), command=apply).grid(row=len(numeric_keys), column=2, sticky='e', padx=8, pady=8)
