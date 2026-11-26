"""Filterable equipment view using the inspector's shared edit session."""
import csv
import tkinter as tk
from tkinter import ttk,filedialog
from .i18n import tr
from .results_tables import display_value
from .properties_dock import editor_controller,object_connection


KINDS=('ElmTerm','ElmLne','ElmTr2','ElmTr3','ElmLod','ElmSym','ElmGenstat','ElmPvsys','ElmShnt','ElmCoup','StaSwitch','ElmXnet','ElmZpu')
GROUPS=('buses','branches','branches3','loads','gens','shunts','switches','external_grids')
FIELDS={'ElmTerm':('un_kv',),'ElmLod':('p_mw','q_mvar','scale'),
    'ElmSym':('p_mw','q_mvar','v_set_pu'),'ElmGenstat':('p_mw','q_mvar','v_set_pu'),
    'ElmPvsys':('p_mw','q_mvar','v_set_pu'),'ElmLne':('params.R1','params.X1','params.B1'),
    'ElmTr2':('type_params.strn','type_params.uktr','params.nntap')}
UNITS={'un_kv':'kV','p_mw':'MW','q_mvar':'Mvar','scale':'1','v_set_pu':'p.u.',
    'params.R1':'Ω','params.X1':'Ω','params.B1':'uS','type_params.strn':'MVA','type_params.uktr':'%','params.nntap':'1'}


def equipment_value(item,path):
    value=item
    for key in path.split('.'):value=value.get(key) if isinstance(value,dict) else None
    if value is None:
        native={'p_mw':'plini','q_mvar':'qlini','scale':'scale0','v_set_pu':'usetp'}
        value=item.get('params',{}).get(native.get(path,''))
    if path in ('p_mw','q_mvar') and item.get('class') in ('ElmSym','ElmGenstat','ElmPvsys'):
        from power_machine.elements import gen_pq_mw,MissingParameter
        try:value=gen_pq_mw(item)[0 if path=='p_mw' else 1]
        except (MissingParameter,ValueError):pass
    return value


def nominal_value(item):
    if item.get('class')=='ElmLod':
        return ' · '.join(display_value(equipment_value(item,key),key)+' '+unit for key,unit in [('p_mw','MW'),('q_mvar','Mvar')] if equipment_value(item,key) is not None) or '—'
    for path,unit in [('un_kv','kV'),('type_params.strn','MVA'),('type_params.sgn','MVA'),('params.sgn','MVA'),('p_mw','MW')]:
        value=equipment_value(item,path)
        if value is not None:return display_value(value,path.rsplit('.',1)[-1])+' '+unit
    return '—'


class EquipmentTable(ttk.Frame):
    def __init__(self,parent,app):
        super().__init__(parent,padding=(10,8))
        self.app=app
        self.kind=None
        self.query=tk.StringVar(self)
        self.rows=[]
        self.sort_key='name'
        self.reverse=False
        self._receiving=False
        self._inline=None
        bar=ttk.Frame(self)
        bar.pack(fill='x',pady=(0,8))
        self.type_label=ttk.Label(bar)
        self.type_label.pack(side='left')
        self.type_box=ttk.Combobox(bar,state='readonly',width=17)
        self.type_box.pack(side='left',padx=(4,12))
        self.type_box.bind('<<ComboboxSelected>>',self._type_changed)
        self.search_label=ttk.Label(bar)
        self.search_label.pack(side='left')
        ttk.Entry(bar,textvariable=self.query,width=14).pack(side='left',padx=4)
        self.export_button=ttk.Button(bar,command=self.export_csv)
        self.export_button.pack(side='right')
        self.undo_button=ttk.Button(bar,width=3,style='Toolbar.TButton',command=lambda:self.history(False))
        self.undo_button.pack(side='right',padx=3)
        self.redo_button=ttk.Button(bar,width=3,style='Toolbar.TButton',command=lambda:self.history(True))
        self.redo_button.pack(side='right')
        from .diagram.icons import Tooltip
        self.undo_tip=Tooltip(self.undo_button,tr('shell.undo')+' · Ctrl+Z')
        self.redo_tip=Tooltip(self.redo_button,tr('shell.redo')+' · Ctrl+Y')
        body=ttk.Frame(self)
        body.pack(fill='both',expand=True)
        body.rowconfigure(0,weight=1)
        body.columnconfigure(0,weight=1)
        self.tree=ttk.Treeview(body,show='headings',selectmode='extended')
        self.tree.grid(row=0,column=0,sticky='nsew')
        sy=ttk.Scrollbar(body,command=self.tree.yview)
        sx=ttk.Scrollbar(body,command=self.tree.xview,orient='horizontal')
        sy.grid(row=0,column=1,sticky='ns')
        sx.grid(row=1,column=0,sticky='ew')
        self.tree.configure(xscrollcommand=sx.set,yscrollcommand=sy.set)
        self.tree.bind('<<TreeviewSelect>>',self._select)
        self.tree.bind('<Double-1>',self._edit_cell)
        self.tree.bind('<Control-z>',lambda e:self.history(False))
        self.tree.bind('<Control-y>',lambda e:self.history(True))
        self.message=tk.StringVar(self)
        self.note=ttk.Label(self,textvariable=self.message,wraplength=650)
        self.note.pack(fill='x',pady=(6,0))
        self.query.trace_add('write',lambda *_:self.refresh())
        self._tokens=[app.bus.subscribe('theme.changed',self._icons),app.bus.subscribe('selection.changed',self._selection),app.bus.subscribe('network.edited',self.refresh),
            app.bus.subscribe('language.changed',self.retranslate),app.bus.subscribe('results.updated',self.refresh)]
        self.bind('<Destroy>',self._dispose,add='+')
        self.bind('<Map>',lambda e:self.refresh() if e.widget is self else None,add='+')
        self.retranslate()
        self._icons()

    def _icons(self,**_):
        from .toolbar_icons import icon
        self.history_icons=[icon(self,key,size=20) for key in ('undo','redo')]
        for button,image in zip((self.undo_button,self.redo_button),self.history_icons):button.configure(image=image)

    def _type_changed(self,event=None):
        index=self.type_box.current()
        self.kind=KINDS[index-1] if index>0 else None
        self.refresh()

    def set_kind(self,kind):
        self.kind=kind
        self.type_box.current(KINDS.index(kind)+1 if kind else 0)
        self.refresh()

    def refresh(self,**_):
        if not hasattr(self,'tree'):return
        model=getattr(self.app,'model',{}) or {}
        index=getattr(self.app,'index',{})
        needle=self.query.get().casefold().strip()
        self.rows=[]
        for group in GROUPS:
            for item in model.get(group,[]):
                if self.kind and item.get('class')!=self.kind:continue
                connection=object_connection(item,index)
                if needle and needle not in ' '.join((item.get('name',''),item.get('id',''),item.get('class',''),connection)).casefold():continue
                self.rows.append(dict(item,connection=connection,nominal=nominal_value(item)))
        fields=FIELDS.get(self.kind,('p_mw','q_mvar','un_kv'))
        self.columns=('name','class','connection',*(() if self.kind=='ElmLod' else ('nominal',)),*fields)
        self.tree.configure(columns=self.columns)
        for key in self.columns:
            label=tr('v3.red.column.'+key) if key in ('name','class','connection','nominal','p_mw','q_mvar','scale','v_set_pu','un_kv') else key.rsplit('.',1)[-1]
            if key in UNITS and UNITS[key]!='1':label+=' ['+UNITS[key]+']'
            self.tree.heading(key,text=label,command=lambda k=key:self.sort(k))
            short_kind=self.kind in ('ElmLod','ElmTerm','ElmLne','ElmCoup','StaSwitch')
            widths=dict(name=120,**{'class':75 if short_kind else 155},connection=90,nominal=110,p_mw=85,q_mvar=90,scale=65)
            self.tree.column(key,width=widths.get(key,100),minwidth=85 if key=='name' else 55,
                anchor='w' if key in ('name','class','connection') else 'e',stretch=key==self.columns[-1])
        def order(item):
            value=equipment_value(item,self.sort_key)
            if value is None:return (2,'')
            return (0,float(value)) if isinstance(value,(int,float)) else (1,str(value).casefold())
        present=[item for item in self.rows if equipment_value(item,self.sort_key) is not None]
        missing=[item for item in self.rows if equipment_value(item,self.sort_key) is None]
        self.rows=sorted(present,key=order,reverse=self.reverse)+missing
        self._receiving=True
        try:
            self.tree.delete(*self.tree.get_children())
            for item in self.rows:
                values=[(tr('v3.red.kind.'+item['class']) if item['class'] in KINDS else item['class']) if key=='class' else display_value(equipment_value(item,key),key.rsplit('.',1)[-1]) for key in self.columns]
                self.tree.insert('','end',iid=item['id'],values=values)
            ident=getattr(self.app,'selected_id',None)
            if ident and self.tree.exists(ident):self.tree.selection_set(ident)
        finally:self._receiving=False
        self.message.set(tr('v3.red.table_note',count=len(self.rows)))

    def sort(self,key):
        self.reverse=not self.reverse if self.sort_key==key else False
        self.sort_key=key
        self.refresh()

    def _select(self,event=None):
        if not self._receiving:
            ids=list(self.tree.selection())
            if ids:self.app.bus.publish('selection.changed',ids=ids,source='equipment')

    def _selection(self,ids,source=None):
        if source=='equipment':return
        if ids and (self.kind or self.query.get()) and not any(self.tree.exists(key) for key in ids):
            # A selection from another view must remain discoverable in Equipos.
            self.kind=None
            self.type_box.current(0)
            self.query.set('')
        ids=[key for key in ids if self.tree.exists(key)]
        if self.tree.selection()!=tuple(ids):
            self._receiving=True
            try:
                self.tree.selection_set(ids)
                if ids:self.tree.see(ids[0])
            finally:self._receiving=False

    def commit_cell(self,ident,path,value):
        self.app.select(ident)
        dock=self.app.properties_dock
        if dock.current_id!=ident or not dock.editor:dock.show_element(ident)
        dock.stage_field(path,value)
        if not dock.apply_changes():
            self.message.set(dock.message.get())
            return False
        self.refresh()
        return True

    def _edit_cell(self,event):
        ident=self.tree.identify_row(event.y)
        column=self.tree.identify_column(event.x)
        if not ident or not column:return
        path=self.columns[int(column[1:])-1]
        if path in ('class','connection','nominal'):return
        self.app.select(ident)
        dock=self.app.properties_dock
        if not dock.editor or path not in dock.editor.variables:return
        original=dock.editor.variables[path].get()
        bounds=self.tree.bbox(ident,column)
        if not bounds:return
        if self._inline:self._inline.destroy()
        edit=ttk.Entry(self.tree,justify='right')
        edit.insert(0,dock.edit_value(path))
        edit.place(x=bounds[0],y=bounds[1],width=bounds[2],height=bounds[3])
        self._inline=edit
        edit.focus_set()
        edit.select_range(0,'end')
        def finish(commit=True):
            if self._inline is not edit:return
            if commit and not self.commit_cell(ident,path,edit.get()):return
            if not commit and dock.current_id==ident and dock.editor:
                dock.stage_field(path,original)
                dock.message.set('')
            self._inline=None
            edit.destroy()
        edit.bind('<Return>',lambda e:finish())
        edit.bind('<Escape>',lambda e:finish(False))
        return edit

    def history(self,redo):
        controller=editor_controller(self.app)
        (controller.redo if redo else controller.undo)()
        self.app.properties_dock.show_element(getattr(self.app,'selected_id',None),force=True)
        self.refresh()
        return 'break'

    def export_csv(self,path=None):
        if path is None:path=filedialog.asksaveasfilename(parent=self,defaultextension='.csv',filetypes=[('CSV','*.csv')])
        if not path:return None
        with open(path,'w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.writer(stream)
            writer.writerow(['ID']+[self.tree.heading(key,'text') for key in self.columns])
            for item in self.rows:writer.writerow([item['id']]+[equipment_value(item,key) for key in self.columns])
        return path

    def retranslate(self,**_):
        self.type_label.configure(text=tr('v3.red.type'))
        self.search_label.configure(text=tr('v3.red.filter'))
        self.export_button.configure(text=tr('v3.red.export'))
        self.undo_tip.text=tr('shell.undo')+' · Ctrl+Z'
        self.redo_tip.text=tr('shell.redo')+' · Ctrl+Y'
        self.type_box.configure(values=[tr('v3.red.all')]+[tr('v3.red.kind.'+key) for key in KINDS])
        self.type_box.current(KINDS.index(self.kind)+1 if self.kind else 0)
        self.refresh()

    def _dispose(self,event):
        if event.widget is self:
            self.undo_tip.hide()
            self.redo_tip.hide()
            for token in self._tokens:self.app.bus.unsubscribe(token)
