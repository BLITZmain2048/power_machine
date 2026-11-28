"""Comparación de dos LF por identidad, sin mezclar lotes de contingencias."""
from .i18n import tr
from .bus import bus
from .results_tables import localized_columns, bind_translations, format_value
from copy import deepcopy
import csv
import json
import math
import tkinter as tk
from tkinter import ttk, filedialog
from .workspace_links import check_link, calculation_sha256,relative_path,file_sha256


def difference(left, right):
    if any(not isinstance(v,(int,float)) or isinstance(v,bool) or not math.isfinite(v) for v in (left,right)):
        return None
    return right-left


def compare_flows(base_model,base_result,other_model,other_result):
    """Delta = segundo caso menos primero. Ausente o no resuelto no es cero."""
    from power_machine.loading import branch_loading
    from power_machine.lf_status import SOLVED
    def solved(result,group):
        ids=set()
        for island in result.get('islands',[]):
            if island.get('status') not in (SOLVED,'solved'):continue
            ids.update(island.get('buses',[]) if group=='buses' else island.get('elements',{}).get('branches',[]))
        return ids
    tables={}
    loads=[branch_loading(base_model,base_result),branch_loading(other_model,other_result)]
    for group in ('buses','branches'):
        maps=[{r.get('object_id',r.get('id')):r for r in res.get(group,[])} for res in (base_result,other_result)]
        allowed=[solved(res,group) for res in (base_result,other_result)]
        rows=[]
        for ident in sorted(set(maps[0])|set(maps[1])):
            a,b=maps[0].get(ident),maps[1].get(ident)
            valid=bool(a and b and ident in allowed[0] and ident in allowed[1])
            row=dict(object_id=ident,name=(b or a).get('name',ident),status='Comparable' if valid else 'Nuevo' if a is None else 'Retirado' if b is None else 'Sin solución comparable')
            if group=='buses':
                row.update(base_u_pu=(a or {}).get('u_pu'),other_u_pu=(b or {}).get('u_pu'))
                row['delta_u_pu']=difference(row['base_u_pu'],row['other_u_pu']) if valid else None
            else:
                for field in ('p_from_mw','q_from_mvar','p_to_mw','q_to_mvar'):
                    row['base_'+field]=(a or {}).get(field)
                    row['other_'+field]=(b or {}).get(field)
                    row['delta_'+field]=difference((a or {}).get(field),(b or {}).get(field)) if valid else None
                row['base_loading_pct']=loads[0].get(ident,{}).get('loading_pct')
                row['other_loading_pct']=loads[1].get(ident,{}).get('loading_pct')
                row['delta_loading_pct']=difference(row['base_loading_pct'],row['other_loading_pct']) if valid else None
            rows.append(row)
        tables[group]=rows
    return tables


COLUMNS={
 'buses':[('name',tr('results.busbar')),('status',tr('results.coverage')),('base_u_pu',tr('results.base_u_p_u')),('other_u_pu',tr('results.alternative_u_p_u')),('delta_u_pu',tr('results.u_p_u_d5c5')),('object_id',tr('results.id_ref'))],
 'branches':[('name',tr('results.branch')),('status',tr('results.coverage')),('delta_p_from_mw',tr('results.p_desde_mw')),('delta_q_from_mvar',tr('results.q_desde_mvar')),('delta_p_to_mw',tr('results.p_hasta_mw')),('delta_q_to_mvar',tr('results.q_hasta_mvar')),('base_loading_pct',tr('results.base_loading')),('other_loading_pct',tr('results.alternative_loading')),('delta_loading_pct',tr('results.loading_percentage_points')),('object_id',tr('results.id_ref'))]}
for field, unit in [('p_from_mw', 'MW'), ('q_from_mvar', 'Mvar'), ('p_to_mw', 'MW'), ('q_to_mvar', 'Mvar')]:
    position = next(i for i, (key, _) in enumerate(COLUMNS['branches']) if key == 'delta_' + field)
    COLUMNS['branches'][position:position] = [('base_' + field, tr('results.base_' + field)), ('other_' + field, tr('results.other_' + field))]



def export_comparison(path,kind,rows):
    with open(path,'w',newline='',encoding='utf-8-sig') as stream:
        writer=csv.writer(stream)
        writer.writerow([title for key,title in COLUMNS[kind]])
        writer.writerows([[row.get(key) for key,title in COLUMNS[kind]] for row in rows])


class CaseComparison(ttk.Frame):
    def __init__(self,parent,tables,on_select):
        super().__init__(parent)
        self.tables,self.views,self.order=tables,{},{}
        self._on_select = on_select
        self.book=ttk.Notebook(self)
        self.book.pack(fill='both',expand=True)
        self.columns = {kind: localized_columns(columns) for kind, columns in COLUMNS.items()}
        for kind,title in [('buses',tr('results.buses')),('branches',tr('results.branches'))]:
            frame=ttk.Frame(self.book)
            self.book.add(frame,text=title)
            ttk.Button(frame,text=tr('results.export_csv_fef2'),command=lambda kind=kind:self.export(kind)).pack(anchor='e')
            body=ttk.Frame(frame)
            body.pack(fill='both',expand=True)
            tree=ttk.Treeview(body,columns=[k for k,t in COLUMNS[kind]],show='headings')
            tree.grid(row=0,column=0,sticky='nsew')
            body.rowconfigure(0,weight=1)
            body.columnconfigure(0,weight=1)
            for orient,axis in [('vertical','y'),('horizontal','x')]:
                bar=ttk.Scrollbar(body,orient=orient,command=getattr(tree,axis+'view'))
                bar.grid(row=0 if axis=='y' else 1,column=1 if axis=='y' else 0,sticky='ns' if axis=='y' else 'ew')
                tree.configure(**{axis+'scrollcommand':bar.set})
            for key,label in self.columns[kind]:
                tree.heading(key,text=label,command=lambda kind=kind,key=key:self.sort(kind,key))
                tree.column(key,width=240 if key in ('name','object_id') else 155,minwidth=100)
            tree.bind('<<TreeviewSelect>>', lambda event, tree=tree: bus.publish('selection.changed', ids=list(tree.selection()), source='results'))
            tree.bind('<Double-1>',lambda event,tree=tree:on_select(tree.selection()[0]) if tree.selection() else None)
            self.views[kind]=tree
            self.populate(kind)
        bind_translations(self)
        self._selection_token = bus.subscribe('selection.changed', self._receive_selection)
        self.bind('<Destroy>', lambda event: bus.unsubscribe(self._selection_token) if event.widget is self else None, add='+')

    def _receive_selection(self, ids, source=None):
        for tree in self.views.values():
            selected = tuple(str(i) for i in ids if tree.exists(str(i)))
            if selected != tree.selection():
                tree.selection_set(selected)
                if selected:
                    tree.see(selected[0])

    def retranslate(self):
        for index, kind in enumerate(('buses', 'branches')):
            self.book.tab(index, text=tr('results.' + kind))
            self.columns[kind] = localized_columns(self.columns[kind])
            for key, label in self.columns[kind]:
                self.views[kind].heading(key, text=label)
            self.populate(kind)

    def populate(self,kind):
        tree=self.views[kind]
        tree.delete(*tree.get_children())
        for row in self.tables[kind]:
            values = dict(row)
            status_key = {'Comparable': 'comparable', 'Nuevo': 'new', 'Retirado': 'retired',
                          'Sin solución comparable': 'unsolved_comparison'}.get(row.get('status'))
            if status_key:
                values['status'] = tr('results.' + status_key)
            tree.insert('','end',iid=row['object_id'],values=['—' if values.get(key) is None else format_value(values[key], key) for key,title in COLUMNS[kind]])

    def sort(self,kind,key):
        from .results_tables import sorted_rows
        reverse=not self.order.get((kind,key),True)
        self.order[kind,key]=reverse
        self.tables[kind]=sorted_rows(self.tables[kind],key,reverse)
        self.populate(kind)

    def export(self,kind):
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.csv')
        if path:export_comparison(path,kind,self.tables[kind])


class CompareActions:
    def compare_cases_dialog(self):
        if not self.available():return
        cases=self.document.to_data()['cases']
        labels={c['name']+' · '+c['case_id']:c['case_id'] for c in cases}
        values=list(labels)
        if len(values)<2:
            self.output.log('AVISO',tr('results.need_two_cases'))
            return
        return self.form(tr('results.compare_cases'),[
            ('base',tr('results.base_case'),values[0],values),('other',tr('results.other_case'),values[1],values)],
            lambda fields:self.compare_cases(labels[fields['base']],labels[fields['other']]))

    def compare_cases(self,base_id,other_id):
        if base_id==other_id:raise ValueError(tr('results.different_cases'))
        if not self.available():return
        doc=self.document
        revision=doc.revision
        data=doc.to_data()
        inputs=[]
        captured_dependencies=[]
        cache=getattr(self,'_case_flow_cache',{})
        self._case_flow_cache=cache
        from .workspace_links import engine_sha256
        engine=engine_sha256()
        cache_keys=[]
        for ident in (base_id,other_id):
            model,study,deps=doc.materialize_case(ident)
            captured_dependencies.append(deepcopy(deps))
            cache_key=(id(doc),ident,calculation_sha256(deps),engine)
            cache_keys.append(cache_key)
            reused=deepcopy(cache.get(cache_key))
            for link in reversed(data['result_links']):
                if link['case_id']==ident and link['module']=='loadflow' and check_link(link,deps,ident)=='current':
                    reused=json.loads((self.root_path/link['result_path']).read_text(encoding='utf-8'))
                    break
            inputs.append((model,study,reused))
        names={c['case_id']:c['name'] for c in data['cases']}
        def calculate():
            flows=[cached if cached is not None else self.api.run_loadflow(self.root_path,study,model=model) for model,study,cached in inputs]
            return dict(tables=compare_flows(inputs[0][0],flows[0],inputs[1][0],flows[1]),
                        case_ids=[base_id,other_id],case_names=[names[base_id],names[other_id]],
                        reused=[value[2] is not None for value in inputs],flows=flows,
                        note=tr('results.comparison_note'))
        def show(result):
            if self.document is not doc or any(doc.materialize_case(ident)[2]!=deps for ident,deps in zip((base_id,other_id),captured_dependencies)):
                self.output.log('AVISO',tr('results.comparison_discarded'))
                return
            self.last_comparison=result
            for key,flow in zip(cache_keys,result['flows']):cache[key]=deepcopy(flow)
            window=tk.Toplevel(self)
            window.title(tr('results.comparison_title', other=names[other_id], base=names[base_id]))
            window.geometry('1150x650')
            ttk.Label(window,text=result['note'],wraplength=1000).pack(fill='x',padx=8,pady=8)
            table=CaseComparison(window,deepcopy(result['tables']),self.select)
            table.pack(fill='both',expand=True)
            window.comparison=table
            self.comparison_window=window
        self.start_task(tr('results.comparing'),calculate,show)
