"""Estudios en lote sobre una instantánea, sin operaciones Tk desde los trabajadores."""
from copy import deepcopy
from importlib import import_module
from queue import Queue, Empty
import threading
import tkinter as tk
from tkinter import ttk

from .panels.panel_widgets import ResultTable
from .paths import run_directory
from .display import public_text, short_path
from .bus import bus
from .i18n import tr
from .theme import theme


def batch_available():
    try:
        module=import_module('power_machine.batch')
        return all(hasattr(module,key) for key in ('run_batch','BatchOptions','Scenario'))
    except ImportError:
        return False


def batch_scenarios(model, count, low, high):
    """Construye multiplicadores declarados por el usuario, sin editar su red."""
    from power_machine.batch import Scenario
    count=int(count)
    low,high=float(low),float(high)
    if not 1 <= count <= 10000 or not 0 < low <= high:
        raise ValueError(tr('batch.error.range'))
    ids=[item['id'] for item in model.get('loads',[]) if not item.get('outserv')]
    if not ids:
        raise ValueError(tr('batch.error.no_loads'))
    return [Scenario(id=f'escenario_{i+1:04d}',load_multipliers={key:low+(high-low)*i/max(1,count-1) for key in ids})
            for i in range(count)]


class BatchWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app=app
        self.title(tr('batch.title'))
        self.geometry('1100x620')
        self.cancel=threading.Event()
        self.queue=Queue()
        self.closed=False
        self.running=False
        self.timer=None
        self.status=tk.StringVar(value=tr('batch.intro'))
        form=ttk.Frame(self,padding=12)
        form.pack(fill='x')
        self.study_kind='loadflow'
        self.study=tk.StringVar(value=tr('batch.loadflow'))
        self.workers=tk.StringVar(value='2')
        self.count=tk.StringVar(value='11')
        self.low=tk.StringVar(value='0.95')
        self.high=tk.StringVar(value='1.05')
        self.study_label=ttk.Label(form,text=tr('batch.study')); self.study_label.grid(row=0,column=0,sticky='w')
        self.study_box=ttk.Combobox(form,textvariable=self.study,values=(tr('batch.loadflow'),tr('batch.contingency')),state='readonly',width=24); self.study_box.grid(row=1,column=0,padx=(0,12))
        self.study_box.bind('<<ComboboxSelected>>', lambda _event: setattr(self, 'study_kind', 'loadflow' if self.study.get() == tr('batch.loadflow') else 'contingency'))
        self.form_labels=[]
        for col,(key,var) in enumerate((('workers',self.workers),('scenarios',self.count),('low',self.low),('high',self.high)),1):
            label=ttk.Label(form,text=tr('batch.'+key)); label.grid(row=0,column=col,sticky='w'); self.form_labels.append((key,label))
            ttk.Entry(form,textvariable=var,width=12).grid(row=1,column=col,padx=(0,12))
        self.intro_label=ttk.Label(form,text=tr('batch.intro'),wraplength=1000); self.intro_label.grid(row=2,column=0,columnspan=5,sticky='w',pady=10)
        self.button=ttk.Button(form,text=tr('batch.run'),command=self.start)
        self.button.grid(row=3,column=0,sticky='w')
        self.cancel_button=ttk.Button(form,text=tr('batch.cancel'),command=self.request_cancel); self.cancel_button.grid(row=3,column=1,sticky='w')
        ttk.Label(self,textvariable=self.status,padding=10).pack(fill='x')
        self.table=ResultTable(self,[('id',tr('integration.scenario')),('status',tr('integration.status')),('v_min_pu',tr('integration.minimum_u_p_u')),
            ('max_loading_pct',tr('integration.maximum_loading')),('unserved_mw',tr('integration.unsupplied_load_mw')),('duration_s',tr('integration.time_s'))],app.root_path)
        self.table.pack(fill='both',expand=True,padx=10,pady=10)
        self.protocol('WM_DELETE_WINDOW',self.close)
        self._subscriptions=[bus.subscribe('language.changed',self._language_changed),bus.subscribe('theme.changed',self._theme_changed)]
        self.bind('<Destroy>',self._dispose,add='+')

    def start(self):
        if self.running:
            return
        try:
            from power_machine.batch import run_batch, BatchOptions
            model=deepcopy(self.app.operational)
            scenarios=batch_scenarios(model,self.count.get(),self.low.get(),self.high.get())
            options=BatchOptions(workers=int(self.workers.get()),blas_threads=1)
            study=self.study_kind
            directory=run_directory(self.app.root_path,'lote')
            document=self.app.document
            revision=document.revision
            study_options={'q_limits':bool(document.study.ldf_options.get('q_limits',True))}
        except Exception as exc:
            self.status.set(tr('batch.preflight', message=public_text(exc)))
            return
        self.cancel.clear()
        self.running=True
        self.button.state(['disabled'])
        self.status.set(tr('batch.running', count=len(scenarios), workers=options.workers))
        bus.publish('worker.progress',kind='batch',completed=0,total=len(scenarios),cancelling=False)
        def work():
            try:
                result=run_batch(model,study,scenarios,study_options=study_options,options=options,output=directory,
                                 progress=lambda row:self.queue.put(('progress',row)),cancel=self.cancel)
                self.queue.put(('result',(result,directory,document,revision)))
            except Exception as exc:
                self.queue.put(('error',str(exc)))
        threading.Thread(target=work,daemon=True,name='power-machine-lotes').start()
        self.timer=self.after(75,self.poll)

    def poll(self):
        self.timer=None
        if self.closed:
            return
        try:
            while True:
                kind,value=self.queue.get_nowait()
                if kind=='progress':
                    self.status.set(tr('batch.progress',completed=value.get('completed',0),total=value.get('total',0)))
                    bus.publish('worker.progress',kind='batch',completed=value.get('completed',0),total=value.get('total',0),cancelling=self.cancel.is_set())
                elif kind=='result':
                    result,path,document,revision=value
                    self.table.set_rows(result.to_records())
                    stale=document is not self.app.document or revision!=document.revision
                    self.status.set((tr('batch.stale') if stale else tr('batch.finished'))+short_path(path,self.app.root_path))
                    self.running=False
                    self.button.state(['!disabled'])
                elif kind=='error':
                    self.status.set(tr('batch.interrupted',message=public_text(value)))
                    self.running=False
                    self.button.state(['!disabled'])
        except Empty:
            pass
        if self.running:
            self.timer=self.after(75,self.poll)

    def close(self):
        self.closed=True
        self.cancel.set()
        if self.timer:
            self.after_cancel(self.timer)
        self.destroy()

    def request_cancel(self):
        self.cancel.set(); bus.publish('worker.cancelled',kind='batch',requested=True)

    def _language_changed(self, **_):
        self.retranslate()

    def _theme_changed(self, **_):
        theme.apply(self)

    def _dispose(self,event):
        if event.widget is self:
            for token in self._subscriptions: bus.unsubscribe(token)
            self._subscriptions=[]

    def retranslate(self):
        self.title(tr('batch.title'))
        self.study_label.configure(text=tr('batch.study'))
        self.study_box.configure(values=(tr('batch.loadflow'),tr('batch.contingency')))
        self.study.set(tr('batch.' + self.study_kind))
        for key,label in self.form_labels: label.configure(text=tr('batch.'+key))
        self.intro_label.configure(text=tr('batch.intro'))
        self.button.configure(text=tr('batch.run'))
        self.cancel_button.configure(text=tr('batch.cancel'))
