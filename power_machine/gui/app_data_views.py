"""Montaje del gestor tabular en ventana amplia y del árbol en el lateral."""
from copy import deepcopy
from .i18n import tr
from .theme import theme
import tkinter as tk
from tkinter import ttk
from .datamanager import DataManager


class IntegratedDataManager(DataManager):
    """Evita reprogramar un filtro por los valores que acaba de presentar."""
    def _build_tree(self):
        def descendants(node=''):
            for child in self.tree.get_children(node):
                yield child
                yield from descendants(child)
        expanded=[node for node in descendants() if self.tree.item(node,'open')]
        selected=[node for node in self.tree.selection() if node in self.index]
        if getattr(self,'_network_explorer',False):
            self._build_network_tree(expanded,selected)
            return
        super()._build_tree()
        self.tree.configure(style='Explorer.Treeview')
        self.tree.tag_configure('section',foreground=theme.color('muted'),font=theme.font('small'))
        for node in self.tree.get_children():self.tree.item(node,tags=('section',))
        from .datamanager import GROUPS
        from .presentation import element_label
        for group,_,cls in GROUPS:
            parent=f'group:{group}:{cls or "all"}'
            if not self.tree.exists(parent):continue
            for item in self.model.get(group,[]):
                ident=item.get('id')
                if not ident or self.tree.exists(ident) or cls and item.get('class')!=cls:continue
                self.tree.insert(parent,'end',iid=ident,text=element_label(item,self.index))
        for node in expanded:
            if self.tree.exists(node):self.tree.item(node,open=True)
        selected=[node for node in selected if self.tree.exists(node)]
        if selected:
            self.tree.selection_set(selected)
            self.tree.see(selected[0])

    def _theme_changed(self,**payload):
        super()._theme_changed(**payload)
        self.tree.tag_configure('section',foreground=theme.color('muted'))
        if hasattr(self,'explorer_painter'):self.explorer_painter.repaint()

    def _build_network_tree(self,expanded,selected):
        from .datamanager import GROUPS
        from .equipment_table import nominal_value
        from .properties_dock import object_connection
        self.tree.delete(*self.tree.get_children())
        self.tree.insert('','end',iid='network',text=tr('v3.red.section.network'),tags=('section',),open=True)
        for group,key,cls in GROUPS:
            items=[item for item in self.model.get(group,[]) if cls is None or item.get('class')==cls]
            if not items:continue
            node=f'group:{group}:{cls or "all"}'
            self.tree.insert('network','end',iid=node,text=tr(key),values=(tr('v3.red.count',count=len(items)),),open=any(item['id'] in selected for item in items))
            for item in items:
                ident=item['id']
                subtitle=' · '.join(filter(None,(object_connection(item,self.index),nominal_value(item))))
                self.tree.insert(node,'end',iid=ident,text=item.get('name',ident)+'\n'+subtitle,values=(subtitle,))
        self.tree.insert('','end',iid='studies',text=tr('v3.red.section.cases'),tags=('section',),open=True)
        self.tree.insert('studies','end',iid='active-case',text=self.case_name,values=(tr('v3.red.active_case'),))
        self.tree.insert('studies','end',iid='scenarios',text=tr('data.tree.scenarios'),open='scenarios' in expanded)
        self.tree.insert('studies','end',iid='variants',text=tr('data.tree.variants'),open='variants' in expanded)
        if self.document:
            data=self.document.to_data()
            for row in data.get('operating_scenarios',[]):self.tree.insert('scenarios','end',iid='scenario:'+row['scenario_id'],text=tr(row['default_name_key']) if row.get('default_name_key') else row['name'])
            for variant in data.get('network_variants',[]):
                parent='variant:'+variant['variant_id']
                self.tree.insert('variants','end',iid=parent,text=variant['name'],open=parent in expanded)
                for stage in variant.get('stages',[]):self.tree.insert(parent,'end',iid='stage:'+stage['stage_id'],text=stage['name'])
        self.tree.insert('studies','end',iid='events',text=tr('data.tree.events'),open='events' in expanded)
        from .results_tables import display_value
        for number,event in enumerate(self.events):self.tree.insert('events','end',iid=f'event:{number}',text=tr('events.kind.'+event.kind),values=(display_value(event.time_s,'time_s')+' s · '+str(event.target_id),))
        self.tree.insert('','end',iid='results',text=tr('v3.red.section.results'),tags=('section',),open=True)
        for number,run in enumerate(self.runs):self.tree.insert('results','end',iid=f'run:{number}',text=run['name'])
        self.tree.insert('results','end',iid='results-compare',text=tr('data.tree.compare_runs'))
        self.tree.insert('','end',iid='library',text=tr('v3.red.section.library'),tags=('section',),open=True)
        self.tree.insert('library','end',iid='library:types',text=tr('v3.red.type_library'),values=(tr('v3.red.source_types'),))
        for ident in selected:
            if self.tree.exists(ident):self.tree.selection_set(ident);self.tree.see(ident)
        if hasattr(self,'explorer_painter'):self.explorer_painter.schedule()

    def _tree_selected(self,event=None):
        selected=self.tree.selection()
        if selected and selected[0] in self.index:
            self.on_select(selected[0])
        else:super()._tree_selected(event)

    def locate(self,ident):
        if ident and self.tree.exists(ident):
            if getattr(self,'_network_explorer',False):
                for node in self.tree.get_children('network'):
                    self.tree.item(node,open=node==self.tree.parent(ident))
            self.tree.selection_set(ident)
            self.tree.see(ident)
        return super().locate(ident)

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
        if child is not pane:child.pack_forget()
    manager.configure(padding=(0,0))
    manager._network_explorer=True
    manager._build_tree()
    manager.tree.column('#0',width=280,minwidth=240,stretch=True)
    manager.explorer_painter=ExplorerPainter(manager)
    def open_node(event=None):
        selection=manager.tree.selection()
        if not selection:return
        node=selection[0]
        if node.startswith('group:'):
            _,group,kind=node.split(':',2)
            app.open_data_table(group,kind if kind!='all' else None)
        elif node=='library:types':show_source_types(app)
        else:app.open_node(node)
    manager.tree.bind('<Double-1>',open_node)
    manager.explorer_painter.open_node=open_node


def show_source_types(app):
    from .type_library import source_types
    from .datamanager import readonly_table
    window=tk.Toplevel(app.windows.window)
    window.title(tr('v3.red.type_library'))
    window.geometry('760x520')
    rows=[]
    for source in source_types(app.model):
        for key,value in source['params'].items():rows.append((source['type_id']+' · '+key,value,source['units'].get(key,'')))
    readonly_table(window,rows or [(tr('v3.red.type_library'),tr('v3.red.no_parameters'),'')])
    theme.apply(window)
    return window


class ExplorerPainter:
    """Full-text tooltips for the native two-line ttk explorer rows."""
    def __init__(self,manager):
        from tkinter import font
        self.manager,self.tree=manager,manager.tree
        self.font=font.Font(self.tree,font=theme.font('body'))
        self.small=font.Font(self.tree,font=theme.font('small'))
        self.canvas=tk.Canvas(self.tree,highlightthickness=0,bg=theme.color('panel'))
        # The native tree paints its normal-sized disclosure indicator and two-line rows.
        self.canvas.place_forget()
        self.job=self.tip_job=self.tip=None
        self.open_node=lambda event:None
        self.canvas.bind('<Button-1>',self.click)
        self.canvas.bind('<Double-1>',lambda e:self.open_node(e))
        self.canvas.bind('<MouseWheel>',self.wheel)
        self.canvas.bind('<Motion>',self.hover)
        self.tree.bind('<Motion>',self.hover,add='+')
        self.tree.bind('<Leave>',lambda e:self.hide_tip(),add='+')
        self.canvas.bind('<Leave>',lambda e:self.hide_tip())
        self.tree.bind('<Configure>',lambda e:self.schedule(),add='+')
        self.tree.bind('<<TreeviewSelect>>',lambda e:self.schedule(),add='+')
        self.tree.bind('<<TreeviewOpen>>',lambda e:self.schedule(),add='+')
        self.tree.bind('<<TreeviewClose>>',lambda e:self.schedule(),add='+')
        original=self.tree.cget('yscrollcommand')
        self._scroll_bounds=None
        def scroll(*args):
            if original:self.tree.tk.call(original,*args)
            if self._scroll_bounds!=args:
                self._scroll_bounds=args
                self.schedule()
        self.tree.configure(yscrollcommand=scroll)
        self.tree.bind('<Destroy>',self.dispose,add='+')
        self.repaint()

    def schedule(self):
        if self.job is None:self.job=self.tree.after_idle(self.paint)

    def repaint(self):
        ttk.Style(self.tree).configure('Red.Explorer.Treeview',background=theme.color('panel'),fieldbackground=theme.color('panel'),rowheight=43)
        self.tree.configure(style='Red.Explorer.Treeview')
        self.canvas.configure(bg=theme.color('panel'))
        self.schedule()

    def elide(self,text,width,font=None):
        font=font or self.font
        if font.measure(text)<=width:return text
        while text and font.measure(text+'…')>width:text=text[:-1]
        return text+'…'

    def paint(self):
        self.job=None
        if not self.tree.winfo_exists():return
        self.full_text={}
        def visit(parent=''):
            for ident in self.tree.get_children(parent):
                row=self.tree.item(ident)
                title=row['text'].split('\n',1)[0]
                subtitle=str(row['values'][0]) if row['values'] else ''
                self.full_text[ident]=title+('\n'+subtitle if subtitle else '')
                visit(ident)
        visit()

    def click(self,event):
        ident=self.tree.identify_row(event.y)
        if not ident:return
        self.tree.focus_set()
        self.tree.focus(ident)
        self.tree.selection_set(ident)
        if self.tree.get_children(ident):self.tree.item(ident,open=not self.tree.item(ident,'open'))
        self.manager._tree_selected()
        self.schedule()

    def wheel(self,event):
        self.hide_tip()
        self.tree.yview_scroll(-3 if event.delta>0 else 3,'units')
        self.schedule()
        return 'break'

    def hover(self,event):
        ident=self.tree.identify_row(event.y)
        if getattr(self,'hovered',None)==ident:return
        self.hide_tip()
        self.hovered=ident
        if ident:
            text=self.full_text.get(ident,self.tree.item(ident,'text'))
            if (ident=='active-case' or ident.startswith('case:')) and self.tree.item(ident,'text').rstrip().endswith('*'):text+='\n'+tr('v3.polish.unsaved')
            self.tip_job=self.tree.after(650,lambda:self.show_tip(text,event.x_root,event.y_root))

    def show_tip(self,text,x,y):
        self.tip_job=None
        self.tip=tk.Toplevel(self.tree)
        self.tip.overrideredirect(True)
        self.tip.geometry(f'+{x+12}+{y+16}')
        ttk.Label(self.tip,text=text,padding=7,wraplength=400).pack()
        theme.apply(self.tip)

    def hide_tip(self):
        if self.tip_job:self.tree.after_cancel(self.tip_job);self.tip_job=None
        if self.tip:self.tip.destroy();self.tip=None

    def dispose(self,event):
        if event.widget is self.tree:
            self.hide_tip()
            if self.job:self.tree.after_cancel(self.job);self.job=None


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
