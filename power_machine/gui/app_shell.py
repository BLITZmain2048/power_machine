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
    from .start_page import show_start_page as build
    return build(app)



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
        self.previous_button=ttk.Button(self.bar,text='◀',width=2,command=lambda:self.scroll(-1))
        self.previous_button.pack(side='left')
        self.canvas = tk.Canvas(self.bar, height=26, highlightthickness=0, background=theme.color('bg'))
        self.canvas.pack(side='left', fill='x', expand=True)
        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window(0, 0, window=self.inner, anchor='nw')
        self.list_button = ttk.Menubutton(self.bar, text='', width=2)
        self.list_button.pack(side='right')
        self.list_menu = tk.Menu(self.list_button, tearoff=False)
        self.list_button.configure(menu=self.list_menu)
        self.next_button=ttk.Button(self.bar,text='▶',width=2,command=lambda:self.scroll(1))
        self.next_button.pack(side='right')
        self.buttons, self.signature = {}, None
        self.inner.bind('<Configure>', self.layout_tabs)
        self.canvas.bind('<MouseWheel>', self.wheel)
        self.canvas.bind('<Configure>', self.layout_tabs)
        self.bar.bind('<Configure>',self.layout_tabs)
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
                button = ttk.Button(frame, text=title, padding=(6,2), command=lambda page=tab: self.select(page))
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
        overflow=self.inner.winfo_reqwidth()>max(1,self.bar.winfo_width()-12)
        if self.bar.winfo_width()>10:
            for button,side in ((self.previous_button,'left'),(self.list_button,'right'),(self.next_button,'right')):
                if overflow and not button.winfo_manager():button.pack(side=side,before=self.canvas)
                elif not overflow and button.winfo_manager():button.pack_forget()
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
    from .chrome import BrandBar, CompactToolbar, SegmentedStatus
    from .analysis_workspace import AnalysisWorkspace
    from .load_tools import open_load_tools
    app.case_label=tk.StringVar(value=tr('shell.loading_data'))
    app.brand=BrandBar(app,'v3.window.analysis',app.case_label)
    app.brand.pack(fill='x')
    app.configuration_text=tk.StringVar(value=tr('shell.load_flow'))
    app.toolbar=CompactToolbar(app)
    app.toolbar.pack(fill='x')
    app.case_caption=ttk.Label(app.toolbar,text=tr('v3.case'),style='Toolbar.TLabel')
    app.case_caption.pack(side='left',padx=(0,5))
    app.case_selector=ttk.Combobox(app.toolbar,state='readonly',width=28)
    app.case_selector.pack(side='left',padx=3)
    app.toolbar.separator()
    app.run_button=app.toolbar.action('v3.run',lambda:app.analysis.run_active(),'run',run=True)
    app.stop_button=app.toolbar.action('shell.stop',app.stop,6,shortcut='Esc')
    app.stop_button.state(['disabled'])
    app.toolbar.separator()
    app.toolbar.action('study.manager',app.study_selector,9)
    app.toolbar.action('tools.diagram',app.open_diagram,'network',shortcut='F9')
    from .chrome import ElidedLabel
    app.configuration_label=ElidedLabel(app.toolbar,app.configuration_text,style='Toolbar.TLabel')
    app.configuration_label.pack(side='left',fill='x',expand=True,padx=8)
    app.action_buttons=[button for button,_,_ in app.toolbar.buttons if button is not app.stop_button]
    app.toolbar_images=[]
    app.menus['edit'].add_command(label=tr('tools.scale_loads'),command=lambda:open_load_tools(app))
    from .ribbon import edit_network
    app.menus['edit'].add_command(label=tr('tools.edit_network'),command=lambda:edit_network(app))
    app.menus['edit'].add_command(label=tr('tools.data_short'),command=app.open_data_table)
    app.output_visible=tk.BooleanVar(value=True)
    app.vertical=ttk.Panedwindow(app,orient='vertical')
    app.vertical.pack(fill='both',expand=True)
    app.book=AnalysisWorkspace(app.vertical,app)
    app.vertical.add(app.book,weight=1)
    app.horizontal=app.analysis.panes
    app.book.bind('<<NotebookTabChanged>>',app.refresh_tab_navigation,add='+')
    app.case_selector.bind('<<ComboboxSelected>>',app.analysis.activate_case)
    app.output=OutputPanel(app.analysis.result_hosts['log'])
    app.output.pack(fill='both',expand=True)
    app.output.set_select_callback(app.select)
    app.windows.ensure_network()
    app.state_text=tk.StringVar(value=tr('shell.ready'))
    app.lf_state_text=tk.StringVar(value=tr('shell.lf_pending'))
    app.study_state_text=tk.StringVar(value=tr('shell.study_no_result'))
    app.time_text=tk.StringVar(value='')
    app.step_text=tk.StringVar(value='')
    app.validity_text=tk.StringVar(value=tr('shell.domain_awaiting_evaluation'))
    app.selection_text=tk.StringVar(value=tr('v3.no_selection'))
    app.status_segments=SegmentedStatus(app.status,mode=app.state_text,selection=app.selection_text,
        pf=app.validity_text,case=app.case_label,evidence=app.show_validity)
    app.status_segments.pack(fill='x')
    app.status_fields=app.status_segments.labels
    app.progress=app.status_segments.progress
    app.validity_label=app.status_segments.labels[2]
    app.evidence_button=app.status_segments.evidence_button
    Tooltip(app.validity_label,lambda:getattr(app,'pf_evidence_text',''))
    app.validity_label.bind('<Button-1>',lambda event:app.show_validity())



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
    if hasattr(app.toolbar,'refresh_icons'):
        app.toolbar.refresh_icons()
        return
    app.toolbar_images = []
    for button in app.action_buttons + [app.stop_button]:
        n = button.action_id
        images = {state: icon(app, n, state=state) for state in ('normal','active','disabled')}
        app.toolbar_images.extend(images.values())
        button.configure(image=(images['normal'], 'disabled', images['disabled'], 'active', images['active']))
