"""Guided analysis entry point using the public workspace API."""
import tkinter as tk
from tkinter import ttk
from .i18n import tr, language
from .theme import theme

PREFERENCE = 'v3.start.hide_on_start'
STUDIES = ('flow', 'shortcircuit', 'contingency', 'rms')


def reopen_start_page(app):
    """Command for Help > Start, even when startup display is disabled."""
    return show_start_page(app, force=True)


def show_start_page(app, *, force=False):
    if not force and app.settings.get(PREFERENCE, False):
        return None
    existing = getattr(app, 'welcome_page', None)
    if existing is not None and existing.winfo_exists():
        app.book.select(existing)
        existing.refresh()
        return existing
    page = GuidedStart(app)
    app.book.add(page, text=tr('v3.start.title'), area='start')
    app.welcome_page = page
    page.retranslate()
    app.book.select(page)
    return page


class GuidedStart(ttk.Frame):
    workspace_area = 'start'

    def __init__(self, app):
        super().__init__(app.book, padding=(18, 12))
        self.app = app
        self._labels, self._tokens, self._traces, self._bindings = [], [], [], []
        self._freshness = 'ready' if getattr(app, 'result', None) else 'pending'
        self._case = self.case_identity()
        self._result_study = app.analysis.active_study
        self.states, self.buttons, self.cards, self.status_labels = {}, {}, {}, {}
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)
        self.label(self, 'heading', font=theme.font('heading')).grid(sticky='w', pady=(0, 3))
        self.label(self, 'intro').grid(sticky='w', pady=(0, 6))
        self.canvas = tk.Canvas(self, highlightthickness=0, takefocus=False)
        self.canvas.grid(row=2, column=0, sticky='nsew')
        scrollbar = ttk.Scrollbar(self, command=self.canvas.yview)
        scrollbar.grid(row=2, column=1, sticky='ns')
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.body = ttk.Frame(self.canvas)
        self.body.columnconfigure((0, 1), weight=1, uniform='cards')
        self.body_window = self.canvas.create_window(0, 0, window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda _: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', self.resize)
        for number, step in enumerate(('case', 'study', 'run', 'results'), 1):
            card = ttk.Frame(self.body, padding=(8, 6), style='Start.Card.TFrame')
            card.grid(row=(number-1)//2, column=(number-1)%2, sticky='nsew', padx=(0, 6), pady=(0, 6))
            card.columnconfigure(0, weight=1)
            self.cards[step] = card
            self.label(card, step, style='Start.Header.TLabel').grid(row=0, column=0, sticky='w')
            status = ttk.Label(card, style='Start.Muted.TLabel')
            status.grid(row=0, column=1, sticky='e', padx=(8, 0))
            self.status_labels[step] = status
        self.case_summary = ttk.Label(self.cards['case'], style='Start.Card.TLabel', wraplength=720)
        self.case_summary.grid(row=1, column=0, columnspan=2, sticky='w', pady=5)
        actions = self.actions('case', 2)
        self.action(actions, 'open', app.open_case, 0)
        self.action(actions, 'example', app.open_ieee39, 1)
        if callable(getattr(app, 'open_recent_dialog', None)):
            self.action(actions, 'recent', app.open_recent_dialog, 2)
        choices = self.actions('study', 1)
        for i, key in enumerate(STUDIES):
            self.action(choices, key, lambda k=key: self.choose_study(k), i % 2, i // 2)
        self.action(self.cards['study'], 'all', self.all_studies, 0, 2)
        self.configuration = ttk.Label(self.cards['run'], style='Start.Card.TLabel', wraplength=720)
        self.configuration.grid(row=1, column=0, columnspan=2, sticky='w', pady=5)
        actions = self.actions('run', 2)
        self.action(actions, 'execute', self.run, 0, run=True)
        self.action(actions, 'configure', app.analysis.configure_active, 1)
        self.action(actions, 'events', app.analysis.show_events, 0, 1)
        actions = self.actions('results', 1)
        for i, area in enumerate(('curves', 'tables', 'log')):
            self.action(actions, area, lambda a=area: self.show_results(a), i % 2, i // 2)
        self.action(actions, 'network', app.open_diagram, 1, 1)
        self.hidden = tk.BooleanVar(self, value=bool(app.settings.get(PREFERENCE, False)))
        self.preference = ttk.Checkbutton(self, variable=self.hidden, command=self.save_preference, takefocus=True)
        self._labels.append((self.preference, 'hide'))
        self.preference.grid(row=3, column=0, sticky='w', pady=(3, 0))
        self.label(self, 'keys', style='Start.Hint.TLabel').grid(row=4, sticky='w', pady=(4, 0))
        for topic in ('network.edited', 'results.updated', 'results.stale', 'selection.changed'):
            self._tokens.append(app.bus.subscribe(topic, lambda _topic=topic, **data: self.on_event(_topic, **data)))
        self._tokens.append(app.bus.subscribe('language.changed', self.retranslate))
        self._tokens.append(app.bus.subscribe('theme.changed', self.restyle))
        for name in ('case_label', 'configuration_text'):
            variable = getattr(app, name, None)
            if variable is not None:
                token = variable.trace_add('write', lambda *_: self.refresh())
                self._traces.append((variable, token))
        tree = app.analysis.study_tree
        token = tree.bind('<<TreeviewSelect>>', lambda _: self.after_idle(self.refresh), add='+')
        self._bindings.append((tree, '<<TreeviewSelect>>', token))
        for i, key in enumerate(('open', 'all', 'execute', 'curves'), 1):
            sequence = f'<Alt-Key-{i}>'
            token = app.bind(sequence, lambda event, k=key: self.shortcut(k), add='+')
            self._bindings.append((app, sequence, token))
        self.bind('<Destroy>', self.dispose, add='+')
        def wire(widget):
            widget.bind('<MouseWheel>', lambda e: self.canvas.yview_scroll(-int(e.delta/120), 'units'), add='+')
            widget.bind('<FocusIn>', lambda e, w=widget: self.reveal(w), add='+')
            for child in widget.winfo_children():
                wire(child)
        wire(self.body)
        self.restyle()

    def label(self, parent, key, **kwargs):
        widget = ttk.Label(parent, **kwargs)
        self._labels.append((widget, key))
        return widget

    def actions(self, step, row):
        frame = ttk.Frame(self.cards[step], style='Start.Card.TFrame')
        frame.grid(row=row, column=0, columnspan=2, sticky='w', pady=(4, 0))
        return frame

    def action(self, parent, key, command, column, row=0, run=False):
        button = ttk.Button(parent, command=command, takefocus=True,
                            style='Run.TButton' if run else 'Start.TButton')
        button.grid(row=row, column=column, sticky='w', padx=(0, 8), pady=3)
        button.bind('<Return>', lambda _: button.invoke())
        self._labels.append((button, key))
        self.buttons[key] = button
        return button

    def restyle(self, **_):
        style = ttk.Style(self)
        style.configure('Start.Card.TFrame', background=theme.color('paper'), borderwidth=1, relief='solid')
        style.configure('Start.Card.TLabel', background=theme.color('paper'), foreground=theme.color('ink'), font=theme.font('small'))
        style.configure('Start.Header.TLabel', background=theme.color('paper'), foreground=theme.color('accent'), font=theme.font('small'))
        style.configure('Start.Muted.TLabel', background=theme.color('paper'), foreground=theme.color('muted'), font=theme.font('small'))
        style.configure('Start.Hint.TLabel', foreground=theme.color('muted'), font=theme.font('small'))
        style.configure('Start.TButton', padding=(4, 2), borderwidth=1, font=theme.font('small'))
        style.map('Start.TButton', bordercolor=[('focus', theme.color('accent2'))], relief=[('focus', 'solid')])
        self.canvas.configure(background=theme.color('bg'))

    def resize(self, event):
        if getattr(self, '_canvas_size', None) == (event.width,event.height):return
        self._canvas_size=(event.width,event.height)
        self._canvas_width = event.width
        width=min(1200,event.width)
        self.canvas.itemconfigure(self.body_window, width=width)
        self.canvas.coords(self.body_window,max(0,(event.width-width)/2),min(32,max(0,(event.height-self.body.winfo_reqheight())/5)))
        columns = 2 if width >= 740 else 1
        for i, card in enumerate(self.cards.values()):
            card.grid_configure(row=i//columns, column=i%columns)
        for label in (self.case_summary, self.configuration):
            label.configure(wraplength=max(180, width//columns-60))

    def reveal(self, widget):
        y = widget.winfo_rooty()-self.body.winfo_rooty()
        top = self.canvas.canvasy(0)
        if y < top or y+widget.winfo_height() > top+self.canvas.winfo_height():
            self.canvas.yview_moveto(max(0, y-10)/max(1, self.body.winfo_height()))

    def case_identity(self):
        doc = getattr(self.app, 'document', None)
        return (id(doc), getattr(doc, 'active_case_id', None))

    def choose_study(self, key):
        self.app.analysis.select_study(key)
        self.refresh()

    def all_studies(self):
        self.app.analysis.study_tree.focus_set()

    def run(self):
        return self.app.analysis.run_active()

    def show_results(self, area):
        analysis = self.app.analysis
        analysis.results.select(analysis.result_hosts[area])
        if area in analysis.books:
            pages = [p for p in analysis.books[area].tabs() if p != str(self)]
            if pages:
                self.app.book.select(pages[-1])

    def shortcut(self, key):
        if self.winfo_ismapped():
            self.buttons[key].focus_set()
            self.buttons[key].invoke()
            return 'break'

    def save_preference(self):
        self.app.settings.set(PREFERENCE, self.hidden.get())
        self.app.settings.save()

    def on_event(self, topic, **_):
        if topic == 'results.updated':
            self._freshness = 'ready'
            self._result_study = self.app.analysis.active_study
        elif topic in ('results.stale', 'network.edited') and self._freshness != 'pending':
            self._freshness = 'stale'
        self.refresh()

    def refresh(self):
        if not self.winfo_exists():
            return
        app = self.app
        identity = self.case_identity()
        if identity != self._case:
            self._freshness = 'pending'
            self._case = identity
        model = getattr(app, 'operational', None) or getattr(app, 'model', {}) or {}
        ready = getattr(app, 'document', None) is not None
        study = app.analysis.active_study
        freshness = self._freshness
        if freshness == 'ready' and study != self._result_study:
            freshness = 'stale'
        self.states = dict(case='ready' if ready else 'pending', study='ready' if ready and study else 'pending',
                           run='ready' if ready and study else 'pending', results=freshness)
        for step, state in self.states.items():
            self.status_labels[step].configure(text=tr('v3.start.'+state))
        name = app.case_label.get() if ready else tr('v3.start.no_case')
        frequency = model.get('meta', {}).get('f_nom_hz')
        hz = f'{frequency:g}' if isinstance(frequency, (int, float)) else '—'
        if language() == 'es':
            hz = hz.replace('.', ',')
        self.case_summary.configure(text=tr('v3.start.summary', name=name,
            buses=len(model.get('buses', [])), lines=sum(row.get('class') == 'ElmLne' for row in model.get('branches', [])),
            gens=len(model.get('gens', [])), loads=len(model.get('loads', [])), hz=hz))
        configuration = app.configuration_text.get() if ready else tr('v3.start.no_case')
        if ready and study == 'flow':
            options = app.document.study.ldf_options
            configuration += '\n' + tr('v3.start.flow_config', tolerance=options.get('tolerance_pu', 1e-8),
                                      iterations=options.get('max_iter', 30))
        elif ready and study == 'rms':
            dt = f'{app.document.study.options.dt_s:g}'
            if language() == 'es':
                dt = dt.replace('.', ',')
            configuration += '\n' + tr('v3.start.rms_config', dt=dt, events=len(app.document.study.events))
        self.configuration.configure(text=configuration)
        for key in ('execute', 'configure', 'events'):
            self.buttons[key].state(['!disabled'] if ready and study else ['disabled'])
        if study == 'rms':
            self.buttons['events'].grid()
        else:
            self.buttons['events'].grid_remove()
        for key in STUDIES:
            self.buttons[key].state(['pressed'] if key == study else ['!pressed'])

    def retranslate(self, **_):
        for widget, key in self._labels:
            widget.configure(text=tr('v3.start.'+key))
        if str(self) in self.app.book.tabs():
            self.app.book.tab(self, text=tr('v3.start.title'))
        self.refresh()

    def dispose(self, event):
        if event.widget is not self:
            return
        for token in self._tokens:
            self.app.bus.unsubscribe(token)
        for variable, token in self._traces:
            variable.trace_remove('write', token)
        for widget, sequence, token in self._bindings:
            if widget.winfo_exists():
                widget.unbind(sequence, token)
