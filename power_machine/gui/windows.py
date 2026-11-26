"""Diagram containers independent of the study notebook."""
import tkinter as tk
from tkinter import ttk
from .i18n import tr
from .theme import theme
from .settings import settings

class DiagramWindow(tk.Toplevel):
    def __init__(self,app,bus,manager=None):
        super().__init__(app)
        self.app,self.bus,self.manager=app,bus,manager
        self.title(tr('window.diagram'))

        try:self.geometry((manager.settings if manager else settings).get('diagram_geometry','1200x760'))
        except tk.TclError:self.geometry('1200x760')
        from .network_workspace import NetworkWorkspace
        self.workspace=NetworkWorkspace(self,app)
        self.workspace.pack(fill='both',expand=True)
        from .chrome import MenuBar
        self.menu_bar=MenuBar(self.workspace,app.menus)
        self.menu_bar.pack(fill='x',after=self.workspace.brand)
        self._title_trace=app.case_label.trace_add('write',lambda *_:self.refresh_title())
        self.refresh_title()
        self.book=self.workspace.diagram_book
        app.data_host=self.workspace.explorer_host
        app.properties_dock=self.workspace.inspector
        self.status=tk.StringVar(value=tr('window.diagram')+' · '+tr('diagram.focus_hint'))
        self.status_label=self.workspace.statusbar.labels[1]
        self.book.bind('<<NotebookTabChanged>>',self._sync_status)
        self.protocol('WM_DELETE_WINDOW',manager.hide_diagram if manager else self.withdraw)
        self._tokens=[bus.subscribe('language.changed',lambda **p:self.retranslate())]
        self.bind('<Destroy>',self._destroy,add='+')
        theme.apply(self)
    def _sync_status(self, event=None):
        selected = self.book.select()
        if selected:
            view = self.book.nametowidget(selected)
            variable = getattr(view,'status',self.status)
            self.status_label.configure(textvariable=variable)
            # The detached host supplies the status bar. Docked widgets keep theirs.
            if hasattr(view,'_status_label'):
                view._status_label.pack_forget()
            if hasattr(view,'_detail_panel'):view._detail_panel.pack_forget()
            if not getattr(view,'_network_chrome_attached',False):
                view.detail_expanded=False
                view._network_chrome_attached=True

    def _destroy(self,event):
        if event.widget is self:
            self.app.case_label.trace_remove('write',self._title_trace)
            for token in self._tokens:self.bus.unsubscribe(token)
    def refresh_title(self):
        self.title(tr('v3.window.network')+' · '+self.app.case_label.get()+' · Power Machine')

    def retranslate(self):
        self.refresh_title()
        self.status.set(tr('window.diagram')+' · '+tr('diagram.focus_hint'))

class WindowManager:
    def __init__(self,app,bus,settings_store=None):
        self.app,self.bus=app,bus
        self.settings=settings_store or settings
        self.window=None
        self.docked=bool(self.settings.get('docked',False))
        self._dock_book=None
    def ensure_network(self):
        if self.window is None or not self.window.winfo_exists():
            self.window=DiagramWindow(self.app,self.bus,self)
        return self.window
    def show_diagram(self):
        if self.docked:
            if self._dock_book is None or not self._dock_book.winfo_exists():
                self._dock_book=ttk.Notebook(self.app.book)
                self.app.book.add(self._dock_book,text=tr('window.diagram'))
            self.app.book.select(self._dock_book)
            return self._dock_book
        self.ensure_network()
        self.window.workspace.documents.select(self.window.workspace.single_line)
        self.window.deiconify()
        self.window.lift()
        return self.window.book
    def hide_diagram(self):
        if self.window and self.window.winfo_exists():
            self.window.workspace.save_layout()
            self.settings.set('diagram_geometry',self.window.geometry())
            self.window.withdraw()
    def dock_diagram(self,value):
        value=bool(value)
        if value==self.docked:return
        self.app.capture_presentation()
        self.hide_diagram()
        snapshots = {group:dict(positions=dict(view.positions), scale=view.scale, offset=tuple(view.offset))
                     for group,view in self.app.diagrams.items()}
        for view in list(self.app.diagrams.values()):view.destroy()
        self.app.diagrams.clear()
        if self._dock_book is not None:
            self._dock_book.destroy()
            self._dock_book=None
        self.docked=value
        self.settings.set('docked',value)
        for group, snapshot in (snapshots or {None:None}).items():
            self.app.open_diagram(group)
            view = self.app.diagrams.get(group)
            if view is not None and snapshot:
                view.positions = snapshot['positions']
                view._rebuild_scene()
                view.scale, view.offset = snapshot['scale'], snapshot['offset']
                view.draw()
    def focus_other(self):
        focused=self.app.focus_get()
        if self.window and focused is not None and focused.winfo_toplevel() is self.window:
            self.app.lift()
            self.app.focus_force()
        else:
            self.show_diagram()
            if self.window and not self.docked:self.window.focus_force()
    def save(self):
        self.settings.set('main_geometry',self.app.geometry())
        if self.window and self.window.winfo_exists():
            self.settings.set('diagram_geometry',self.window.geometry())
            self.window.workspace.save_layout()
        self.settings.save()
