"""Montaje del gestor tabular en ventana amplia y del árbol en el lateral."""
from copy import deepcopy
from .i18n import tr
from .theme import theme
import tkinter as tk
from tkinter import ttk
from .datamanager import DataManager


class IntegratedDataManager(DataManager):
    """Evita reprogramar un filtro por los valores que acaba de presentar."""
    def schedule_filter(self,*args):
        if not getattr(self,'presenting',False):super().schedule_filter(*args)

    def populate_table(self):
        self.presenting=True
        try:return super().populate_table()
        finally:self.presenting=False


class DataTableWindow(IntegratedDataManager):
    def __init__(self,parent,app):
        self.application=app
        super().__init__(parent,app.operational,app.api.substation_of,app.properties,app.select,app.open_node)
        ttk.Label(self,text=tr('tools.data'),wraplength=700).pack(fill='x',before=self.winfo_children()[0])

    def apply_edits(self,edits,label):
        if not self.application.available():return None
        result=super().apply_edits(edits,label)
        if result and result.get('accepted'):
            if edits and all(e['field'] in ('p_mw','q_mvar') for e in edits):
                self.application.network_changed()
                self.application._tools_network_revision=(id(self.application.document),self.application.document.revision)
            else:self.application.update_document()
        return result


def compact_data_manager(app,manager):
    if not hasattr(manager,'table'):return
    # El árbol existente conserva los IDs, las clases y sus eventos.
    pane=manager.tree.master.master
    if not isinstance(pane,ttk.Panedwindow):return
    for tab in pane.panes():
        if str(tab)!=str(manager.tree.master):pane.forget(tab)
    for child in list(manager.winfo_children()):
        if child is not pane and not isinstance(child,ttk.Label):child.pack_forget()
    ttk.Button(manager,text=tr('shell.table_filters_and_editing'),command=app.open_data_table).pack(fill='x',before=pane,pady=(0,5))
    manager.tree.column('#0',width=310,stretch=False)
    xbar=ttk.Scrollbar(manager.tree.master,orient='horizontal',command=manager.tree.xview)
    xbar.grid(row=1,column=0,sticky='ew')
    manager.tree.configure(xscrollcommand=xbar.set)
    def open_node(event=None):
        selection=manager.tree.selection()
        if not selection:return
        node=selection[0]
        if node.startswith('group:'):
            _,group,kind=node.split(':',2)
            app.open_data_table(group,kind if kind!='all' else None)
        else:app.open_node(node)
    manager.tree.bind('<Double-1>',open_node)


def open_data_table(app,group='loads',kind=None):
    if not app.document:return
    window=tk.Toplevel(app)
    window.title(tr('shell.data_manager_table_and_filters_295036'))
    window.geometry('1150x650')
    window.minsize(1000,500)
    manager=DataTableWindow(window,app)
    manager.pack(fill='both',expand=True)
    manager.current_group,manager.current_class=group,kind
    app.data_windows=getattr(app,'data_windows',[])+[(window,manager)]
    refresh_data_windows(app)
    window.update_idletasks()
    pane=manager.tree.master.master
    if isinstance(pane,ttk.Panedwindow):pane.sashpos(0,255)
    return window


def refresh_data_windows(app):
    active=[]
    for window,manager in getattr(app,'data_windows',[]):
        if not window.winfo_exists():continue
        manager.model=app.operational
        manager.index=app.index
        manager.events=app.document.study.events
        manager.runs=app.data_manager.runs
        manager.case_name=app.document.study.name
        changed=manager.document is not app.document
        manager.set_document(app.document)
        manager.set_result(app.result)
        if changed and hasattr(app,'restore_data_table_presentation'):app.restore_data_table_presentation(manager)
        active.append((window,manager))
    app.data_windows=active
