"""Disposición compacta y navegación de páginas de la ventana de ingeniería."""
import re
from .i18n import tr
from .theme import theme
import tkinter as tk
from tkinter import ttk
from .output import OutputPanel
from .theme import theme
from .i18n import tr


class Tooltip:
    def __init__(self, widget, text):
        self.widget, self.text, self.window, self.timer = widget, text, None, None
        widget.bind('<Enter>', self.schedule, add='+')
        widget.bind('<Leave>', self.hide, add='+')
        widget.bind('<FocusIn>', self.schedule, add='+')
        widget.bind('<FocusOut>', self.hide, add='+')
        widget.bind('<ButtonPress>', self.hide, add='+')
        widget.bind('<Destroy>', self.hide, add='+')

    def schedule(self, event=None):
        self.hide()
        self.timer = self.widget.after(450, self.show)

    def show(self):
        self.timer = None
        if not self.widget.winfo_exists():
            return
        self.window = tk.Toplevel(self.widget)
        self.window.overrideredirect(True)
        self.window.geometry(f'+{self.widget.winfo_rootx()}+{self.widget.winfo_rooty()+self.widget.winfo_height()+3}')
        text = self.text() if callable(self.text) else self.text
        ttk.Label(self.window, text=text, background=theme.color('surface_alt'), padding=5).pack()

    def hide(self, event=None):
        if self.timer:
            self.widget.after_cancel(self.timer)
            self.timer = None
        if self.window:
            self.window.destroy()
            self.window = None


from .toolbar_icons import icon


def show_start_page(app):
    """Primer paso operativo, sin ejes vacíos antes de la primera corrida."""
    page = ttk.Frame(app.book, padding=28)
    heading = ttk.Label(page, font=theme.font('heading'), wraplength=760)
    heading.pack(anchor='w', pady=(8,18))
    detail = ttk.Label(page, wraplength=760, justify='left')
    detail.pack(anchor='w', pady=(0,24))
    page.bind('<Configure>',lambda event:(heading.configure(wraplength=max(200,event.width-60)),detail.configure(wraplength=max(200,event.width-60))))
    actions = ttk.Frame(page)
    actions.pack(anchor='w')
    buttons = []
    for position,(key, command) in enumerate([('shell.load_flow',app.run_loadflow), ('shell.events',app.events),
                         ('shell.rms_simulation',app.simulation_dialog), ('int2.view_diagram',app.open_diagram),
                         ('study.manager',app.study_selector)]):
        button = ttk.Button(actions, command=command)
        button.grid(row=position//2,column=position%2,sticky='ew',padx=4,pady=5)
        buttons.append((button,key))
    def retranslate():
        heading.configure(text=tr('int2.start_heading'))
        detail.configure(text=tr('int2.start_detail'))
        for button,key in buttons:
            button.configure(text=tr(key))
        app.book.tab(page,text=tr('int2.start_title'))
    app.book.add(page,text=tr('int2.start_title'))
    page.retranslate = retranslate
    app.welcome_page = page
    retranslate()
    app.book.select(page)
    return page


class WorkspaceNotebook(ttk.Notebook):
    """Notebook compatible con barra propia, títulos completos y cierre individual."""
    def __init__(self, parent, *, on_close, can_close):
        style = ttk.Style(parent)
        style.layout('Workspace.TNotebook.Tab', [])
        super().__init__(parent, style='Workspace.TNotebook')
        self._disposing=False
        self.on_close, self.can_close = on_close, can_close
        self.bar = ttk.Frame(parent, padding=(0, 2))
        self.bar.pack(fill='x')
        ttk.Button(self.bar, text='◀', width=2, command=lambda: self.scroll(-1)).pack(side='left')
        self.canvas = tk.Canvas(self.bar, height=28, highlightthickness=0, background=theme.color('bg'))
        self.canvas.pack(side='left', fill='x', expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window(0, 0, window=self.inner, anchor='nw')
        self.list_button = ttk.Menubutton(self.bar, text='▾', width=2)
        self.list_button.pack(side='right')
        self.list_menu = tk.Menu(self.list_button, tearoff=False)
        self.list_button.configure(menu=self.list_menu)
        ttk.Button(self.bar, text='▶', width=2, command=lambda: self.scroll(1)).pack(side='right')
        self.buttons, self.signature = {}, None
        self.inner.bind('<Configure>', self.layout_tabs)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<Configure>', lambda event: self.ensure_visible())
        self.bind('<<NotebookTabChanged>>', lambda event: self.sync(), add='+')

    def add(self, child, **kwargs):
        super().add(child, **kwargs)
        child.bind('<Destroy>', lambda event: self.schedule_sync() if event.widget == child else None, add='+')
        self.sync()

    def tab(self, tab_id, option=None, **kwargs):
        value = super().tab(tab_id, option, **kwargs)
        if kwargs:
            self.sync()
        return value

    def sync(self):
        if not self.winfo_exists():
            return
        tabs = self.tabs()
        signature = tuple((tab, super(WorkspaceNotebook,self).tab(tab,'text'), self.can_close(tab)) for tab in tabs)
        if signature != self.signature:
            self.signature = signature
            for child in self.inner.winfo_children():
                child.destroy()
            self.buttons.clear()
            self.list_menu.delete(0,'end')
            for tab, title, closable in signature:
                frame = ttk.Frame(self.inner)
                frame.pack(side='left', padx=(0,2))
                button = ttk.Button(frame, text=title, padding=(8,4), command=lambda page=tab: self.select(page))
                button.pack(side='left')
                self.buttons[tab] = (frame, button)
                if closable:
                    ttk.Button(frame, text='×', width=2, padding=2, command=lambda page=tab: self.on_close(page)).pack(side='left')
                for widget in [frame]+frame.winfo_children():
                    widget.bind('<MouseWheel>', self.wheel)
                self.list_menu.add_command(label=title, command=lambda page=tab: self.select(page))
        for tab, (_, button) in self.buttons.items():
            button.state(['pressed'] if tab == self.select() else ['!pressed'])
        self.after_idle(self.ensure_visible)

    def schedule_sync(self):
        if not self._disposing and not getattr(self.winfo_toplevel(),'_closed',False):
            self.after_idle(self.sync)

    def destroy(self):
        self._disposing=True
        from .panel_context import cancel_panel_timers
        cancel_panel_timers(self)
        super().destroy()

    def layout_tabs(self,event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        self.after_idle(self.ensure_visible)

    def ensure_visible(self):
        if not self.winfo_exists() or self.select() not in self.buttons:
            return
        frame = self.buttons[self.select()][0]
        start, width = frame.winfo_x(), frame.winfo_width()
        left, view = self.canvas.canvasx(0), self.canvas.winfo_width()
        total = max(1, self.inner.winfo_reqwidth())
        if view <= 1 or not self.canvas.winfo_ismapped():
            return
        if width >= view:
            if abs(left-start)>1:self.canvas.xview_moveto(start/total)
        elif start < left:
            self.canvas.xview_moveto(start/total)
        elif start+width > left+view:
            self.canvas.xview_moveto(max(0,start+width-view)/total)

    def scroll(self, direction):
        self.canvas.xview_scroll(direction*6, 'units')

    def wheel(self, event):
        self.scroll(-1 if event.delta > 0 else 1)
        return 'break'


def build_workspace(app):
    app.case_label = tk.StringVar(value=tr('shell.loading_data'))
    from .ribbon import build_ribbon
    build_ribbon(app)
    welcome=ttk.Frame(app,padding=(10,5))
    welcome.pack(fill='x')
    ttk.Label(welcome,text=tr('shell.network_data_and_topology_study_document_scenarios'),style='Muted.TLabel').pack(side='left')
    ttk.Button(welcome,text=tr('shell.run_load_flow_f10'),command=app.run_loadflow).pack(side='right')
    ttk.Button(welcome,text=tr('study.manager'),command=app.study_selector).pack(side='right',padx=5)
    ttk.Button(welcome,text=tr('study.run_history'),command=app.run_history_dialog).pack(side='right',padx=5)
    refresh_toolbar_icons(app)
    welcome.pack_forget()
    app.output_visible=tk.BooleanVar(value=True)
    app.vertical=ttk.Panedwindow(app,orient='vertical')
    app.vertical.pack(fill='both',expand=True)
    app.horizontal=ttk.Panedwindow(app.vertical,orient='horizontal')
    app.vertical.add(app.horizontal,weight=4)
    app.data_host=ttk.Frame(app.horizontal,width=260)
    app.horizontal.add(app.data_host,weight=1)
    center=ttk.Frame(app.horizontal,padding=(2,0,2,0))
    app.horizontal.add(center,weight=4)
    app.book=WorkspaceNotebook(center,on_close=app.close_page,
        can_close=lambda tab: str(app.diagrams.get(None)) != str(tab))
    app.book.bind('<<NotebookTabChanged>>',app.refresh_tab_navigation,add='+')
    app.book.pack(fill='both',expand=True)
    from .properties_dock import PropertiesDock
    app.properties_dock=PropertiesDock(app.horizontal,app)
    app.horizontal.add(app.properties_dock,weight=1)
    app.output=OutputPanel(app.vertical)
    if hasattr(app.output,'set_select_callback'):
        app.output.set_select_callback(app.select)
    app.vertical.add(app.output,weight=1)
    app.state_text=tk.StringVar(value=tr('shell.ready'))
    app.lf_state_text=tk.StringVar(value=tr('shell.lf_pending'))
    app.study_state_text=tk.StringVar(value=tr('shell.study_no_result'))
    app.time_text=tk.StringVar(value='')
    app.step_text=tk.StringVar(value='')
    app.validity_text=tk.StringVar(value=tr('shell.domain_awaiting_evaluation'))
    app.status_fields=[]
    def field(variable,width,formatter=lambda text:text,tooltip=None):
        display=tk.StringVar()
        label=ttk.Label(app.status,textvariable=display,width=width,anchor='w',padding=(4,2),font=theme.font('small'))
        label.pack(side='left',fill='x',expand=variable is app.case_label)
        def update(*args):
            raw=variable.get()
            value='' if '—' in raw else formatter(raw).replace('\n',' ')
            display.set(value if len(value)<=width else value[:width-1]+'…')
        variable.trace_add('write',update)
        update()
        Tooltip(label,tooltip or variable.get)
        ttk.Separator(app.status,orient='vertical').pack(side='left',fill='y',pady=3)
        app.status_fields.append(label)
        return label
    field(app.case_label,36)
    def compact_lf(text):
        if text.startswith(('LF · Flujo convergente en', 'LF · Load flow converged in')):
            numbers=re.findall(r'\d+',text)
            return tr('shell.lf_ok')+(numbers[1]+tr('shell.islands_without_supply') if len(numbers)>1 else tr('shell.converged'))
        return text.replace('LF · Flujo convergente',tr('shell.lf_converged')).replace('LF · pendiente tras cambios',tr('shell.lf_pending_511521'))
    field(app.lf_state_text,20,compact_lf)
    field(app.study_state_text,20,lambda text:text.replace(tr('shell.study'),'').replace('calculando o finalizando cancelación',tr('shell.computing_cancelling')))
    field(app.time_text,18,lambda text:text.replace('t = ','t=').replace(' / ','/'))
    field(app.step_text,12)
    app.progress=ttk.Progressbar(app.status,length=80,maximum=100)
    app.progress.pack(side='left',padx=4)
    app.validity_label=field(app.validity_text,30,lambda text: text,tooltip=lambda:getattr(app,'pf_evidence_text',''))
    app.validity_label.bind('<Button-1>',lambda event:app.show_validity())
    app.evidence_button=ttk.Button(app.status,text=tr('int2.evidence'),command=app.show_validity,style='Compact.TButton')
    app.evidence_button.pack(side='left',padx=3)
    Tooltip(app.status,app.state_text.get)


class EditorTabStrip(ttk.Frame):
    """Barra desplazable para páginas fijas de un editor ya construido."""
    def __init__(self,book):
        super().__init__(book.master)
        self.book,self.disposed=book,False
        book.configure(style='Workspace.TNotebook')
        self.pack(fill='x',before=book)
        ttk.Button(self,text='◀',width=2,command=lambda:self.canvas.xview_scroll(-5,'units')).pack(side='left')
        self.canvas=tk.Canvas(self,width=1,height=28,highlightthickness=0,background=theme.color('bg'))
        self.canvas.pack(side='left',fill='x',expand=True)
        ttk.Button(self,text='▶',width=2,command=lambda:self.canvas.xview_scroll(5,'units')).pack(side='right')
        self.inner=ttk.Frame(self.canvas)
        self.canvas.create_window(0,0,window=self.inner,anchor='nw')
        self.buttons={}
        for tab in book.tabs():
            button=ttk.Button(self.inner,text=book.tab(tab,'text'),command=lambda tab=tab:book.select(tab),padding=(7,3))
            button.pack(side='left')
            self.buttons[tab]=button
            button.bind('<MouseWheel>',self.wheel)
        self.canvas.bind('<MouseWheel>',self.wheel)
        self.inner.bind('<Configure>',self.layout)
        book.bind('<<NotebookTabChanged>>',self.layout,add='+')

    def wheel(self,event):
        self.canvas.xview_scroll(-5 if event.delta>0 else 5,'units')
        return 'break'

    def layout(self,event=None):
        if self.disposed:return
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))
        self.after_idle(self.reveal)

    def reveal(self):
        if self.disposed or self.book.select() not in self.buttons:return
        for tab,button in self.buttons.items():button.state(['pressed'] if tab==self.book.select() else ['!pressed'])
        button=self.buttons[self.book.select()]
        x,width=button.winfo_x(),button.winfo_width()
        left,view=self.canvas.canvasx(0),self.canvas.winfo_width()
        total=max(1,self.inner.winfo_reqwidth())
        if x<left:self.canvas.xview_moveto(x/total)
        elif x+width>left+view:self.canvas.xview_moveto(max(0,x+width-view)/total)

    def destroy(self):
        self.disposed=True
        from .panel_context import cancel_panel_timers
        cancel_panel_timers(self)
        super().destroy()


class MenuRegistry(dict):
    """Stable identifiers, with aliases only at the phase 2 migration boundary."""
    aliases = {'Archivo': 'file', 'Edición': 'edit', 'Ver': 'view', 'Cálculo': 'calculation', 'Simulación RMS': 'rms', 'Gráficos': 'plots', 'Herramientas': 'tools', 'Ventana': 'window', 'Ayuda': 'help'}
    def __getitem__(self, key):
        return super().__getitem__(self.aliases.get(key, key))


def refresh_toolbar_icons(app):
    app.toolbar_images = []
    for button in app.action_buttons + [app.stop_button]:
        n = button.action_id
        images = {state: icon(app, n, state=state) for state in ('normal','active','disabled')}
        app.toolbar_images.extend(images.values())
        button.configure(image=(images['normal'], 'disabled', images['disabled'], 'active', images['active']))
