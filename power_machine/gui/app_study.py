"""Integración pública del documento v2, navegación e informes de estudio."""
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import json
import tkinter as tk
from tkinter import ttk, simpledialog, filedialog, messagebox
from .workspace_links import file_sha256, relative_path, calculation_sha256
from .document import Document
from .i18n import tr, language
from .theme import theme
from . import study_document as v2


class StudyActions:
    def connect_study_menus(self):
        """Registra acciones por claves estables, nunca por etiquetas traducidas."""
        menu = self.menus['file']
        menu.add_command(label=tr('study.generate_report'), command=self.generate_report_dialog)
        menu.add_command(label=tr('document.open_recent'), command=self.open_recent_dialog)
        study = tk.Menu(self.menu, tearoff=False)
        self.menus['edit'].add_cascade(label=tr('study.menu'), menu=study)
        for key, action in [('study.new_case', lambda:self.study_action('new')),
                ('study.duplicate_case', lambda:self.study_action('duplicate')),
                ('study.rename_case', lambda:self.study_action('rename')),
                ('study.manager', self.study_selector),
                ('study.new_scenario', lambda:self.study_action('scenario')),
                ('study.new_variant', lambda:self.study_action('variant')),
                ('study.new_stage', lambda:self.study_action('stage')),
                ('study.run_history', self.run_history_dialog),
                ('study.undo', self.undo_study), ('study.redo', self.redo_study)]:
            study.add_command(label=tr(key), command=action)
        self.menus['tools'].add_command(label=tr('study.protection_bridge'), command=self.protection_bridge_dialog)
        self.menus['tools'].add_command(label=tr('study.store_protection'), command=self.store_protection_workspace)

    def open_recent_dialog(self):
        """Abre documentos recientes desde Settings sin persistir rutas manualmente."""
        settings = getattr(self, 'settings', None)
        recent = [Path(value) for value in (settings.get('recent_files', []) if settings else []) if Path(value).is_file()]
        if not recent:
            messagebox.showinfo(tr('document.recent'), tr('document.no_recent'), parent=self)
            return
        win = tk.Toplevel(self)
        win.title(tr('document.recent'))
        values = tk.Listbox(win, height=min(10, len(recent)), exportselection=False)
        for path in recent:
            values.insert('end', path.name)
        values.pack(fill='both', expand=True, padx=theme.pad_l, pady=theme.pad_l)
        def open_selected():
            chosen = values.curselection()
            if not chosen or not self.maybe_save():
                return
            try:
                document = Document.open(recent[chosen[0]], self.api.StudyCase)
                self.install_document(document)
                win.destroy()
            except Exception as exc:
                self.show_error(exc)
        ttk.Button(win, text=tr('document.open'), command=open_selected).pack(side='left', padx=theme.pad_l, pady=(0,theme.pad_l))
        ttk.Button(win, text=tr('study.close'), command=win.destroy).pack(side='right', padx=theme.pad_l, pady=(0,theme.pad_l))

    def store_protection_workspace(self):
        if not self.available():return
        item=self.parity_panels.get('protection')
        if not item:
            self.open_parity_panel('protection')
            return
        try:
            doc=self.document
            case=next(c for c in doc.to_data()['cases'] if c['case_id']==doc.active_case_id)
            existing=next((w for w in doc.to_data()['workspaces'] if w['kind']=='protection' and w['workspace_id'] in case['workspace_ids']),None)
            ident=doc.put_workspace('protection',deepcopy(item[1].panel.controller.workspace),{},doc.revision,
                                    workspace_id=existing['workspace_id'] if existing else None)
            doc.configure_case(doc.active_case_id,doc.revision,workspace_ids=list(dict.fromkeys(case['workspace_ids']+[ident])))
            self.update_document()
        except Exception as exc:self.show_error(exc)

    def study_identity(self):
        key=(id(self.document),self.document.revision,language())
        if getattr(self,'_identity_key',None)==key:return self._identity_label
        data = self.document.to_data()
        case = next(c for c in data['cases'] if c['case_id']==data['active_case_id'])
        scenario = next(s for s in data['operating_scenarios'] if s['scenario_id']==case['scenario_id'])
        variant = next((v for v in data['network_variants'] if v['variant_id']==case['variant_id']), None)
        stage = next((s for s in (variant or {}).get('stages',[]) if s['stage_id']==case['stage_id']), None)
        self._identity_key=key
        scenario_name = tr(scenario['default_name_key']) if scenario.get('default_name_key') else scenario['name']
        self._identity_label=' · '.join([case['name'], scenario_name,
            variant['name'] if variant else tr('study.default_network'),
            stage['name'] if stage else tr('study.no_stage')])
        return self._identity_label

    def activate_study(self, case_id=None):
        if not self.available(): return
        document=self.document
        self.capture_presentation()
        self.close_parity_panels()
        if hasattr(self,'analysis'):self.analysis.select_study('flow',open_panel=False)
        model, study, dependencies=document.activate_case(case_id or document.active_case_id,document.revision)
        # La fachada mantiene study operativo para compatibilidad con los paneles.
        self.model=document.network.model if document.network.state is not None else self.base_model
        self.index=self.api.element_index(self.model)
        self.output.index=self.index
        self.catalog=self.api.signal_catalog(self.root_path,model=model)
        self.update_document()
        self.rebuild_plots()
        return model, study, dependencies

    def study_action(self, action, name=None, effective_at=None):
        if not self.available(): return
        doc=self.document
        name=name or simpledialog.askstring(tr('study.menu'), tr('study.name'), parent=self)
        if not name: return
        try:
            if action in ('new','duplicate'):
                ident=doc.duplicate_case(doc.active_case_id,name,doc.revision)
                if action=='new':
                    scenario=doc.create_scenario(name+' · '+tr('study.operation'),{},doc.revision)
                    doc.configure_case(ident,doc.revision,scenario_id=scenario,variant_id=None,stage_id=None)
                return self.activate_study(ident)
            if action=='rename': doc.configure_case(doc.active_case_id,doc.revision,name=name)
            if action=='scenario':
                ident=doc.create_scenario(name,deepcopy(doc.study.scenario),doc.revision)
                doc.configure_case(doc.active_case_id,doc.revision,scenario_id=ident)
            if action=='variant':
                ident=doc.create_variant(name,doc.revision)
                stage=doc.add_stage(ident,tr('study.initial_stage'),datetime.now(timezone.utc).isoformat(),[],doc.revision)
                doc.configure_case(doc.active_case_id,doc.revision,variant_id=ident,stage_id=stage)
            if action=='stage':
                data=doc.to_data()
                case=next(c for c in data['cases'] if c['case_id']==doc.active_case_id)
                if not case['variant_id']: raise ValueError(tr('study.select_variant_first'))
                date=effective_at or simpledialog.askstring(tr('study.stage'),tr('study.stage_date'),parent=self)
                if not date:return
                ident=doc.add_stage(case['variant_id'],name,date,[],doc.revision,parent_stage_id=case['stage_id'])
                doc.configure_case(doc.active_case_id,doc.revision,stage_id=ident)
            return self.activate_study()
        except Exception as exc:self.show_error(exc)

    def study_selector(self):
        if not self.available():return
        win=tk.Toplevel(self)
        win.title(tr('study.manager_title'))
        win.geometry('1080x500')
        tree=ttk.Treeview(win,columns=('kind','state'),height=16,selectmode='extended')
        tree.heading('#0',text=tr('study.name'))
        tree.heading('kind',text=tr('study.kind'))
        tree.heading('state',text=tr('study.state'))
        tree.column('#0',width=420)
        tree.column('kind',width=130)
        tree.column('state',width=120)
        tree.pack(fill='both',expand=True,padx=theme.pad_l,pady=theme.pad_l)
        identities={}
        def populate():
            tree.delete(*tree.get_children())
            identities.clear()
            data=self.document.to_data()
            for group,key,label_key in [('cases','case_id','study.case'),('operating_scenarios','scenario_id','study.scenario'),('network_variants','variant_id','study.variant')]:
                parent=tree.insert('', 'end',text=tr('study.group.'+group),values=(tr(label_key),''),open=True)
                for row in data[group]:
                    ident=row[key]
                    state=tr('study.active') if group=='cases' and ident==data['active_case_id'] else ''
                    display_name=tr(row['default_name_key']) if row.get('default_name_key') else row['name']
                    tree.insert(parent,'end',iid=ident,text=display_name,values=(tr(label_key),state))
                    identities[ident]=(key,ident)
                    for stage in row.get('stages',[]):
                        sid=stage['stage_id']
                        tree.insert(ident,'end',iid=sid,text=stage['name']+' · '+stage['effective_at'],values=(tr('study.stage'),''))
                        identities[sid]=('stage',(ident,sid))
            if data.get('trash'):
                parent=tree.insert('', 'end',text=tr('study.trash'),values=(tr('study.trash'),''),open=False)
                for index, entry in enumerate(data['trash']):
                    iid='trash:'+str(index)
                    tree.insert(parent,'end',iid=iid,text=entry['item'].get('name',tr('study.unnamed')),values=(tr('study.'+entry['kind']),tr('study.deleted')))
                    identities[iid]=('trash',index)
        def selected():
            return [identities[item] for item in tree.selection() if item in identities]
        def apply():
            rows=selected()
            if len(rows)!=1:return
            kind,value=rows[0]
            try:
                doc=self.document
                if kind=='case_id':self.activate_study(value)
                elif kind=='stage': doc.configure_case(doc.active_case_id,doc.revision,variant_id=value[0],stage_id=value[1]); self.activate_study()
                elif kind=='variant_id': doc.configure_case(doc.active_case_id,doc.revision,variant_id=value,stage_id=None); self.activate_study()
                elif kind=='scenario_id': doc.configure_case(doc.active_case_id,doc.revision,scenario_id=value); self.activate_study()
                elif kind=='trash': doc.restore_from_trash(value,doc.revision); self.update_document()
                populate()
            except Exception as exc:self.show_error(exc)
        def rename():
            rows=selected()
            if len(rows)!=1 or rows[0][0] not in ('case_id','scenario_id','variant_id','stage'):return
            kind, identity=rows[0]
            value=simpledialog.askstring(tr('study.rename_case'),tr('study.name'),parent=win)
            if not value:return
            try:
                if kind=='case_id': self.document.configure_case(identity,self.document.revision,name=value)
                else:
                    collection,key={'scenario_id':('operating_scenarios','scenario_id'),'variant_id':('network_variants','variant_id')}[kind]
                    def mutate(data):
                        if kind=='stage': v2.find(v2.find(data['network_variants'],'variant_id',identity[0])['stages'],'stage_id',identity[1])['name']=value
                        else: v2.find(data[collection],key,identity)['name']=value
                    self.document._transaction(mutate,self.document.revision,tr('integration.rename_object'))
                self.update_document(); populate()
            except Exception as exc:self.show_error(exc)
        def remove():
            rows=selected()
            if len(rows)!=1 or rows[0][0] not in ('case_id','scenario_id','variant_id'):return
            kind, identity=rows[0]
            kind={'case_id':'case','scenario_id':'scenario','variant_id':'variant'}[kind]
            if not messagebox.askyesno(tr('study.confirm_title'),tr('study.confirm_delete'),parent=win):return
            try:self.document.move_to_trash(kind,identity,self.document.revision); self.update_document(); populate()
            except Exception as exc:self.show_error(exc)
        def compare():
            cases=[value for kind,value in selected() if kind=='case_id']
            if len(cases)!=2:
                messagebox.showinfo(tr('study.compare'),tr('study.select_two_cases'),parent=win);return
            try:
                first, second=[self.document.case_summary(case) for case in cases]
                messagebox.showinfo(tr('study.compare'),tr('study.compare_summary',first=first['name'],second=second['name'],runs_a=first['result_count'],runs_b=second['result_count']),parent=win)
            except Exception as exc:self.show_error(exc)
        bar=ttk.Frame(win,padding=theme.pad_m);bar.pack(fill='x')
        def create(action):
            self.study_action(action)
            populate()
        win.case_tree = tree
        win.case_actions = {}
        for key,command in [('study.new_case',lambda:create('new')),('study.duplicate_case',lambda:create('duplicate')),('study.activate',apply),('study.rename',rename),('study.delete',remove),('study.compare',compare)]:
            button = ttk.Button(bar,text=tr(key),command=command)
            button.pack(side='left',padx=(0,theme.pad_s))
            win.case_actions[key] = button
        ttk.Button(bar,text=tr('study.close'),command=win.destroy).pack(side='right')
        populate()
        return win

    def _publish_stale_runs(self):
        """Expone al shell el cambio de vigencia sin mutar una corrida archivada."""
        if not self.document:
            return
        stale = [row for row in self.document.runs_for_case() if row['freshness'] != 'current']
        if stale and hasattr(self, 'bus'):
            self.bus.publish('results.stale', reason=tr('study.results_stale'))

    def undo_study(self):
        if self.available() and self.document.undo():
            self.update_document()
            self._publish_stale_runs()

    def redo_study(self):
        if self.available() and self.document.redo():
            self.update_document()
            self._publish_stale_runs()

    def run_history_dialog(self):
        if not self.available():
            return
        win=tk.Toplevel(self)
        win.title(tr('study.run_history_title'))
        win.geometry('850x390')
        columns=('module','created','status','freshness')
        tree=ttk.Treeview(win,columns=columns,show='headings')
        for column, key, width in [('module','study.module',160),('created','study.created',245),
                                   ('status','study.status',150),('freshness','study.freshness',170)]:
            tree.heading(column,text=tr(key)); tree.column(column,width=width,anchor='w')
        tree.pack(fill='both',expand=True,padx=theme.pad_l,pady=theme.pad_l)
        def run_values(row):
            module = row['module']
            if module == 'loadflow':
                name = tr('shell.load_flow')
            elif module in ('rms','simulation'):
                name = tr('shell.rms_simulation')
            else:
                from .panels.panel_widgets import CapabilityGate
                panel_id = row.get('panel_id') or next((key for key,value in CapabilityGate.MODULES.items() if value==module), None)
                entry = self.panel_registry.entries.get(panel_id)
                name = entry.display_title() if entry else module
            stamp = row['created_at_utc']
            try:
                stamp = datetime.fromisoformat(stamp.replace('Z','+00:00')).astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')
            except (ValueError,TypeError):
                pass
            return (name,stamp,tr('study.run_status.'+row['status']),
                    tr('study.current') if row['freshness']=='current' else tr('study.stale'))
        rows=self.document.runs_for_case()
        for row in rows:
            tree.insert('', 'end',iid=row['run_id'],values=run_values(row))
        self._publish_stale_runs()
        detail = tk.StringVar(win, tr('int2.history_select'))
        ttk.Label(win, textvariable=detail, wraplength=800).pack(fill='x', padx=theme.pad_l, pady=4)
        def selected():
            ident = tree.selection()
            return next((row for row in self.document.runs_for_case() if ident and row['run_id'] == ident[0]), None)
        def explain(_=None):
            row = selected()
            if not row:
                detail.set(tr('int2.history_select'))
                return
            identity = tr('int2.history_identity',module=row['module'],timestamp=row['created_at_utc'])+'\n'
            if row['freshness'] == 'current':
                detail.set(identity+tr('int2.history_current'))
                return
            _, _, current = self.document.materialize_case(self.document.active_case_id)
            changed = [tr('int2.dependency.' + name) for name in (
                'base_electrical_sha256','materialized_electrical_sha256','scenario_sha256',
                'options_sha256','events_sha256','workspace_hashes')
                if name in current and row.get('dependencies',{}).get(name) != current[name]]
            detail.set(identity+tr('int2.history_stale', reason=', '.join(changed) or tr('int2.integrity_or_engine')))
        def open_result():
            row = selected()
            if not row: return
            try:
                content = (self.root_path / row['result_path']).read_text(encoding='utf-8')
                window = tk.Toplevel(win)
                window.title(tr('int2.archived_result'))
                window.geometry('900x600')
                ttk.Label(window,text=tr('int2.archive_readonly'),padding=8).pack(fill='x')
                text=tk.Text(window,wrap='none')
                text.pack(fill='both',expand=True)
                from .display import public_text
                text.insert('end',public_text(content))
                text.configure(state='disabled')
            except Exception as exc: self.show_error(exc)
        def recalculate():
            row = selected()
            if not row: return
            module=row['module']
            if module == 'loadflow':
                self.run_loadflow()
            elif module in ('rms','simulation'):
                self.simulation_dialog()
            else:
                from .panels.panel_widgets import CapabilityGate
                panel_id=row.get('panel_id') or next((key for key,value in CapabilityGate.MODULES.items() if value==module), None)
                if panel_id:
                    self.open_parity_panel(panel_id)
                else:
                    messagebox.showinfo(tr('int2.recalculate'), tr('int2.recalculate_select_panel'), parent=win)
        tree.bind('<<TreeviewSelect>>',explain)
        bar=ttk.Frame(win,padding=theme.pad_m);bar.pack(fill='x')
        win.history_actions={}
        for key,command in [('int2.open_result',open_result),('int2.recalculate',recalculate),
                            ('study.undo',lambda:(self.undo_study(),refresh())),
                            ('study.redo',lambda:(self.redo_study(),refresh()))]:
            button=ttk.Button(bar,text=tr(key),command=command)
            button.pack(side='left',padx=theme.pad_s)
            win.history_actions[key]=button
        ttk.Button(bar,text=tr('study.close'),command=win.destroy).pack(side='right')
        win.history_tree,win.history_detail = tree,detail
        def refresh():
            for item in tree.get_children(): tree.delete(item)
            for row in self.document.runs_for_case():
                tree.insert('', 'end',iid=row['run_id'],values=run_values(row))
            explain()
        def retranslate():
            selection = tree.selection()
            win.title(tr('study.run_history_title'))
            for column, key in (('module','study.module'),('created','study.created'),('status','study.status'),('freshness','study.freshness')):
                tree.heading(column,text=tr(key))
            for key,button in win.history_actions.items():
                button.configure(text=tr(key))
            refresh()
            for ident in selection:
                if tree.exists(ident):tree.selection_add(ident)
            explain()
        win.retranslate = retranslate
        return win

    def archive_result(self, module, result, panel_id=None, dependencies=None):
        """Cierra el JSON antes de enlazarlo y conserva versiones por corrida."""
        from .paths import run_directory
        doc=self.document
        if dependencies is None:
            _,_,dependencies=doc.materialize_case(doc.active_case_id)
        dependencies=deepcopy(dependencies)
        from .workspace_links import engine_sha256
        dependencies.update(engine_id=module, engine_source='power_machine', engine_version_sha256=engine_sha256(),
                            input_files=deepcopy(dependencies.get('input_files',[]))+deepcopy(result.get('provenance',{}).get('input_files',[])))
        path=run_directory(self.root_path,module)/'result.json'
        path.write_text(json.dumps(result,ensure_ascii=False,allow_nan=False,indent=2),encoding='utf-8')
        link=dict(run_id=str(uuid4()),case_id=doc.active_case_id,module=module,panel_id=panel_id,
                  created_at_utc=datetime.now(timezone.utc).isoformat(),result_path=str(path),result_file_sha256=file_sha256(path),
                  schema=result.get('schema','power_machine-loadflow-v1'),status=result.get('status','completed'),raw_status=result.get('status','completed'),
                  cancelled=bool(result.get('cancelled',False)),dependencies=dependencies,calculation_sha256=calculation_sha256(dependencies),
                  validation=result.get('gui_validation_label','sin validar contra la referencia externa'),coverage=result.get('coverage',{}))
        doc.attach_result(link)
        if hasattr(self, 'bus'):
            self.bus.publish('results.updated', study=doc.active_case_id, result_id=link['run_id'])
        return link

    def generate_report(self, output_dir):
        from .study_report import generate_study_report
        doc=self.document
        rows=[]
        data=doc.to_data()
        active_model,_,dependencies=doc.materialize_case(doc.active_case_id)
        from .workspace_links import check_link
        for link in data['result_links']:
            if link['case_id']!=doc.active_case_id:continue
            freshness=check_link(link,dependencies,doc.active_case_id)
            try: result=json.loads((self.root_path/link['result_path']).read_text(encoding='utf-8'))
            except (OSError,ValueError):result={}
            rows.append(dict(module=link['module'],run_id=link['run_id'],status=link['status'],freshness=freshness,
                             validation_label=link['validation'],coverage=link['coverage'],result=result))
        snapshot=dict(study_id=data['document_id'],title=self.study_identity(),model=data['base'],network_model=active_model,
                      scenario=doc.snapshot()['scenario'],objective=tr('integration.active_study_report'),missing_data=[tr('integration.no_new_comparison_against_reference_simulator')])
        return generate_study_report(output_dir,snapshot,rows,compile_pdf=False)

    def generate_report_dialog(self):
        if not self.available():return
        try:
            destination=filedialog.askdirectory(parent=self, initialdir=self.root_path, title=tr('int2.report_destination'))
            if not destination:return
            path=Path(destination).resolve()
            if not path.is_relative_to(self.root_path.resolve()):
                raise ValueError(tr('integration.exports_must_remain_inside_the_project'))
            # Cada revisi?n conserva su propio informe y no sustituye otro hist?rico.
            path=path/self.document.to_data()['document_id']/(str(self.document.revision)+'_'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S_%f'))
            self.capture_presentation()
            report=self.generate_report(path)
            self.output.log('INFO',tr('int2.report_saved',name=Path(report['tex']).name))
            if messagebox.askyesno(tr('study.generate_report'),tr('int2.report_open_folder'),parent=self):
                import os
                if hasattr(os,'startfile'):os.startfile(str(path))
        except Exception as exc:self.show_error(exc)

    def protection_bridge_dialog(self):
        self.open_parity_panel('protection')
        self.output.log('AVISO',tr('integration.sc_to_tcc_requires_terminal_mapping_ct'))
        item=self.parity_panels.get('protection')
        if item is None:return
        if not hasattr(item[1].panel,'controller'):
            self.output.log('AVISO',tr('integration.the_protection_panel_exposes_no_workspace_controller'))
            return
        win=tk.Toplevel(self)
        win.title(tr('integration.sc_to_terminal_protection'))
        ttk.Label(win,text=tr('integration.traceable_terminal_mapping_json_with_ct_and'),wraplength=460).pack(padx=12,pady=8)
        path=tk.StringVar()
        ttk.Entry(win,textvariable=path,width=64).pack(padx=12)
        def choose():
            value=filedialog.askopenfilename(parent=win,initialdir=self.root_path,filetypes=[('Mapping','*.json')])
            if value:path.set(value)
        ttk.Button(win,text=tr('integration.choose_mapping'),command=choose).pack()
        minimum,maximum=tk.StringVar(value='100'),tk.StringVar(value='10000')
        for label,var in [(tr('integration.minimum_current_a'),minimum),(tr('integration.maximum_current_a'),maximum)]:
            ttk.Label(win,text=label).pack()
            ttk.Entry(win,textvariable=var).pack()
        pair_fields={key:tk.StringVar() for key in ('primary','backup','breaker','margin')}
        stages=list(item[1].panel.controller.workspace.get('stages',{}))
        for key,label in [('primary',tr('integration.primary_stage')),('backup',tr('integration.backup_stage')),('breaker',tr('integration.circuit_breaker_opening_s')),('margin',tr('integration.required_study_margin_s'))]:
            ttk.Label(win,text=label).pack()
            if key in ('primary','backup'):
                ttk.Combobox(win,textvariable=pair_fields[key],values=stages,state='readonly').pack()
            else:ttk.Entry(win,textvariable=pair_fields[key]).pack()
        feedback=tk.StringVar()
        ttk.Label(win,textvariable=feedback,wraplength=460).pack()
        def apply():
            try:
                cc=self.parity_results.get('shortcircuit')
                if not cc or cc.get('stale'):raise ValueError(tr('integration.run_current_sc_before_linking_protection'))
                mapping_path=Path(path.get()).resolve()
                if not mapping_path.is_relative_to(self.root_path):raise ValueError(tr('integration.mapping_outside_the_project'))
                mapping=json.loads(mapping_path.read_text(encoding='utf-8-sig'))
                controller=item[1].panel.controller
                from power_machine.protection import CoordinationPair
                if not pair_fields['primary'].get() or not pair_fields['backup'].get():
                    raise ValueError(tr('integration.load_and_select_primary_and_backup_stages'))
                pair=CoordinationPair(pair_fields['primary'].get(),pair_fields['backup'].get(),
                    float(pair_fields['breaker'].get()),float(pair_fields['margin'].get()))
                controller.submit([float(minimum.get()),float(maximum.get())],(pair,),
                    shortcircuit_result=deepcopy(cc),terminal_mapping=mapping)
                win.destroy()
            except Exception as exc:feedback.set(str(exc))
        ttk.Button(win,text=tr('integration.link_and_evaluate_coverage'),command=apply).pack(pady=8)
        return win
