"""Presentación por documento y preferencias locales de disposición."""
from copy import deepcopy
from dataclasses import asdict
import json
import os
from pathlib import Path
from .i18n import tr
from .theme import theme
import tkinter as tk
from tkinter import ttk


def read_json(path,default):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return deepcopy(default)


class WorkspaceActions:
    def open_manual(self):
        from tkinter import messagebox
        messagebox.showinfo('power_machine', tr('shell.open_a_network_from_file_use_calculation'), parent=self)

    def open_script_console(self):
        from .scripting import open_scripting_window
        return open_scripting_window(self, self.operational)

    def connect_presentation_menus(self):
        self.menus['tools'].add_command(label=tr('shell.python_scripts'), command=self.open_script_console)
        from .presentation_specs import plot_presets,box_presets
        plots=tk.Menu(self.menus['plots'],tearoff=False)
        self.menus['plots'].add_cascade(label=tr('shell.plot_templates'),menu=plots)
        for key,spec in plot_presets().items():
            plots.add_command(label=spec['name'],command=lambda spec=spec:self.apply_plot_template(spec))
        boxes=tk.Menu(self.menus['view'],tearoff=False)
        self.menus['view'].add_cascade(label=tr('shell.result_box_formats'),menu=boxes)
        for key,spec in box_presets().items():
            boxes.add_command(label=key.replace('_',' '),command=lambda spec=spec:self.apply_box_preset(spec))
        self.menus['view'].add_command(label=tr('shell.undo_presentation'),command=lambda:self.document.history.step('presentation'))
        self.menus['view'].add_command(label=tr('shell.redo_presentation'),command=lambda:self.document.history.step('presentation',redo=True))

    def apply_plot_template(self,spec):
        from .presentation_specs import resolve_template
        spec=deepcopy(spec)
        spec['events']=[dict(time_s=event.time_s,label=event.kind) for event in self.document.study.events]
        acquired=set(self.buffer.keys)
        resolved=resolve_template(spec,[row for row in self.catalog if row['key'] in acquired],self.index)
        self.document.edit(lambda study:study.plots.append(resolved))
        self.rebuild_plots()
        self.book.select(self.plot_pages[-1])
        self.update_document(invalidate=False)
        self.capture_presentation()
        if resolved['missing_signals']:self.output.log('AVISO',tr('shell.template_with_missing_signals')+str(resolved['missing_signals']))

    def refresh_plot_templates(self):
        acquired=set(self.buffer.keys)
        if not acquired:return
        catalog=[row for row in self.catalog if row['key'] in acquired]
        for page in self.plot_pages:
            if 'template' in page.spec:page.apply_presentation(page.spec,catalog=catalog,index=self.index)

    def apply_box_preset(self,boxes):
        page=self.diagram_page()
        spec=page.get_presentation()
        spec['diagram'].update(boxes=deepcopy(boxes),legacy_boxes=False)
        page.apply_presentation(spec)
        self.capture_presentation()

    def refresh_calculation_capabilities(self):
        if not self.document:return
        from power_machine.study_capabilities import capabilities
        from .panel_context import fingerprint
        snapshot=self.document.snapshot()
        key=fingerprint({'model':self.model,'study':snapshot})
        if getattr(self,'_capability_signature',None)==key:return
        self._capability_signature=key
        result=capabilities(self.model,snapshot)
        self.calculation_capabilities=result
        legacy_restrictions={
            'calc_lfctl':[tr('shell.discrete_control_outside_the_load_flow_no'),
                          tr('shell.explicit_equipment_busbar_setpoint_and_band_ratings'),
                          tr('shell.generates_a_proposal_for_review_without_automatically')],
            'contingency':[tr('shell.explicit_batch_and_skipped_cases_non_convergence'),
                      tr('shell.island_classification_must_be_assessed_together_with')],
            'modal':[tr('shell.continuous_profile_limited_to_100_mva_and'),
                       tr('shell.hybrid_controls_and_models_outside_the_profile')],
            'protection':[tr('shell.analytical_curves_do_not_validate_an_installed'),
                       tr('shell.explicit_terminal_fault_current_ct_phase_circuit')]}
        for ident,item in getattr(self,'calculation_items',{}).items():
            menu,index,title,module=item
            row=result['calculations'].get(module,{})
            blockers=row.get('blockers',[])
            reason=' / '.join(b['message'] for b in blockers)
            blocked=bool(blockers)
            menu.entryconfigure(0,state='disabled' if blocked else 'normal')
            self.menus['calculation'].entryconfigure(index,label=title+(tr('shell.blocked_ec1d5e')+reason[:65] if blocked else tr('shell.with_restrictions')))
            self.calculation_details[ident]=dict(row,qualification_revision=result['qualification_revision'],
                restrictions=row.get('restrictions',[]) or legacy_restrictions.get(ident,[tr('shell.scope_of_the_profile_and_panel_input')]))

    def show_calculation_details(self,ident):
        from tkinter import messagebox
        value=self.calculation_details.get(ident,{})
        lines=[value.get('qualification_revision',tr('shell.no_specific_qualification')),*value.get('restrictions',[]),
               *[tr('shell.requires')+s for s in value.get('missing_data',[])],
               *[tr('shell.blocked')+s['message'] for s in value.get('blockers',[])]]
        messagebox.showinfo(tr('shell.scope_and_restrictions'), '\n\n'.join(lines),parent=self)

    def workspace_preferences_path(self):
        return self.settings.path

    def restore_window(self):
        self.window_preferences = self.settings.get('panel_options', {}).get('workspace_v3', {})
        try:
            self.state('normal')
            geometry = self.settings.get('main_geometry', '1400x850')
            if geometry == '1400x850':
                geometry = f'{max(1000,min(1400,self.winfo_screenwidth()))}x{max(600,min(850,self.winfo_screenheight()-80))}'
            self.geometry(geometry)
        except tk.TclError:
            self.geometry('1400x850')

    def restore_dividers(self):
        data=getattr(self,'window_preferences',{})
        if not data:return
        self.horizontal.sashpos(0,min(max(205,int(data.get('sidebar',260))),self.winfo_width()-400))
        self.set_output_visible(bool(data.get('output_visible',True)))


    def save_window_preferences(self):
        data=dict(sidebar=self.horizontal.sashpos(0),output_visible=self.output_visible.get(),
                  output_sash=0)
        options = deepcopy(self.settings.get('panel_options', {}))
        options['workspace_v3'] = data
        self.settings.set('panel_options', options)
        self.settings.set('main_geometry', self.geometry())
        self.settings.save()

    def reset_workspace_layout(self):
        self.window_preferences={}
        self.state('normal')
        width,height=min(1400,self.winfo_screenwidth()),min(850,self.winfo_screenheight()-70)
        self.geometry(f'{max(1000,width)}x{max(600,height)}+0+0')
        self.set_output_visible(True)
        self.update_idletasks()
        self.initial_panes()
        self.save_window_preferences()

    def presentation_data(self):
        return self.document.data.setdefault('presentation',{}).setdefault('gui_workspace_v1',{}).setdefault(self.document.active_case_id,{})

    def capture_presentation(self):
        if not self.document:return
        data=self.presentation_data()
        before=deepcopy(data)
        diagrams={}
        for group,page in self.diagrams.items():
            diagrams[group or '__general__']=dict(color_mode=page.color_mode,voltage_limits=list(page.voltage_limits),
                result_digits=deepcopy(page.result_digits),detail_zoom=page.detail_zoom,boxes=self.show_boxes.get())
            if hasattr(page,'get_presentation'):diagrams[group or '__general__']['specification']=page.get_presentation()
        data.setdefault('diagrams',{}).update(diagrams)
        data['plots']=[page.get_presentation() for page in self.plot_pages]
        if data['plots']!=self.document.snapshot().get('plots',[]):
            self.document.edit(lambda study:setattr(study,'plots',deepcopy(data['plots'])))
        if self.table_pages:
            data['tables']={page.kind:dict(columns={key:value.get() for key,value in page.column_visible.items()},
                numeric_ranges=deepcopy(page.numeric_ranges),limits=deepcopy(page.limits),filter=page.text_filter.get()) for page in self.table_pages}
        data_tables=data.setdefault('data_tables',{})
        managers=[self.data_manager]+[manager for window,manager in getattr(self,'data_windows',[]) if window.winfo_exists()]
        for manager in managers:
            key=manager.current_group+'|'+str(manager.current_class or 'all')
            data_tables[key]=dict(columns={k:v.get() for k,v in manager.column_visible.items()},
                query=manager.query.get(),station=manager.station_filter.get(),voltage=manager.voltage_filter.get(),
                state=manager.state_filter.get(),sort_key=manager.sort_key,sort_reverse=deepcopy(manager.sort_reverse))
        saved=data.setdefault('workspaces',{})
        for ident,(_,context) in self.parity_panels.items():
            controller=getattr(context.panel,'controller',None)
            if controller is None:continue
            if ident=='protection':
                saved['protection']=dict(id='workspace-protection',provenance={'origin':'power_machine-workspace'},content=deepcopy(controller.workspace))
                saved['protection_controls']=self.protection_controls(context.panel)
            if ident=='dsl_editor':saved['dsl']=dict(id=controller.draft.object_id,provenance={'origin':'power_machine-dsl-workspace'},content=asdict(controller.draft))
            if ident=='dsl_editor':saved['dsl_texts']=self.dsl_texts(context.panel)
        from .presentation_specs import default_presentation,validate_presentation,plot_presets
        specs=deepcopy(data.get('specification') or default_presentation())
        general=data['diagrams'].get('__general__',next(iter(diagrams.values()),{}))
        if general.get('specification'):specs=deepcopy(general['specification'])
        if general:
            specs['diagram'].update(color_mode=general['color_mode'],voltage_limits=general['voltage_limits'],
                digits=general['result_digits'],show_results=general['boxes'])
        specs['table_pages']=[dict(version=1,name=kind+' LF',**{'class':kind},
            columns=[key for key,value in table['columns'].items() if value],filters=[]) for kind,table in data.get('tables',{}).items()]
        specs['plot_pages']=deepcopy(data['plots'])
        if not specs['plot_templates']:specs['plot_templates']=list(plot_presets().values())
        data['specification']=validate_presentation(specs)
        # La frontera del documento conserva tipos JSON, también para límites y puertos.
        after=json.loads(json.dumps(data,ensure_ascii=False,allow_nan=False))
        self.document.data['presentation']['gui_workspace_v1'][self.document.active_case_id]=after
        before_visual={key:deepcopy(before.get(key,{})) for key in ('diagrams','tables','data_tables')}
        after_visual={key:deepcopy(after.get(key,{})) for key in ('diagrams','tables','data_tables')}
        if before and before_visual!=after_visual:
            from .history import SnapshotCommand
            document,case_id=self.document,self.document.active_case_id
            def restore(value):
                document.data['presentation']['gui_workspace_v1'][case_id].update(value)
                if self.document is document and document.active_case_id==case_id:
                    for group,page in self.diagrams.items():self.restore_diagram_presentation(page,group)
                    for page in self.table_pages:self.restore_table_presentation(page)
                    self.restore_data_table_presentation(self.data_manager)
            document.history.record('presentation',SnapshotCommand(restore,before_visual,after_visual))

    def restore_diagram_presentation(self,page,group):
        data=self.presentation_data().get('diagrams',{})
        saved=data.get(group or '__general__',data.get('__general__',{}))
        if not saved:return
        if 'specification' in saved:page.apply_presentation(saved['specification'])
        page.set_color_mode(saved.get('color_mode','voltage'),voltage_limits=saved.get('voltage_limits',[.95,1.05]))
        page.set_result_format(saved.get('result_digits',{}))
        page.set_detail_zoom(saved.get('detail_zoom',.65))
        self.show_boxes.set(saved.get('boxes',True))
        page.set_result_boxes_visible(self.show_boxes.get())

    def restore_table_presentation(self,page):
        saved=self.presentation_data().get('tables',{}).get(page.kind,{})
        for key,value in saved.get('columns',{}).items():
            if key in page.column_visible:page.column_visible[key].set(value)
        if saved:
            page.limits=deepcopy(saved.get('limits',page.limits))
            page.numeric_ranges=deepcopy(saved.get('numeric_ranges',{}))
            page.text_filter.set(saved.get('filter',''))
            page.populate()

    def restore_data_table_presentation(self,manager):
        key=manager.current_group+'|'+str(manager.current_class or 'all')
        saved=self.presentation_data().get('data_tables',{}).get(key)
        if not saved:return
        for key,value in saved['columns'].items():
            if key in manager.column_visible:manager.column_visible[key].set(value)
        manager.query.set(saved['query'])
        manager.station_filter.set(saved['station'])
        manager.voltage_filter.set(saved['voltage'])
        manager.state_filter.set(saved['state'])
        manager.sort_key=saved['sort_key']
        manager.sort_reverse=deepcopy(saved['sort_reverse'])
        manager.populate_table()

    def restore_panel_workspace(self,ident,panel):
        saved=self.presentation_data().get('workspaces',{})
        controller=getattr(panel,'controller',getattr(panel,'protection_controller',None))
        if controller is None:
            self.output.log('AVISO',tr('shell.panel')+ident+tr('shell.does_not_expose_a_controller_to_restore'))
            return
        panel.controller=controller
        if ident=='protection' and 'protection' in saved:
            controller.workspace=deepcopy(saved['protection'].get('content',saved['protection']))
            controller.invalidate()
            from power_machine.protection.settings import effective_stages
            from .panels.panel_widgets import ElementSelector
            rows=[dict(id=s.object_id,object_id=s.object_id,name=s.object_id) for s in effective_stages(controller.workspace)]
            for widget in self.widget_descendants(panel):
                if isinstance(widget,ElementSelector):widget.set_items(rows,self.model)
            self.protection_controls(panel,saved.get('protection_controls',{}))
        if ident=='dsl_editor' and 'dsl' in saved:
            from power_machine.dsl_editor.schema import draft_from_dict
            controller.set_draft(draft_from_dict(deepcopy(saved['dsl'].get('content',saved['dsl']))))
            self.dsl_texts(panel,saved.get('dsl_texts'))
        # El consumidor restaura los selectores si publica esta extensión.
        restore=getattr(panel,'refresh_workspace',None)
        if callable(restore):restore()

    @staticmethod
    def widget_descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from WorkspaceActions.widget_descendants(child)
        if getattr(widget,'_analysis_external',False):
            yield widget.form_host
            yield from WorkspaceActions.widget_descendants(widget.form_host)

    def protection_controls(self,panel,restore=None):
        from .panels.panel_widgets import ElementSelector
        fields={}
        for widget in self.widget_descendants(panel):
            if not isinstance(widget,(ttk.Entry,ttk.Combobox)):continue
            form_host=getattr(panel,'form_host',None)
            external=getattr(panel,'_analysis_external',False) and str(widget).startswith(str(form_host)+'.')
            suffix=str(widget).removeprefix(str(form_host)) if external else ''
            key='options'+suffix if external else str(widget).removeprefix(str(panel))
            if restore is not None and external and key not in restore:
                legacy='.!frame.!frame'+suffix
                if legacy in restore:restore={**restore,key:restore[legacy]}
            if restore is not None and key in restore:
                value=restore[key]
                if isinstance(widget,ElementSelector):
                    if not value or value in widget.choices.values():widget.set_id(value)
                elif isinstance(widget,ttk.Combobox):widget.set(value)
                else:
                    widget.delete(0,'end')
                    widget.insert(0,value)
            fields[key]=widget.get_id() if isinstance(widget,ElementSelector) else widget.get()
        return fields

    def dsl_texts(self,panel,restore=None):
        texts={}
        for book in self.widget_descendants(panel):
            if not isinstance(book,ttk.Notebook):continue
            for tab in book.tabs():
                title=book.tab(tab,'text')
                text=next((w for w in self.widget_descendants(book.nametowidget(tab)) if isinstance(w,tk.Text)),None)
                if text is None:continue
                if restore is not None and title in restore:
                    state=text.cget('state')
                    text.configure(state='normal')
                    text.delete('1.0','end')
                    text.insert('1.0',restore[title])
                    text.configure(state=state)
                texts[title]=text.get('1.0','end-1c')
        return texts

    def validation_records(self):
        return []

    def show_validation(self):
        win=tk.Toplevel(self)
        win.title(tr('shell.powerfactory_validation_retained_records'))
        text=tk.Text(win,width=100,height=24,wrap='word')
        text.pack(fill='both',expand=True)
        text.insert('end',tr('shell.historical_scope_of_the_records_a_modified'))
        for row in self.validation_records():text.insert('end',row['name']+' · '+row['state']+'\n'+row['source']+'\n\n')
        text.configure(state='disabled')
        return win

    def show_about(self):
        from datetime import datetime,timezone
        version_path=self.root_path/'power_machine.gui/app.py'
        build=read_json(self.root_path/'build_manifest.json',{})
        version=build.get('version',tr('shell.development'))
        date=build.get('build_date') or build.get('created_at_utc') or (datetime.fromtimestamp(version_path.stat().st_mtime,timezone.utc).isoformat() if version_path.exists() else tr('shell.source_checkout'))
        from tkinter import messagebox
        messagebox.showinfo(tr('shell.about_power_machine'),tr('shell.power_machine_version_build_date_development_build', value0=version, value1=date),parent=self)
