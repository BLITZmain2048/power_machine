"""Single-column inspector backed by the shared ElementEditor and controller."""
import tkinter as tk
from tkinter import ttk
from .i18n import tr
from .theme import theme
from .results_tables import display_value


def editor_controller(app):
    from .netedit.integration import get_editor_controller
    return get_editor_controller(app)


def ensure_session(app):return editor_controller(app).session


def object_connection(item,index):
    ports=[item.get(k) for k in ('bus','bus_from','bus_to','bus_hv','bus_mv','bus_lv','bus1','bus2') if item.get(k)]
    return ' — '.join(index.get(key,{}).get('name',key) for key in ports)


class PropertiesDock(ttk.Frame):
    """Present ElementEditor variables inline, preserving its validation.

    stage_field and apply_changes are shared with EquipmentTable. The existing
    ElementEditor owns the values and controller.edit. No second model is used.
    """
    def __init__(self,parent,app):
        super().__init__(parent,style='Inspector.TFrame',width=320)
        self.app,self.editor,self.current_id=app,None,None
        self._styles()
        self.dirty=self._applying=False
        self._traces=[]
        self.collapsed={'shortcircuit','rms'}
        self.rows={}
        from .chrome import PanelHeader
        PanelHeader(self,'v3.inspector').pack(fill='x')
        head=ttk.Frame(self,style='Inspector.TFrame',padding=12)
        head.pack(fill='x')
        self.code=ttk.Label(head,text='·',style='Inspector.Code.TLabel')
        self.code.pack(side='left',padx=(0,9),anchor='n')
        titles=ttk.Frame(head,style='Inspector.TFrame')
        titles.pack(fill='x',expand=True)
        self.heading=ttk.Label(titles,style='Inspector.Title.TLabel',wraplength=220)
        self.heading.pack(fill='x')
        self.subtitle=ttk.Label(titles,style='Inspector.Key.TLabel',wraplength=220,font=theme.font('small'))
        self.subtitle.pack(fill='x',pady=(3,0))
        self.body=ttk.Frame(self,style='Inspector.TFrame')
        self.body.pack(fill='both',expand=True)
        self.canvas=tk.Canvas(self.body,highlightthickness=0,bg=theme.color('panel'),width=300)
        sy=ttk.Scrollbar(self.body,command=self.canvas.yview)
        sy.pack(side='right',fill='y')
        self.canvas.pack(side='left',fill='both',expand=True)
        self.canvas.configure(yscrollcommand=sy.set)
        self.canvas.bind('<MouseWheel>',self._wheel)
        self.fields=ttk.Frame(self.canvas,style='Inspector.TFrame',padding=(10,0,10,8))
        self._content=self.canvas.create_window(0,0,window=self.fields,anchor='nw')
        self.fields.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self._content,width=e.width))
        self.footer=ttk.Frame(self,style='Inspector.TFrame',padding=8)
        self.apply_button=ttk.Button(self.footer,command=self.apply_changes)
        self.apply_button.pack(side='right')
        self.discard_button=ttk.Button(self.footer,command=self.discard_changes)
        self.discard_button.pack(side='right',padx=5)
        self.message=tk.StringVar(self)
        self.error=ttk.Label(self,textvariable=self.message,style='Error.TLabel',wraplength=300)
        self.note=ttk.Label(self,style='Inspector.Key.TLabel',wraplength=300,font=theme.font('small'),padding=(10,8))
        self.note.pack(side='bottom',fill='x',before=self.body)
        self.message.trace_add('write',self._message_changed)
        self._tokens=[app.bus.subscribe('theme.changed',self._theme),app.bus.subscribe('language.changed',self.retranslate),
            app.bus.subscribe('results.updated',self._results_changed),app.bus.subscribe('results.stale',self._results_changed),
            app.bus.subscribe('network.edited',self._network_changed)]
        self.bind('<Destroy>',self._dispose,add='+')
        self.show_element(None)
        self.retranslate()

    def _clear_editor(self):
        for variable,token in self._traces:
            try:variable.trace_remove('write',token)
            except tk.TclError:pass
        self._traces=[]
        if self.editor:self.editor.destroy()
        self.editor=None

    def show_element(self,ident,*,force=False):
        if self._applying:return
        if ident==self.current_id and self.dirty and not force:return
        self._clear_editor()
        self.current_id=ident
        self.dirty=False
        self.message.set('')
        self.footer.pack_forget()
        if ident:
            from .element_dialog import ElementEditor
            controller=editor_controller(self.app)
            try:self.editor=ElementEditor(self,controller,ident,on_applied=self.applied)
            except (ValueError,KeyError):self.current_id=None
            if self.editor:
                self.baseline={key:var.get() for key,var in self.editor.variables.items()}
                for key,var in self.editor.variables.items():
                    self._traces.append((var,var.trace_add('write',lambda *_,path=key:self._changed(path))))
                self.code.configure(text=str(self.editor.item.get('name',ident)).split(' · ')[0][:9])
                self._object_heading()
        if not self.editor:
            self.code.configure(text='·')
            self.heading.configure(text=tr('tools.properties'))
            self.subtitle.configure(text=tr('tools.select_element'))
        self._render()

    def _section_for(self,path):
        label=self.editor._field_labels.get(path)
        tab=None
        if label:
            for key,content in self.editor.contents.items():
                if label.master is content:tab=key;break
        if tab in ('shortcircuit','rms'):return tab
        if path.startswith('type_params.') or path.rsplit('.',1)[-1] in ('sgn','un_kv','strn','utrn_h','utrn_l'):return 'nameplate'
        return 'identification' if tab in (None,'basic') else tab

    def _object_heading(self):
        from .equipment_table import KINDS,nominal_value
        item=self.editor.item
        self.heading.configure(text=tr('v3.red.kind.'+item['class']) if item['class'] in KINDS else item['class'])
        self.subtitle.configure(text=' · '.join(part for part in (object_connection(item,self.app.index),nominal_value(item)) if part and part!='—'))

    def _render(self):
        self._inline=None
        for child in self.fields.winfo_children():child.destroy()
        self.rows={}
        if not self.editor:return
        groups={key:[] for key in ('identification','nameplate','loadflow','shortcircuit','rms')}
        for path in self.editor.variables:groups[self._section_for(path)].append(path)
        for key,paths in groups.items():
            holder=self._section(key)
            if key in self.collapsed:continue
            if not paths:ttk.Label(holder,text=tr('v3.red.no_parameters'),style='Inspector.Key.TLabel',wraplength=270).pack(fill='x',pady=5)
            for path in paths:self._row(holder,path)
        self._result_holder=self._section('results')
        if 'results' not in self.collapsed:self._render_results()

    def _section(self,key):
        outer=ttk.Frame(self.fields,style='Inspector.TFrame')
        outer.pack(fill='x',pady=(8,0))
        button=ttk.Button(outer,text=('▸ ' if key in self.collapsed else '▾ ')+tr('v3.red.section.'+key),
            command=lambda:self._toggle(key),style='Inspector.RedSection.TButton')
        button.pack(fill='x')
        button.bind('<MouseWheel>',self._wheel)
        holder=ttk.Frame(outer,style='Inspector.TFrame')
        holder.pack(fill='x')
        return holder

    def _toggle(self,key):
        if key in self.collapsed:self.collapsed.remove(key)
        else:self.collapsed.add(key)
        self._render()

    def _row(self,parent,path):
        row=ttk.Frame(parent,style='Inspector.TFrame',padding=(0,6))
        row.pack(fill='x')
        row.columnconfigure(0,weight=1)
        label=tr('edit.outserv') if path=='outserv' else tr('edit.fixed_voltage') if path=='rms_fixed_voltage' else self.editor.field_label(path)
        ttk.Label(row,text=label,style='Inspector.RedKey.TLabel',wraplength=155).grid(row=0,column=0,sticky='w')
        value=ttk.Label(row,style='Inspector.RedValue.TLabel',cursor='hand2',anchor='e',wraplength=150)
        value.grid(row=0,column=1,sticky='e',padx=(5,0))
        unit=self.editor.units.get(path,'')
        if unit and unit!='1':ttk.Label(row,text=unit,style='Inspector.RedKey.TLabel',wraplength=95).grid(row=0,column=2,sticky='e',padx=(4,0))
        ttk.Separator(parent).pack(fill='x')
        self.rows[path]=(row,value)
        self._update_value(path)
        if path not in self.editor._port_fields:value.bind('<Button-1>',lambda e,p=path:self.begin_edit(p))
        for child in [row,*row.winfo_children()]:child.bind('<MouseWheel>',self._wheel)

    def _wheel(self,event):
        self.canvas.yview_scroll(-1 if event.delta>0 else 1,'units')
        return 'break'

    def display_field(self,path):
        value=self.editor.variables[path].get()
        if isinstance(value,bool):return tr('data.state.out_of_service' if value else 'data.state.in_service') if path=='outserv' else tr('v3.red.yes' if value else 'v3.red.no')
        if path=='mode':return tr('edit.mode.'+value) if value else '—'
        if path in self.editor.numeric:
            try:return display_value(float(value),path.rsplit('.',1)[-1]) if str(value).strip() else '—'
            except ValueError:return str(value)
        return str(value) if value else '—'

    def edit_value(self,path):
        """Return the complete source value for an input, without display rounding."""
        from .i18n import language
        value=str(self.editor.variables[path].get())
        return value.replace('.',',') if path in self.editor.numeric and language()=='es' else value

    def _update_value(self,path):
        if path in self.rows:
            changed=self.editor.variables[path].get()!=self.baseline[path]
            self.rows[path][1].configure(text=self.display_field(path)+(' •' if changed else ''),foreground=theme.color('accent') if changed else theme.color('ink'))

    def _changed(self,path):
        if not self.editor:return
        self.dirty=any(var.get()!=self.baseline[key] for key,var in self.editor.variables.items())
        self._update_value(path)
        if self.dirty:self.footer.pack(side='bottom',fill='x',before=self.note)
        else:self.footer.pack_forget()

    def _message_changed(self,*_):
        if self.message.get():self.error.pack(side='bottom',fill='x',padx=10,before=self.body)
        else:self.error.pack_forget()

    def stage_field(self,path,value):
        if not self.editor or path not in self.editor.variables or path in self.editor._port_fields:raise ValueError(tr('v3.red.not_editable'))
        if path in self.editor.numeric:value=str(value).replace(',','.')
        self.editor.variables[path].set(value)

    def begin_edit(self,path):
        if getattr(self,'_inline',None):self._inline.destroy()
        row,label=self.rows[path]
        variable=self.editor.variables[path]
        choices=[tr('v3.red.no'),tr('v3.red.yes')] if isinstance(variable,tk.BooleanVar) else [tr('edit.mode.constq'),tr('edit.mode.constv')] if path=='mode' else None
        edit=ttk.Combobox(row,values=choices,state='readonly',width=16) if choices else ttk.Entry(row,justify='right',width=16)
        if choices:edit.current(int(variable.get()) if isinstance(variable,tk.BooleanVar) else (0 if variable.get()=='constq' else 1))
        else:edit.insert(0,self.edit_value(path))
        edit.grid(row=0,column=1,sticky='e',padx=(5,0))
        self._inline=edit
        edit.focus_set()
        if not choices:edit.select_range(0,'end')
        def finish(commit=True):
            if self._inline is not edit:return
            if commit:
                value=bool(edit.current()) if isinstance(variable,tk.BooleanVar) else ('constq','constv')[edit.current()] if path=='mode' else edit.get()
                self.stage_field(path,value)
            self._inline=None
            edit.destroy()
        edit.bind('<Return>',lambda e:finish())
        edit.bind('<Escape>',lambda e:finish(False))
        edit.bind('<FocusOut>',lambda e:finish())
        self._finish_inline=finish
        return edit

    def apply_changes(self):
        if not self.editor:return False
        editor=self.editor
        self._applying=True
        try:ok=editor.apply()
        finally:self._applying=False
        if ok:
            self.show_element(self.current_id,force=True)
        else:self.message.set(editor.message.get())
        return ok

    def discard_changes(self):self.show_element(self.current_id,force=True)
    def applied(self,*_,**__):
        self.app.state_text.set(tr('tools.results_stale'))
        if not self._applying:self.show_element(self.current_id,force=True)

    def _render_results(self):
        holder=getattr(self,'_result_holder',None)
        if not holder or not holder.winfo_exists():return
        for child in holder.winfo_children():child.destroy()
        result=getattr(self.app,'result',None) or {}
        row=next((r for group in ('buses','branches','gens','loads','branches3') for r in result.get(group,[]) if r.get('id')==self.current_id),{})
        if result:
            for view in getattr(self.app,'diagrams',{}).values():
                if self.current_id in view.results:row={**row,**view.results[self.current_id]};break
        self.result_values={}
        for key,unit in [('u_pu','p.u.'),('p_mw','MW'),('q_mvar','Mvar'),('loading_pct','%')]:
            value=row.get(key)
            if value is None and key in ('p_mw','q_mvar'):value=next((row[k] for k in (key.replace('_','_from_',1),key.replace('_','_hv_',1)) if row.get(k) is not None),None)
            if value is None:continue
            self.result_values[key]=value
            line=ttk.Frame(holder,style='Inspector.TFrame',padding=(0,6))
            line.pack(fill='x')
            ttk.Label(line,text=tr('v3.red.result.'+key),style='Inspector.Key.TLabel').pack(side='left')
            ttk.Label(line,text=display_value(value,key)+' '+unit,style='Inspector.RedValue.TLabel').pack(side='right')
        if not self.result_values:ttk.Label(holder,text=tr('v3.red.no_results'),style='Inspector.Key.TLabel',wraplength=270).pack(fill='x',pady=6)

    def _results_changed(self,**_):self._render_results()
    def _network_changed(self,**_):
        if not self.dirty and not self._applying:self.show_element(self.current_id,force=True)
    def _theme(self,**_):
        self._styles()
        self.canvas.configure(bg=theme.color('panel'))
        if self.editor:
            for path in self.rows:self._update_value(path)
    def _styles(self):
        style=ttk.Style(self)
        style.configure('Inspector.RedValue.TLabel',font=theme.font('body'),background=theme.color('panel'),foreground=theme.color('ink'),anchor='e')
        style.configure('Inspector.RedKey.TLabel',font=theme.font('small'),background=theme.color('panel'),foreground=theme.color('muted'))
        style.configure('Inspector.RedSection.TButton',font=(*theme.font('small'),'bold'),background=theme.color('panel'),
            foreground=theme.color('muted'),borderwidth=0,lightcolor=theme.color('panel'),darkcolor=theme.color('panel'),
            bordercolor=theme.color('panel'),padding=(0,5),anchor='w')
        style.map('Inspector.RedSection.TButton',background=[('pressed',theme.color('select')),('active',theme.color('soft'))])
    def retranslate(self,**_):
        if getattr(self,'_inline',None):self._finish_inline()
        self.apply_button.configure(text=tr('v3.red.apply'))
        self.discard_button.configure(text=tr('v3.red.discard'))
        self.note.configure(text=tr('v3.red.inspector_note'))
        if self.editor:
            self._object_heading()
            self._render()
        else:
            self.heading.configure(text=tr('tools.properties'))
            self.subtitle.configure(text=tr('tools.select_element'))
    def _dispose(self,event):
        if event.widget is self:
            for token in self._tokens:self.app.bus.unsubscribe(token)
            self._clear_editor()
