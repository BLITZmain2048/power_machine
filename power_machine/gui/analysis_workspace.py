"""Analysis composition and compatibility routing for existing result pages."""
import tkinter as tk
from tkinter import ttk
from .chrome import BrandBar, CompactToolbar, PanelHeader, SegmentedStatus
from .i18n import tr, language
from .time_cursor import TimeCursor, number
from .events_editor import EventCards
from .theme import theme


STUDIES=(('flow','shell.load_flow',3),('shortcircuit','shell.short_circuit',10),
    ('contingency','v3.analysis.study.contingency',11),('rms','shell.rms_simulation',5),
    ('modal','integration.small_signal_analysis',8),('calc_cct','tools.cct',8),
    ('calc_pvqv','tools.pvqv',8),('calc_sens','tools.sensitivity',8),
    ('calc_qds','tools.qds',8),('calc_met','shell.dynamic_metrics',8),
    ('protection','shell.protection',8),('calc_lfctl','shell.lf_controls_and_limits',3))
STUDY_GROUPS=(('stationary',('flow','shortcircuit','contingency','calc_pvqv','calc_sens')),
              ('dynamic',('rms','modal','calc_cct','calc_qds')),
              ('tools',('calc_met','protection','calc_lfctl')))
MODULE_STUDIES={'loadflow':'flow','rms':'rms','simulation':'rms','contingency':'contingency','shortcircuit':'shortcircuit','sc_sweep':'shortcircuit',
                'small_signal':'modal','modal':'modal','cct':'calc_cct','pvqv':'calc_pvqv',
                'voltage_stability':'calc_pvqv','sensitivity':'calc_sens','sensitivities':'calc_sens',
                'qds':'calc_qds','qds_panel':'calc_qds','metrics':'calc_met','dyn_metrics':'calc_met','lf_controls':'calc_lfctl'}
STATUS_SYMBOLS={'never':'○','current':'●','stale':'◷','error':'!','cancelled':'■'}


def study_state_label(state):
    return tr('v3.fix.cancelled') if state=='cancelled' else tr('v3.analysis.state.'+state)


class AnalysisWorkspace(ttk.Frame):
    """Notebook-like API keeps calculation controllers independent of the layout.

    New pages can declare ``workspace_area`` as curves or tables. StudyPanel
    exposes its options in options_host and its results in the central notebook.
    """
    def __init__(self,parent,app):
        super().__init__(parent)
        from .app_shell import WorkspaceNotebook
        self.app=app
        app.analysis=self
        self.active_study='flow'
        self._current=''
        self.owners={}
        self.study_states={key:'never' for key,_,_ in STUDIES}
        self._state_case=(id(app.document), app.document.active_case_id) if getattr(app,'document',None) else None
        self._events_signature=None
        self._refresh_id=None
        self.panes=ttk.Panedwindow(self,orient='horizontal')
        self.panes.pack(fill='both',expand=True)
        self.sidebar=ttk.Frame(self.panes,width=355,style='Panel.TFrame')
        self.panes.add(self.sidebar,weight=0)
        PanelHeader(self.sidebar,'v3.studies').pack(fill='x')
        study_list=ttk.Frame(self.sidebar)
        study_list.pack(fill='x',padx=4,pady=(0,5))
        self.study_list=study_list
        self.bind('<Configure>',self._resize_studies,add='+')
        ttk.Style(self).configure('Analysis.Explorer.Treeview',rowheight=23)
        self.study_tree=ttk.Treeview(study_list,columns=('state',),show='tree',height=18,selectmode='browse',style='Analysis.Explorer.Treeview')
        self.study_tree.column('#0',width=280,minwidth=180)
        self.study_tree.column('state',width=27,minwidth=27,stretch=False,anchor='center')
        scrollbar=ttk.Scrollbar(study_list,command=self.study_tree.yview)
        self.study_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side='right',fill='y')
        self.study_tree.pack(side='left',fill='x',expand=True)
        self.study_tree.bind('<<TreeviewSelect>>',self._choose_study)
        self.study_images=[]
        from .toolbar_icons import icon
        from .app_shell import Tooltip
        Tooltip(self.study_tree,lambda:tr('v3.study_hint'))
        for group, keys in STUDY_GROUPS:
            parent=self.study_tree.insert('','end',iid='group:'+group,text=tr('v3.analysis.group.'+group),open=True,tags=('section',))
            for key in keys:
                _,label,icon_number=next(row for row in STUDIES if row[0]==key)
                image=icon(self,icon_number)
                self.study_images.append(image)
                self.study_tree.insert(parent,'end',iid=key,text='   '+tr(label),image=image,values=('○',))
        self.state_legend=ttk.Label(self.sidebar,style='Muted.TLabel',font=('Segoe UI',8),wraplength=335)
        self.state_legend.pack(fill='x',padx=8,pady=(0,5))
        PanelHeader(self.sidebar,'v3.configuration').pack(fill='x')
        self.options_host=ttk.Frame(self.sidebar)
        self.options_host.pack(fill='both',expand=True,padx=5,pady=5)
        from .panels.panel_widgets import ScrollForm
        self.basic_options=ScrollForm(self.options_host)
        self.basic_title=ttk.Label(self.basic_options.content,style='Title.TLabel',wraplength=300)
        self.basic_title.pack(anchor='w',pady=(0,4))
        self.basic_detail=ttk.Label(self.basic_options.content,wraplength=300,justify='left')
        self.basic_detail.pack(anchor='w')
        self.options_button=ttk.Button(self.basic_options.content,text=tr('shell.calculation_options'),command=self.configure_active,style='Toolbar.TButton')
        self.options_button.pack(anchor='w')
        self.options_button.pack_configure(before=self.basic_detail,pady=(0,4))
        self.basic_options.wire()
        self.history_button=ttk.Button(self.sidebar,text=tr('study.run_history'),command=app.run_history_dialog,style='Toolbar.TButton')
        self.history_button.pack(side='bottom',anchor='w',padx=8,pady=4,before=self.options_host)
        self.center=ttk.Frame(self.panes)
        self.panes.add(self.center,weight=1)
        self.results=ttk.Notebook(self.center,style='Results.TNotebook')
        self.results.pack(fill='both',expand=True)
        self.result_hosts={}
        self.books={}
        for area in ('curves','events','tables','log'):
            host=ttk.Frame(self.results)
            self.result_hosts[area]=host
            self.results.add(host,text=tr('v3.tab.'+area))
            if area in ('curves','tables'):
                book=WorkspaceNotebook(host,on_close=app.close_page,can_close=lambda tab:True)
                book.pack(fill='both',expand=True)
                book.bind('<<NotebookTabChanged>>',lambda event,a=area:self._changed(a),add='+')
                self.books[area]=book
        self.start_host=ttk.Frame(self.results)
        self.start_book=ttk.Notebook(self.start_host,style='Hidden.TNotebook')
        self.start_book.pack(fill='both',expand=True)
        self.results.bind('<<NotebookTabChanged>>',self._area_changed,add='+')
        self.time_cursor=TimeCursor(self.result_hosts['curves'],app,on_ready=self._temporal_ready)
        self.time_cursor.pack(fill='x',before=self.books['curves'].bar)
        self.maximize_curves_button=ttk.Button(self.time_cursor.controls,command=self.toggle_curve_area,style='Toolbar.TButton',text=tr('v3.fix.maximize_curves'))
        self.maximize_curves_button.pack(side='right',padx=3)
        self.curve_events=EventCards(self.result_hosts['curves'],self.seek_event,self.edit_event,compact=True)
        self.curve_events.on_seek=lambda index:self.seek_event(index,self.curve_events.rows)
        self.curve_events.pack(fill='x',before=self.books['curves'].bar)
        self.event_cards=EventCards(self.result_hosts['events'],self.seek_event,self.edit_event)
        self.event_cards.pack(fill='x')
        self.event_editor_host=ttk.Frame(self.result_hosts['events'])
        self.event_editor_host.pack(fill='both',expand=True)
        self.empty_tables=ttk.Frame(self.result_hosts['tables'],padding=20)
        self.empty_message=ttk.Label(self.empty_tables,text=tr('v3.empty_tables'),wraplength=480,justify='center')
        self.empty_message.pack(pady=10)
        self.empty_run=ttk.Button(self.empty_tables,text=tr('shell.run_load_flow_f10'),command=app.run_loadflow,style='Run.TButton')
        self.empty_run.pack()
        self.empty_tables.place(relx=.5,rely=.45,anchor='center')
        self.bar=self.books['curves'].bar
        self.list_menu=self.books['curves'].list_menu
        self.event_editor=None
        self._tokens=[app.bus.subscribe('language.changed',self.retranslate),
                      app.bus.subscribe('theme.changed',self._theme_changed),
                      app.bus.subscribe('results.updated',self._results_updated),
                      app.bus.subscribe('results.stale',self._results_stale)]
        self.bind('<Destroy>',self._dispose,add='+')
        self.select_study('flow',open_panel=False)
        self._render_states()
        self._refresh_id=self.after(400,self._refresh_summary)

    def toggle_curve_area(self):
        expanded=str(self.sidebar) not in self.panes.panes()
        if expanded:
            self.panes.insert(0,self.sidebar,weight=0)
            self.curve_events.pack(fill='x',before=self.books['curves'].bar)
        else:
            self.panes.forget(self.sidebar)
            self.curve_events.pack_forget()
        self.maximize_curves_button.configure(text=tr('v3.fix.maximize_curves' if expanded else 'v3.fix.restore_curves'))

    def _resize_studies(self,event=None):
        if event is not None and event.widget is not self:return
        height=18 if self.winfo_toplevel().winfo_height()>=900 else 9
        if int(self.study_tree.cget('height'))!=height:self.study_tree.configure(height=height)

    def add(self,child,**kwargs):
        area=kwargs.pop('area',getattr(child,'workspace_area',None))
        if area is None:
            name=type(child).__name__
            area='curves' if 'Plot' in name or isinstance(child,ttk.Notebook) else 'tables'
        if area=='start':
            if str(self.start_host) not in self.results.tabs():self.results.insert(0,self.start_host,text=tr('v3.start.title'))
            owner=self.start_book
        else:owner=self.books[area]
        self.owners[str(child)]=owner
        if area=='tables':self.empty_tables.place_forget()
        owner.add(child,**kwargs)
        if area=='curves' and hasattr(child,'set_time_cursor'):
            self.time_cursor.attach_page(child)
            child.set_events(self._document_events())
        child.bind('<Destroy>',lambda event:self._removed(child) if event.widget is child else None,add='+')

    def _removed(self,child):
        self.owners.pop(str(child),None)
        if self._current==str(child):self._current=''
        if self.start_book.winfo_exists() and not self.start_book.tabs():
            if str(self.start_host) in self.results.tabs():self.results.forget(self.start_host)
        if self.books['tables'].winfo_exists() and self.empty_tables.winfo_exists() and not self.books['tables'].tabs():
            self.empty_tables.place(relx=.5,rely=.45,anchor='center')

    def tabs(self):return tuple(page for book in [*self.books.values(),self.start_book] for page in book.tabs())

    def tab(self,tab_id,option=None,**kwargs):
        return self.owners[str(tab_id)].tab(tab_id,option,**kwargs)

    def select(self,tab_id=None):
        if tab_id is None:return self._current if self._current in self.tabs() else ''
        key=str(tab_id)
        owner=self.owners[key]
        owner.select(tab_id)
        area=next((a for a,b in self.books.items() if b is owner),'start')
        self.results.select(self.start_host if area=='start' else self.result_hosts[area])
        self._current=key
        self._show_panel_options(key)
        self.event_generate('<<NotebookTabChanged>>')
        return ''

    def forget(self,tab_id):
        owner=self.owners[str(tab_id)]
        owner.forget(tab_id)
        self.owners.pop(str(tab_id),None)
        if owner is self.start_book and not owner.tabs():self.results.forget(self.start_host)

    def sync(self):
        for book in self.books.values():book.sync()

    def _changed(self,area):
        if self.results.select()!=str(self.result_hosts[area]):return
        selected=self.books[area].select()
        if selected:
            self._current=selected
            self._show_panel_options(selected)
            if area=='curves' and hasattr(self,'time_cursor'):
                page=self.nametowidget(selected)
                if page is getattr(self.app,'welcome_page',None):
                    self.time_cursor.pack_forget()
                    self.curve_events.pack_forget()
                elif not self.time_cursor.winfo_manager():
                    self.time_cursor.pack(fill='x',before=self.books['curves'].bar)
                    self.curve_events.pack(fill='x',before=self.books['curves'].bar)
                if hasattr(page,'set_time_cursor'):self.time_cursor.use_page(page)
        self.event_generate('<<NotebookTabChanged>>')

    def _area_changed(self,event=None):
        if self.results.select()==str(self.start_host):
            self._current=self.start_book.select()
            self.event_generate('<<NotebookTabChanged>>')
            return
        area=next((a for a,h in self.result_hosts.items() if str(h)==self.results.select()),None)
        if area in self.books:self._changed(area)
        else:
            self._current=''
            if area=='events' and self.app.document:self.show_events()

    def _choose_study(self,event=None):
        selected=self.study_tree.selection()
        if selected and selected[0] in self.study_states:self.select_study(selected[0])

    def select_study(self,key,*,open_panel=True):
        self.active_study=key
        if self.study_tree.exists(key) and self.study_tree.selection()!=(key,):
            self.study_tree.selection_set(key)
            self.study_tree.see(key)
        if key in ('flow','rms'):
            self._show_options(self.basic_options)
            self.basic_title.configure(text=tr('shell.load_flow' if key=='flow' else 'shell.rms_simulation'))
            self.basic_detail.configure(text=tr('v3.flow_options' if key=='flow' else 'v3.rms_options'))
        elif open_panel:self.app.open_parity_panel(key)
        self.refresh_document()

    def _show_options(self,widget):
        for child in self.options_host.winfo_children():child.pack_forget()
        widget.pack(fill='both',expand=True)

    def _show_panel_options(self,key):
        for panel_id,(frame,context) in self.app.parity_panels.items():
            if str(frame)==key:
                self.active_study=panel_id
                panel=getattr(context,'panel',None)
                if getattr(panel,'_analysis_external',False):self._show_options(panel.form_host)
                self.refresh_document()
                break

    def configure_active(self):
        return self.app.simulation_dialog() if self.active_study=='rms' else self.app.options_dialog()

    def run_active(self):
        if self.active_study=='flow':return self.app.run_loadflow()
        if self.active_study=='rms':return self.app.start_simulation()
        item=self.app.parity_panels.get(self.active_study)
        if item:
            button=getattr(item[1].panel,'action_buttons',{}).get('run')
            if button is not None:return button.invoke()

    def refresh_document(self):
        app=self.app
        if not app.document:return
        data=app.document.to_data()
        self.case_ids=[row['case_id'] for row in data['cases']]
        def case_name(row):
            name=tr(row['default_name_key']) if row.get('default_name_key') else row['name']
            if app.model.get('meta',{}).get('benchmark')=='ieee39' and name in ('IEEE 39 · caso base','IEEE 39 · base case'):
                return tr('shell.ieee39_base_case')
            return name
        app.case_selector.configure(values=[case_name(row) for row in data['cases']])
        if data['active_case_id'] in self.case_ids:app.case_selector.current(self.case_ids.index(data['active_case_id']))
        self.time_cursor.check_case()
        case=(id(app.document),app.document.active_case_id)
        if self._state_case!=case:
            self._state_case=case
            self.study_states={key:'never' for key,_,_ in STUDIES}
            for link in reversed(app.document.runs_for_case()):
                key=link.get('panel_id') if link.get('panel_id') in self.study_states else MODULE_STUDIES.get(link.get('module'),link.get('module'))
                if key in self.study_states:
                    self.study_states[key]='cancelled' if link.get('cancelled') or link.get('status')=='cancelled' else 'error' if link.get('status') in ('error','failed') else 'current' if link.get('freshness')=='current' else 'stale'
            self._render_states()
        self.refresh_configuration()
        self._refresh_events()
        if self.event_editor is not None and self.event_editor.winfo_exists() and self.event_editor.case_id!=app.document.active_case_id:
            self.event_editor.destroy()
            self.event_editor=None
            if self.results.select()==str(self.result_hosts['events']):self.show_events()

    def activate_case(self,event=None):
        n=self.app.case_selector.current()
        if 0<=n<len(self.case_ids):self.app.activate_study(self.case_ids[n])

    def show_events(self,preset=None,event_index=None):
        if self.event_editor is not None and self.event_editor.winfo_exists() and (preset or self.event_editor.document is not self.app.document or self.event_editor.case_id!=self.app.document.active_case_id):
            self.event_editor.destroy()
        if self.event_editor is None or not self.event_editor.winfo_exists():
            from .events_editor import EmbeddedEventsEditor
            app=self.app
            powered={row['id']:row['energized'] for row in (app.result or {}).get('buses',[])}
            self.event_editor=EmbeddedEventsEditor(self.event_editor_host,app.document,app.api,app.root_path,
                app.model,app.api.element_index(app.operational),app.update_document,energized=powered,preset=preset,event_bus=app.bus)
            self.event_editor.pack(fill='both',expand=True)
            self.event_editor.on_rows_changed=lambda rows:self.event_cards.set_events(rows,self.app.index)
            self.event_cards.set_events(self.event_editor.rows,self.app.index)
        else:
            app=self.app
            powered={row['id']:row['energized'] for row in (app.result or {}).get('buses',[])}
            self.event_editor.refresh_context(app.model,app.api.element_index(app.operational),powered)
        self.results.select(self.result_hosts['events'])
        if event_index is not None:
            self.event_editor.tree.selection_set(str(event_index))
            self.event_editor.load_selection()
        return self.event_editor

    def _document_events(self):
        return [event.to_dict() for event in self.app.document.study.events] if self.app.document else []

    def _refresh_events(self,force=False):
        rows=self._document_events()
        signature=repr(rows)
        if force or signature!=self._events_signature:
            self._events_signature=signature
            self.curve_events.set_events(rows,getattr(self.app,'index',{}))
            if self.event_editor is None or not self.event_editor.winfo_exists():
                self.event_cards.set_events(rows,getattr(self.app,'index',{}))

    def seek_event(self,index,rows=None):
        rows=self.event_cards.rows if rows is None else rows
        if 0<=index<len(rows):
            self.time_cursor.pause()
            self.time_cursor.seek(rows[index]['time_s'])
            self.results.select(self.result_hosts['curves'])

    def edit_event(self,index):
        self.show_events(event_index=index)

    def _temporal_ready(self):
        rows=self._document_events()
        for page in list(self.time_cursor.pages):
            if getattr(page,'_analysis_events',None)!=rows:page.set_events(rows)

    def _results_updated(self,study=None,result_id=None,**_):
        key=MODULE_STUDIES.get(study,study)
        doc=self.app.document
        link=next((row for row in doc.to_data()['result_links'] if row['run_id']==result_id),{}) if doc else {}
        if link:
            if link.get('case_id')!=doc.active_case_id:return
            key=link.get('panel_id') if link.get('panel_id') in self.study_states else MODULE_STUDIES.get(link.get('module'),link.get('module'))
        if key in self.study_states:
            result=getattr(self.app,'parity_results',{}).get(link.get('module',study),{})
            if key=='flow':result=self.app.result or {}
            status=link.get('status',result.get('status'))
            failed=bool(link.get('cancelled',result.get('cancelled',False))) or status in ('error','failed','cancelled','invalid','not_evaluable','blocked') or result.get('converged') is False
            self.study_states[key]='cancelled' if bool(link.get('cancelled',result.get('cancelled',False))) or status=='cancelled' else 'error' if failed else 'current'
            self._render_states()
        if key=='rms':self.time_cursor._read_source()

    def _results_stale(self,**_):
        self.study_states={key:'stale' if state in ('current','error') else state for key,state in self.study_states.items()}
        self._render_states()

    def _render_states(self):
        self.study_tree.tag_configure('section',foreground=theme.color('muted'),font=(*theme.font('small'),'bold'))
        for state,token in [('never','text'),('current','ok'),('stale','text_muted'),('error','error'),('cancelled','warn')]:
            self.study_tree.tag_configure('status:'+state,foreground=theme.color(token))
        for key,state in self.study_states.items():
            self.study_tree.item(key,values=(STATUS_SYMBOLS[state],),tags=('status:'+state,))
        self.state_legend.configure(text='   '.join(STATUS_SYMBOLS[state]+' '+study_state_label(state) for state in STATUS_SYMBOLS))
        welcome=getattr(self.app,'welcome_page',None)
        if welcome and welcome.winfo_exists():welcome.refresh()

    def refresh_configuration(self):
        app=self.app
        if not app.document:return
        title=next((tr(label) for key,label,_ in STUDIES if key==self.active_study),self.active_study)
        study=app.document.study
        if self.active_study=='rms':
            options=study.options
            key='v3.analysis.rms_summary_short' if 1<app.winfo_width()<1450 else 'v3.analysis.rms_summary'
            title=tr(key,duration=number(options.t_end_s-options.t_start_s,2),step=number(options.dt_s*1000,3).rstrip('0').rstrip('.,'))
            detail=tr('v3.analysis.rms_configuration',start=number(options.t_start_s),end=number(options.t_end_s),step=number(options.dt_s*1000),events=len(study.events))
            if self.basic_detail.cget('text')!=detail:self.basic_detail.configure(text=detail)
        elif self.active_study=='flow':
            enabled=tr('v3.analysis.on' if study.ldf_options.get('q_limits',False) else 'v3.analysis.off')
            title+=' · '+tr('v3.analysis.q_limits',value=enabled)
        else:
            item=app.parity_panels.get(self.active_study)
            panel=getattr(item[1],'panel',None) if item else None
            summary=[]
            def read_fields(parent):
                for widget in parent.winfo_children():
                    if isinstance(widget,(ttk.Entry,ttk.Combobox)) and widget.winfo_manager()=='grid':
                        info=widget.grid_info()
                        labels=[w for w in parent.grid_slaves(row=info['row']) if isinstance(w,ttk.Label)]
                        value=widget.get()
                        if labels and value and len(value)<35:
                            summary.append(str(labels[0].cget('text')).rstrip(':')+' '+value)
                    read_fields(widget)
            if panel is not None:read_fields(getattr(panel,'form_host',panel))
            if summary:title+=' · '+' · '.join(summary[:2])
        if app.configuration_text.get()!=title:app.configuration_text.set(title)

    def _refresh_summary(self):
        self._refresh_id=None
        self.refresh_configuration()
        self._refresh_events()
        self._refresh_id=self.after(400,self._refresh_summary)

    def retranslate(self,**_):
        expanded=str(self.sidebar) not in self.panes.panes()
        self.maximize_curves_button.configure(text=tr('v3.fix.restore_curves' if expanded else 'v3.fix.maximize_curves'))
        for key,label,_ in STUDIES:self.study_tree.item(key,text='   '+tr(label))
        for group,_ in STUDY_GROUPS:self.study_tree.item('group:'+group,text=tr('v3.analysis.group.'+group))
        self._render_states()
        self._refresh_events(force=True)
        self.event_cards.retranslate()
        for area,host in self.result_hosts.items():self.results.tab(host,text=tr('v3.tab.'+area))
        if str(self.start_host) in self.results.tabs():self.results.tab(self.start_host,text=tr('v3.start.title'))
        self.options_button.configure(text=tr('shell.calculation_options'))
        self.empty_message.configure(text=tr('v3.empty_tables'))
        self.empty_run.configure(text=tr('shell.run_load_flow_f10'))
        self.history_button.configure(text=tr('study.run_history'))
        if self.active_study in ('flow','rms'):self.select_study(self.active_study,open_panel=False)
        self.refresh_document()

    def _theme_changed(self,**_):
        from .toolbar_icons import icon
        images=[]
        for key,_,number in STUDIES:
            picture=icon(self,number)
            images.append(picture)
            self.study_tree.item(key,image=picture)
        self.study_images=images
        self._render_states()
        self._refresh_events(force=True)
        self.event_cards.retranslate()

    def _dispose(self,event):
        if event.widget is self:
            if self._refresh_id:self.after_cancel(self._refresh_id)
            for token in self._tokens:self.app.bus.unsubscribe(token)
