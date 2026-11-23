"""Shared element editor contract for diagram dialogs and docked properties."""
from copy import deepcopy
import math
import tkinter as tk
from tkinter import ttk
from .i18n import tr
from .theme import theme


class ElementEditor(ttk.Frame):
    """Edit controller element in place, with Basic/LF/SC/RMS tabs.

    Stable public constructor: ElementEditor(parent, controller, object_id,
    *, on_applied=None). Apply validates fields, calls controller.edit and calls
    on_applied(object_id) on success. A draft is edited and completed only after
    its required fields pass validation. No electrical defaults are invented.
    Public fields: variables (path -> Tk variable), message, notebook, apply().
    """

    def __init__(self,parent,controller,object_id,*,on_applied=None):
        super().__init__(parent,padding=10)
        self.controller,self.object_id,self.on_applied=controller,object_id,on_applied
        self.readonly=False
        self.item=deepcopy(controller.drafts.get(object_id) or controller.session.get_element(object_id))
        if not self.item:
            raise ValueError(tr('edit.missing_element'))
        self.variables,self.numeric,self.required,self.units={},set(),set(),{}
        self._field_labels={}
        self._text_widgets=[]
        self._port_fields=set()
        self.message=tk.StringVar(self)
        self.notebook=ttk.Notebook(self)
        ttk.Style(self).configure('Element.TNotebook.Tab',padding=(4,3),font=theme.font('small'))
        self.notebook.configure(style='Element.TNotebook')
        self.notebook.pack(fill='both',expand=True)
        self.tabs={}
        self.contents={}
        self._scrolls=[]
        from .panels.panel_widgets import ScrollForm
        for key in ('basic','loadflow','shortcircuit','rms'):
            page=ttk.Frame(self.notebook,padding=10)
            self.notebook.add(page,text=tr('edit.tab.'+key))
            scroll=ScrollForm(page)
            scroll.hint.pack_forget()
            scroll.pack(fill='both',expand=True)
            self._scrolls.append(scroll)
            self.tabs[key]=page
            self.contents[key]=scroll.content
            scroll.content.columnconfigure(1,weight=1)
        self._build()
        self._add_type_library()
        for scroll in self._scrolls:scroll.wire()
        footer=ttk.Frame(self)
        footer.pack(fill='x',pady=(8,0))
        ttk.Label(footer,textvariable=self.message,wraplength=480,style='Error.TLabel').pack(side='left',fill='x',expand=True)
        self.apply_button=ttk.Button(footer,text=tr('edit.apply'),command=self.apply)
        self.apply_button.pack(side='right')
        from .bus import bus
        self._language_subscription=bus.subscribe('language.changed',self.retranslate)
        self.bind('<Destroy>',self._destroy,add='+')

    def _destroy(self,event):
        if event.widget is self:
            from .bus import bus
            bus.unsubscribe(self._language_subscription)
            self.variables.clear()
            for key,value in tuple(vars(self).items()):
                if isinstance(value,tk.Variable):setattr(self,key,None)

    def _field(self,tab,path,unit='',required=False,choices=None):
        data=self.item
        for part in path.split('.'):
            data=data.get(part) if isinstance(data,dict) else None
        page=self.contents[tab]
        row=len(page.grid_slaves(column=0))
        label=ttk.Label(page,text=self.field_label(path)+(' ['+unit+']' if unit else '')+(' *' if required else ''),wraplength=155)
        label.grid(row=row,column=0,sticky='w',padx=(0,12),pady=4)
        self._field_labels[path]=label
        var=tk.StringVar(self,'' if data is None else str(data))
        self.variables[path]=var
        if required:self.required.add(path)
        if unit:self.numeric.add(path)
        self.units[path]=unit
        if path=='mode':
            widget=ttk.Frame(page)
            for code in ('constq','constv'):
                radio=ttk.Radiobutton(widget,text=tr('edit.mode.'+code),value=code,variable=var)
                radio.pack(anchor='w')
                self._text_widgets.append((radio,'edit.mode.'+code))
        else:
            widget=ttk.Combobox(page,textvariable=var,values=choices,state='readonly',width=16) if choices is not None else ttk.Entry(page,textvariable=var,width=16)
        widget.grid(row=row,column=1,sticky='ew',pady=4)
        if path in self._port_fields and self.object_id not in self.controller.drafts:
            widget.configure(state='disabled')

    @staticmethod
    def field_label(path):
        known={'name','un_kv','p_mw','q_mvar','scale','v_set_pu','q_min_mvar','q_max_mvar','mode','bus','bus_from','bus_to','bus_hv','bus_mv','bus_lv','bus1','bus2'}
        return tr('edit.field.'+path) if path in known else path.rsplit('.',1)[-1]

    def _build(self):
        from power_machine.elements import gen_pq_mw,gen_q_limits_mvar,MissingParameter
        kind=self.item.get('class')
        if kind=='ElmLod':
            for public,native in [('p_mw','plini'),('q_mvar','qlini'),('scale','scale0')]:
                self.item.setdefault(public,self.item.get('params',{}).get(native))
        if kind in ('ElmSym','ElmGenstat','ElmPvsys'):
            try:
                p,q=gen_pq_mw(self.item)
                self.item.setdefault('p_mw',p)
                self.item.setdefault('q_mvar',q)
                low,high=gen_q_limits_mvar(self.item)
                self.item.setdefault('q_min_mvar',low)
                self.item.setdefault('q_max_mvar',high)
            except (MissingParameter,ValueError,TypeError):pass
            self.item.setdefault('v_set_pu',self.item.get('params',{}).get('usetp'))
        self._field('basic','name',required=True)
        if kind=='ElmXnet':
            self._field('loadflow','params.bustp',required=True,choices=['SL','PV','PQ'])
            for key,unit in [('usetp','p.u.'),('phiini','deg'),('pgini','MW'),('qgini','Mvar')]:
                self._field('loadflow','params.'+key,unit,True)
            for key,unit in [('snss','MVA'),('snssmin','MVA'),('rntxn','1'),('rntxnmin','1')]:
                self._field('shortcircuit','params.'+key,unit)
            for key,unit in [('ikss','kA'),('ikssmin','kA'),('xntrn','1'),('xntrnmin','1')]:
                self._field('shortcircuit','params.'+key,unit)
            self._field('rms','params.tag','s')
            self._field('rms','params.cmax','1')
            self.variables['rms_fixed_voltage']=tk.BooleanVar(self,bool(self.item.get('rms_fixed_voltage',False)))
            fixed=ttk.Checkbutton(self.contents['rms'],text=tr('edit.fixed_voltage'),variable=self.variables['rms_fixed_voltage'])
            fixed.grid(row=10,columnspan=2,sticky='w')
            self._text_widgets.append((fixed,'edit.fixed_voltage'))

        if kind=='ElmTerm':self._field('basic','un_kv','kV',True)
        ports=self.controller.ports(self.object_id)
        buses=sorted(b['id'] for b in self.controller.model.get('buses',[]))
        self._port_fields={p['field'] for p in ports}
        for port in ports:self._field('basic',port['field'],required=self.object_id in self.controller.drafts,choices=['']+buses)
        self.variables['outserv']=tk.BooleanVar(self,bool(self.item.get('outserv')))
        state=ttk.Checkbutton(self.contents['basic'],text=tr('edit.outserv'),variable=self.variables['outserv'])
        state.grid(row=20,columnspan=2,sticky='w',pady=8)
        self._text_widgets.append((state,'edit.outserv'))
        if kind in ('ElmLod','ElmSym','ElmGenstat','ElmPvsys'):
            self._field('loadflow','p_mw','MW',True)
            self._field('loadflow','q_mvar','Mvar',True)
            if kind=='ElmLod':self._field('loadflow','scale','1',True)
            else:
                self._field('loadflow','mode',choices=['constq','constv'])
                self._field('loadflow','v_set_pu','p.u.',True)
                self._field('loadflow','q_min_mvar','Mvar')
                self._field('loadflow','q_max_mvar','Mvar')
        for outer,unit_map in [('params','units'),('type_params','type_units')]:
            for key,value in self.item.get(outer,{}).items():
                if outer+'.'+key in self.variables:continue
                if kind in ('ElmLod','ElmSym','ElmGenstat','ElmPvsys') and key in ('plini','qlini','scale0','pgini','qgini','usetp','q_min','q_max','outserv'):
                    continue
                if not (value is None or isinstance(value,(int,float))):continue
                tab='rms' if key in ('h','H','inertia','tag','damping','xd','xq','xd1','xq1','td01','tq01') else 'shortcircuit' if key in ('R0','X0','r0','x0','uk0tr','xdp') else 'loadflow'
                unit=self.item.get(unit_map,{}).get(key,'1' if key in ('nlnum','ntnum','ngnum','scale0','ncapa','ncapx') else tr('edit.unit_missing'))
                self._field(tab,outer+'.'+key,unit,value is None and self.object_id in self.controller.drafts)
        for tab in ('shortcircuit','rms'):
            if not self.contents[tab].grid_slaves():
                note=ttk.Label(self.contents[tab],text=tr('edit.no_parameters'),wraplength=460)
                note.grid(sticky='w')
                self._text_widgets.append((note,'edit.no_parameters'))

    def retranslate(self,**payload):
        """Translate an open editor while preserving its pending field values."""
        from .i18n import translate_text
        for key,page in self.tabs.items():
            self.notebook.tab(page,text=tr('edit.tab.'+key))
        for path,label in self._field_labels.items():
            unit=translate_text(self.units[path])
            self.units[path]=unit
            label.configure(text=self.field_label(path)+(' ['+unit+']' if unit else '')+(' *' if path in self.required else ''))
        for widget,key in self._text_widgets:widget.configure(text=tr(key))
        self.apply_button.configure(text=tr('edit.apply'))
        if self.message is not None:self.message.set(translate_text(self.message.get()))

    def values(self):
        changes={}
        for path,var in self.variables.items():
            value=var.get()
            if path not in ('outserv','rms_fixed_voltage'):
                value=value.strip()
                if path in self.required and not value:raise ValueError(tr('edit.required',field=self.field_label(path)))
                if path in self.numeric:
                    try:value=float(value) if value else None
                    except ValueError:raise ValueError(tr('edit.invalid_number',field=self.field_label(path),unit=self.units[path])) from None
                    if value is not None and not math.isfinite(value):raise ValueError(tr('edit.finite'))
                else:value=value or None
            target=changes
            parts=path.split('.')
            for part in parts[:-1]:target=target.setdefault(part,{})
            target[parts[-1]]=value
        for key in ('un_kv','v_set_pu'):
            if changes.get(key) is not None and changes[key]<=0:raise ValueError(tr('edit.positive',field=tr('edit.field.'+key)))
        if changes.get('scale') is not None and changes['scale']<0:raise ValueError(tr('edit.nonnegative_scale'))
        if self.item.get('class')=='ElmXnet':
            from power_machine.external_grid import impedance
            candidate=deepcopy(self.item)
            from power_machine.netedit.commands import merge_fields
            merge_fields(candidate,changes)
            buses={b['id']:b for b in self.controller.model.get('buses',[])}
            kv=buses[candidate['bus']]['un_kv']
            for case in ('maximum','minimum'):impedance(candidate,kv,case=case)
        if self.item.get('class') in ('ElmSym','ElmGenstat','ElmPvsys'):
            if changes.get('mode')=='constv' and changes.get('v_set_pu') is None:raise ValueError(tr('edit.required',field=tr('edit.field.v_set_pu')))
            if changes.get('mode')=='constq' and changes.get('q_mvar') is None:raise ValueError(tr('edit.required',field=tr('edit.field.q_mvar')))
            low,high=changes.get('q_min_mvar'),changes.get('q_max_mvar')
            if low is not None and high is not None and low>high:raise ValueError(tr('edit.q_limits'))
            if low is not None or high is not None:
                params={**self.item.get('params',{}),**changes.get('params',{})}
                types={**self.item.get('type_params',{}),**changes.get('type_params',{})}
                sgn=types.get('sgn') if self.item['class']=='ElmSym' else params.get('sgn')
                if self.item['class']=='ElmPvsys' and sgn is not None:sgn=sgn/1000
                count=params.get('ngnum',types.get('ngnum'))
                if sgn is None or count is None or sgn<=0 or count<=0:raise ValueError(tr('edit.generator_rating_required'))
                for public,native in [('q_min_mvar','q_min'),('q_max_mvar','q_max')]:
                    if changes.get(public) is not None:
                        changes.setdefault('params',{})[native]=changes[public]/(sgn*count)
                        changes.setdefault('units',{})[native]='p.u.'
        if self.object_id not in self.controller.drafts:
            for path in self._port_fields:changes.pop(path,None)
        return changes

    def set_readonly(self,value=True):
        self.readonly=value
        self.apply_button.configure(state='disabled' if value else 'normal')
        self.message.set(tr('edit.readonly') if value else '')

    def _add_type_library(self):
        from .type_library import source_types
        self.available_types=[t for t in source_types(self.controller.model) if t['instance_class']==self.item['class']]
        if not self.available_types:return
        page=self.contents['basic']
        self.type_choice=tk.StringVar(self)
        label=ttk.Label(page,text=tr('edit.type_library'))
        label.grid(row=21,column=0,sticky='w')
        self._text_widgets.append((label,'edit.type_library'))
        box=ttk.Combobox(page,textvariable=self.type_choice,state='readonly',values=[t['type_id'] for t in self.available_types])
        box.grid(row=21,column=1,sticky='ew')
        def choose(event):
            selected=self.available_types[box.current()]
            self._selected_type=selected
            for key,value in selected['params'].items():
                path='type_params.'+key
                if path in self.variables:self.variables[path].set(str(value))
        box.bind('<<ComboboxSelected>>',choose)

    def apply(self):
        """Validate and apply through the controller, returning success as bool."""
        if self.readonly:
            self.message.set(tr('edit.readonly'))
            return False
        try:
            changes=self.values()
            if hasattr(self,'_selected_type'):
                value=self._selected_type
                changes.update(type_id=value['type_id'],type_class=value['type_class'],
                    type_params={**value['params'],**changes.get('type_params',{})},type_units=value['units'])
            self.controller.edit(self.object_id,changes)
            if self.object_id in self.controller.drafts:
                self.controller.complete_draft(self.object_id,self.controller.drafts[self.object_id])
            if self.message is not None:self.message.set(tr('edit.applied'))
            if self.on_applied:self.on_applied(self.object_id)
            return True
        except (ValueError,TypeError,KeyError) as exc:
            if self.message is not None:self.message.set(str(exc))
            return False


def open_element_dialog(root,controller,object_id):
    """Return a Toplevel hosting ElementEditor for the given controller element.

    The caller owns the returned window. Applying keeps it open for review.
    The editor is available as window.editor. No additional Tk root is created.
    """
    window=tk.Toplevel(root)
    window.title(tr('edit.title'))
    window.geometry('650x560')
    window.editor=ElementEditor(window,controller,object_id)
    window.title(element_title(window.editor.item,controller.model))
    window.editor.pack(fill='both',expand=True)
    theme.apply(window)
    return window


def element_title(item, model):
    buses={b['id']:b for b in model.get('buses',[])}
    bus=buses.get(item.get('bus',item.get('bus_hv',item.get('bus_from'))),{})
    kind=tr('edit.kind.'+item['class'])
    if kind.startswith('edit.kind.'):kind=item['class']
    parts=[kind+' '+str(item.get('name') or item['id'])]
    if bus:parts.append(str(bus.get('name') or bus['id']))
    kv=bus.get('un_kv',item.get('un_kv'))
    if kv is not None:parts.append(f'{kv:g} kV')
    return ' · '.join(parts)
