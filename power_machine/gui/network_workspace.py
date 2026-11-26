"""Network window composition. Phase B owns explorer, equipment and inspector."""
import tkinter as tk
from tkinter import ttk
from .chrome import BrandBar, CompactToolbar, PanelHeader, SegmentedStatus
from .i18n import tr
from .properties_dock import PropertiesDock


class NetworkWorkspace(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent)
        self.app=app
        self.brand=BrandBar(self,'v3.window.network',app.case_label)
        self.brand.pack(fill='x')
        self.toolbar=CompactToolbar(self)
        self.toolbar.pack(fill='x')
        from .ribbon import edit_network
        self.toolbar.action('tools.edit_network',lambda:edit_network(app),'network')
        self.toolbar.action('shell.undo',app.undo,'undo',shortcut='Ctrl+Z')
        self.toolbar.action('shell.redo',app.redo,'redo',shortcut='Ctrl+Y')
        self.toolbar.separator()
        self.toolbar.action('tools.data_short',app.open_data_table,9)
        self.focus_var=tk.BooleanVar(self,False)
        self.focus_button=ttk.Checkbutton(self.toolbar,text=tr('v3.expand_workspace'),variable=self.focus_var,command=self.toggle_focus)
        self.focus_button.pack(side='right',padx=8)
        self.panes=ttk.Panedwindow(self,orient='horizontal')
        self.panes.pack(fill='both',expand=True)
        self.explorer=ttk.Frame(self.panes,style='Panel.TFrame',width=260)
        PanelHeader(self.explorer,'v3.explorer').pack(fill='x')
        self.explorer_host=ttk.Frame(self.explorer,style='Panel.TFrame')
        self.explorer_host.pack(fill='both',expand=True)
        self.document_host=ttk.Frame(self.panes)
        self.documents=ttk.Notebook(self.document_host,style='Doc.TNotebook')
        self.documents.pack(fill='both',expand=True)
        self.single_line=ttk.Frame(self.documents)
        self.diagram_book=ttk.Notebook(self.single_line,style='Hidden.TNotebook')
        self.diagram_book.pack(fill='both',expand=True)
        self.equipment_host=ttk.Frame(self.documents)
        from .equipment_table import EquipmentTable
        self.equipment=EquipmentTable(self.equipment_host,app)
        self.equipment.pack(fill='both',expand=True)
        self.documents.add(self.single_line,text=tr('v3.single_line'))
        self.documents.add(self.equipment_host,text=tr('v3.equipment'))
        self.inspector=PropertiesDock(self.panes,app)
        for widget,weight in ((self.explorer,0),(self.document_host,1),(self.inspector,0)):
            self.panes.add(widget,weight=weight)
        self.mode=tk.StringVar(self,tr('v3.mode.network'))
        self.selection=tk.StringVar(self,tr('v3.no_selection'))
        self.statusbar=SegmentedStatus(self,mode=self.mode,selection=self.selection,case=app.case_label)
        self.statusbar.pack(side='bottom',fill='x',before=self.panes)
        self.documents.bind('<<NotebookTabChanged>>',self._document_changed,add='+')
        self.diagram_book.bind('<<NotebookTabChanged>>',self._document_changed,add='+')
        self._tokens=[app.bus.subscribe('diagram.mode.changed',self._document_changed),app.bus.subscribe('selection.changed',self._selected),
                      app.bus.subscribe('language.changed',self.retranslate),
                      app.bus.subscribe('theme.changed',lambda **_:self.toolbar.refresh_icons())]
        self.bind('<Destroy>',self._dispose,add='+')
        self.bind('<Map>',lambda e:self.after_idle(self.initial_panes) if e.widget is self else None,add='+')

    def initial_panes(self):
        if self.focus_var.get() or self.panes.winfo_width()<200:return
        width=self.panes.winfo_width()
        sizes=self.app.settings.get('network_dividers',{})
        self.panes.sashpos(0,max(265,min(int(sizes.get('explorer',285)),int(width*.25))))
        self.panes.sashpos(1,max(580,width-min(int(sizes.get('inspector',320)),int(width*.28))))

    def save_layout(self):
        if not self.focus_var.get() and len(self.panes.panes())==3:
            self.app.settings.set('network_dividers',dict(explorer=self.panes.sashpos(0),
                inspector=self.panes.winfo_width()-self.panes.sashpos(1)))

    def toggle_focus(self):
        if self.focus_var.get():
            self._saved_sashes=[self.panes.sashpos(i) for i in (0,1)]
            self.panes.forget(self.explorer)
            self.panes.forget(self.inspector)
        else:
            self.panes.insert(0,self.explorer,weight=0)
            self.panes.add(self.inspector,weight=0)
            self.update_idletasks()
            for i,pos in enumerate(getattr(self,'_saved_sashes',())):self.panes.sashpos(i,pos)

    def _selected(self,ids,source=None):
        from .presentation import element_label
        ident=ids[0] if ids else None
        self.selection.set(element_label(self.app.index[ident],self.app.index) if ident in self.app.index else tr('v3.no_selection'))

    def _document_changed(self,event=None,**_):
        if self.documents.select()==str(self.equipment_host):
            self.mode.set(tr('v3.equipment'))
            self.statusbar.labels[1].configure(textvariable=self.selection)
        else:
            selected=self.diagram_book.select()
            if selected:
                view=self.diagram_book.nametowidget(selected)
                self.mode.set(tr('v3.int.mode.edit' if view.editing else 'v3.int.mode.view'))
                self.statusbar.labels[1].configure(textvariable=getattr(view,'status',self.selection))

    def retranslate(self,**_):
        self.brand.retranslate()
        self.toolbar.retranslate()
        self.focus_button.configure(text=tr('v3.expand_workspace'))
        self.documents.tab(self.single_line,text=tr('v3.single_line'))
        self.documents.tab(self.equipment_host,text=tr('v3.equipment'))
        self.equipment.retranslate()
        self.mode.set(tr('v3.mode.network'))
        self._selected([self.app.selected_id] if self.app.selected_id else [])
        self._document_changed()

    def _dispose(self,event):
        if event.widget is self:
            for token in self._tokens:self.app.bus.unsubscribe(token)
