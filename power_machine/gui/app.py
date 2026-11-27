"""Ventana de ingeniería power_machine. Los cálculos pertenecen exclusivamente al motor."""
from copy import deepcopy
from functools import lru_cache
import importlib
import json
import math
from pathlib import Path
from queue import Queue, Empty
import threading
import time
import traceback
from .paths import configure_tk_libraries
configure_tk_libraries()
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog

from .datamanager import DataManager, PropertiesDialog
from .app_data_views import IntegratedDataManager as DataManager
from .document import Document
from .events_editor import EventsEditor
from .output import OutputPanel, clean_text
from .paths import data_root, results_root, configure_tk_libraries
from .plots import SignalBuffer, VariablesDialog, validate_plot_specs
from .app_plots import EngineeringPlotPage as PlotPage
from .results_tables import ResultsTable
from .worker import SimulationManager
from .panels.registry import PanelRegistry
from .panel_context import PanelContext, cancel_panel_timers
from .presentation import default_plot, element_label, station
from .history import discover_runs, read_signals
from .bus import bus
from .theme import theme
from .i18n import tr, language, set_language, retranslate_tree, translate_text
from .settings import Settings
from .windows import WindowManager
from .app_shell import MenuRegistry


@lru_cache(maxsize=24)
def _domain_reference(root, name):
    from power_machine.study import reference_study
    return reference_study(root, name)


def validity_messages(study, root=None, index=None):
    """Diferencias de configuración frente a PF, sin confundir avisos de uso."""
    return [tr('shell.no_powerfactory_comparison_for_the_current_configuration')]


def frequency_messages(study):
    if any(e.kind == 'short_circuit' for e in study.events) and any('|m:fehz|' in k for k in study.monitored):
        return [tr('shell.frequency_during_the_first_10_ms_after')]
    return []


def validate_options(start, end, dt_ms, every, tolerance, max_iter):
    start, end, dt_ms, tolerance = map(float, (start, end, dt_ms, tolerance))
    every, max_iter = int(every), int(max_iter)
    if not all(math.isfinite(v) for v in (start, end, dt_ms, tolerance)):
        raise ValueError(tr('shell.numeric_options_must_be_finite'))
    if end <= start or dt_ms <= 0 or tolerance <= 0 or every < 1 or max_iter < 1:
        raise ValueError(tr('shell.check_the_interval_step_tolerance_and_positive'))
    return dict(t_start_s=start, t_end_s=end, dt_s=dt_ms / 1000, output_every=every), dict(tolerance_pu=tolerance, max_iter=max_iter)


def loadflow_status(result):
    """Presenta el criterio del motor por islas sin modificar el resultado."""
    from power_machine.lf_status import energized_status
    status = energized_status(result)
    if status['ok']:
        message = tr('shell.lf.converged_islands', n=status['n_solved'])
        if status['n_deenergized']:
            message += tr('shell.lf.deenergized_islands', n=status['n_deenergized'])
        unserved = sum(len(v) for v in status['deenergized_injections'].values())
        if unserved:
            message += tr('shell.lf.unsupplied_injections', n=unserved)
    elif status['n_failed']:
        message = tr('shell.lf.failed_islands', n=status['n_failed'])
    else:
        message = tr('shell.lf.no_reference')
    return status['ok'], message


from .benchmark_actions import BenchmarkActions, benchmark_scene, benchmark_study, network_model
from .coordination import PanelCoordination
from .app_views import ViewActions
from .app_study import StudyActions
from .app_workspace import WorkspaceActions
from .app_compare import CompareActions


class Application(BenchmarkActions, PanelCoordination, ViewActions, StudyActions, WorkspaceActions, CompareActions, tk.Tk):
    def __init__(self, root=None, *, selftest=False, case=None):
        from .identity import initialize_windows_identity, register_window_icons
        initialize_windows_identity()
        configure_tk_libraries()
        super().__init__()
        register_window_icons(self)
        try:
            self.bus = bus
            bus.attach(self)
            self.settings = Settings()
            if self.settings.get('language') in ('en', 'es'):
                set_language(self.settings.get('language'))
            theme.set(self.settings.get('theme', 'light') if self.settings.get('theme') in ('light','dark') else 'light')
            self.windows = WindowManager(self, bus, self.settings)
            self._shell_tokens = [
                bus.subscribe('worker.progress', self._worker_progress),
                bus.subscribe('worker.cancelled', self._worker_cancelled),
                bus.subscribe('selection.changed', self._selection_from_bus),
                bus.subscribe('element.open', self._open_from_bus),
                bus.subscribe('study.event.requested', self._event_from_bus),
                bus.subscribe('theme.changed', self._theme_changed),
                bus.subscribe('language.changed', self._language_changed),
            ]
            self.bind('<Destroy>', self._shell_destroyed, add='+')
            self.root_path = results_root()
            self.initial_case = case or 'ieee39'
            self.selftest = selftest
            self.exit_code = 0
            self.document = None
            self.api = None
            self.model, self.index, self.operational = {}, {}, {}
            self.base_model = {}
            self.model_path = None
            self.result = None
            self.catalog = []
            self.diagrams, self.plot_pages, self.table_pages = {}, [], []
            self.buffer = SignalBuffer()
            self.reference = None
            self.reference_token = 0
            self.tasks = Queue()
            self.busy = False
            self.closing = False
            self.selected_id = None
            self.panel_registry = PanelRegistry()
            self.parity_panels = {}
            self.parity_results = {}
            self._closed = False
            self._selftest_report = {'passed': False, 'root': str(self.root_path)}
            self._heartbeat = 0
            self._last_tick = time.monotonic()
            self._max_tick_gap = 0.0
            self._selftest_deadline = time.monotonic() + 300
            self.geometry('1400x850')
            self.minsize(1000, 600)
            self.update_idletasks()
            if self.winfo_screenwidth() < 1400 or self.winfo_screenheight() < 900:
                try:
                    self.state('zoomed')
                except tk.TclError:
                    pass
            else:
                self.geometry(f'1400x850+{(self.winfo_screenwidth()-1400)//2}+{(self.winfo_screenheight()-850)//2}')
            self.title('Power Machine')
            self.configure(background=theme.color('bg'))
            self.style_ui()
            self.build_ui()
            for module, error in self.panel_registry.errors.items():
                self.output.log("ERROR", tr('shell.panel_unavailable', value0=module, value1=error))
            self.manager = SimulationManager(self, self.worker_message)
            self.protocol('WM_DELETE_WINDOW', self.close)
            self.bind_shortcuts()
            self.load_backend()
            self.update_idletasks()
            self.restore_window()
            theme.apply(self)
            self.after(150, self.initial_panes)
            self.after(100, self.poll_tasks)
            self.after(250, self.refresh_plots)
            if selftest:
                self.after(600, self.begin_selftest)
        except BaseException:
            self.destroy()
            raise

    def destroy(self):
        self._closed = True
        for token in getattr(self,'_shell_tokens',()):
            bus.unsubscribe(token)
        self._shell_tokens = []
        if bus.root is self: bus.close()
        theme._roots.discard(self)
        super().destroy()

    def remember_file(self, path):
        recent = self.settings.get('recent_files', [])
        value = str(Path(path).resolve())
        self.settings.set('recent_files', [value] + [p for p in recent if p != value][:9])

    def style_ui(self):
        theme.apply(self)

    def _shell_destroyed(self, event):
        if event.widget is self:
            for token in self._shell_tokens:
                self.bus.unsubscribe(token)
            self.bus.close()

    def _worker_progress(self, kind, completed=0, total=None, cancelling=False, terminal=None, **payload):
        if not hasattr(self,'progress'): return
        from .analysis_workspace import MODULE_STUDIES
        if completed==0 and not terminal:
            self._active_worker_study=MODULE_STUDIES.get(kind,getattr(self.analysis,'active_study','flow'))
        if total and completed is not None:
            self.progress.stop()
            self.progress.configure(mode='determinate',value=max(0,min(100,100*float(completed)/float(total))))
        if terminal=='error' and hasattr(self,'analysis'):
            self.analysis.study_states[getattr(self,'_active_worker_study',self.analysis.active_study)]='error'
            self._failed_worker_study=getattr(self,'_active_worker_study',self.analysis.active_study)
            self.analysis._render_states()
        if terminal:
            self.progress.stop()
            self.state_text.set(tr('integration.worker_finished',kind=kind))
        else:
            self.state_text.set(tr('integration.worker_progress',kind=kind,completed=completed or 0,total=total or '?'))
        if cancelling: self._worker_cancelled(kind,not bool(terminal))

    def _worker_cancelled(self, kind, requested=True, **payload):
        self.state_text.set(tr('integration.worker_cancelling' if requested else 'integration.worker_cancelled',kind=kind))
        if not requested and hasattr(self,'analysis'):
            from .analysis_workspace import MODULE_STUDIES
            key=MODULE_STUDIES.get(kind,kind)
            if key in self.analysis.study_states:
                self.analysis.study_states[key]='cancelled'
                self.analysis._render_states()

    def _selection_from_bus(self, ids, source=None):
        if source != 'main':
            self.select(ids[0] if ids else None, publish=False)

    def _open_from_bus(self, id):
        self.lift()
        self.focus_force()
        self.properties(id)

    def _event_from_bus(self, kind, target_id):
        self.lift()
        if kind == 'disconnect':
            self.context_action(target_id, 'switch_open')
        else:
            self.events(dict(kind='short_circuit' if kind == 'fault' else kind, target_id=target_id))

    def _theme_changed(self, name):
        self.settings.set('theme', name)
        if hasattr(self, 'theme_var'): self.theme_var.set(name)
        if hasattr(self, 'toolbar'):
            from .app_shell import refresh_toolbar_icons
            refresh_toolbar_icons(self)
            theme.apply(self)

    def _language_changed(self, code):
        if getattr(self,"_closed",False): return
        self.settings.set('language', code)
        if hasattr(self, 'language_var'): self.language_var.set(code)
        if not hasattr(self, 'menu'):
            return
        retranslate_tree(self)
        for panel_id, (frame, context) in self.parity_panels.items():
            context.title = self.panel_registry.entries[panel_id].display_title()
            self.book.tab(frame, text=context.title)
        for name in ('state_text','lf_state_text','study_state_text','validity_text'):
            variable = getattr(self, name, None)
            if variable is not None:
                variable.set(translate_text(variable.get()))
        for ident, (menu, index, title, module) in self.calculation_items.items():
            self.calculation_items[ident] = (menu, index, translate_text(title), module)
        self._capability_signature = None
        self.refresh_calculation_capabilities()
        if self.document:
            self.update_title()
        labels = dict(buses='shell.busbars', branches='shell.branches', gens='shell.generators')
        for page in self.table_pages:
            suffix = tr('shell.stale') if self.result is None else ''
            self.book.tab(page, text='LF · '+tr(labels[page.kind])+suffix)
        if not getattr(self,'selected_id',None) and hasattr(self,'selection_text'):
            self.selection_text.set(tr('tools.select_element'))
        if self.result:
            self.lf_state_text.set('LF · '+loadflow_status(self.result)[1])
        if hasattr(self.book, 'sync'):
            self.book.signature = None
            self.book.sync()
        self.refresh_tab_navigation()

    def initial_panes(self):
        if self.vertical.winfo_height() < 100:
            self.after(100, self.initial_panes)
            return
        self.horizontal.sashpos(0, min(380, int(self.winfo_width()*.30)))
        self.reset_output_height()
        self.restore_dividers()
        if len(self.horizontal.panes())==3:
            self.horizontal.sashpos(1,max(500,self.winfo_width()-365))

    def build_ui(self):
        self.status = ttk.Frame(self)
        self.status.pack(side='bottom', fill='x')
        self.menu = tk.Menu(self, tearoff=False)
        self.configure(menu=self.menu)
        self.menus = MenuRegistry()
        for key in ('file','edit','view','calculation','rms','plots','tools','window','help'):
            menu = tk.Menu(self.menu, tearoff=False)
            self.menu.add_cascade(label=tr('menu.'+key), menu=menu)
            self.menus[key] = menu
        file = self.menus['file']
        file.add_command(label=tr('shell.open_ieee_39_network'), command=self.open_ieee39)
        file.add_command(label=tr('shell.open_model_json'), command=self.choose_benchmark)
        file.add_command(label=tr('shell.import_powerfactory_csv_folder'), command=self.import_pf)
        file.add_command(label=tr('shell.save_network'), command=self.save_network)
        file.add_command(label=tr('shell.new_study_document'), command=self.new_case, accelerator='Ctrl+N')
        file.add_command(label=tr('shell.open_study_document'), command=self.open_case, accelerator='Ctrl+O')
        file.add_command(label=tr('shell.save_study_document'), command=self.save_case, accelerator='Ctrl+S')
        file.add_command(label=tr('shell.save_document_as'), command=lambda: self.save_case(True))
        self.references_menu = tk.Menu(file, tearoff=False)

        file.add_separator()
        file.add_command(label=tr('shell.exit'), command=self.close)
        self.menus['edit'].add_command(label=tr('shell.undo'), command=self.undo, accelerator='Ctrl+Z')
        self.menus['edit'].add_command(label=tr('shell.redo'), command=self.redo, accelerator='Ctrl+Y')
        self.menus['edit'].add_command(label=tr('shell.element_properties'), command=lambda: self.properties(self.selected_id))
        self.menus['calculation'].add_command(label=tr('shell.load_flow'), command=self.run_loadflow)
        self.menus['calculation'].add_command(label=tr('shell.initial_conditions'), command=self.initial_conditions, accelerator='F11')
        self.menus['calculation'].add_command(label=tr('shell.calculation_options'), command=self.options_dialog)
        self.install_calculation_menu()
        self.menus['rms'].add_command(label=tr('shell.compare_native_ieee_39_rms_with_powerfactory'),command=self.open_native_comparison)
        self.menus['rms'].add_command(label=tr('shell.start_simulation'), command=self.simulation_dialog, accelerator='F12')
        self.menus['rms'].add_command(label=tr('shell.ieee_39_example_three_phase_fault_with'), command=self.rms_fault_demo)
        self.menus['rms'].add_command(label=tr('shell.stop'), command=self.stop, accelerator='Esc')
        self.menus['rms'].add_command(label=tr('shell.event_editor'), command=self.events)
        self.menus['rms'].add_command(label=tr('shell.simulation_options'), command=self.simulation_dialog)
        self.menus['plots'].add_command(label=tr('shell.new_page'), command=self.new_plot)
        self.menus['plots'].add_command(label=tr('shell.define_variables'), command=self.define_variables)
        self.menus['plots'].add_command(label=tr('shell.remove_curve_0c10ab'), command=self.remove_curve)
        self.show_boxes = tk.BooleanVar(value=True)
        self.menus['view'].add_checkbutton(label=tr('shell.results_on_diagram'), variable=self.show_boxes, command=self.toggle_boxes)
        self.menus['view'].add_command(label=tr('shell.fit_diagram_to_window'), command=self.fit_diagram)
        self.menus['view'].add_command(label=tr('shell.export_diagram_as_postscript'), command=self.export_diagram)
        self.menus['view'].add_command(label=tr('shell.load_flow_tables'), command=self.show_results)
        self.menus['view'].add_command(label=tr('shell.close_current_tab'), command=self.close_tab)
        self.menus['help'].add_command(label=tr('shell.model_notices'), command=lambda: self.output.show_model_warnings())
        self.menus['help'].add_command(label=tr('shell.validity_domain'), command=self.show_validity)
        self.menus['help'].add_command(label=tr('shell.about_power_machine'), command=self.show_about)
        self.menus['help'].add_command(label=tr('shell.validation'),command=self.show_validation)
        self.menus['help'].add_command(label=tr('shell.manual'), command=self.open_manual)
        from .start_page import reopen_start_page
        self.menus['help'].add_command(label=tr('v3.start.title'),command=lambda:reopen_start_page(self))
        self.menus['help'].add_command(label=tr('v3.int.shortcuts'),command=lambda:messagebox.showinfo(tr('v3.int.shortcuts'),tr('v3.int.shortcut_help'),parent=self))
        from .app_shell import build_workspace
        build_workspace(self)
        view = self.menus['view']
        view.add_separator()
        view.add_command(label=tr('menu.view.diagram'), command=self.open_diagram, accelerator='F9')
        view.add_command(label=tr('v3.expand_workspace'),command=lambda:self.windows.ensure_network().workspace.focus_button.invoke())
        self.diagram_docked = tk.BooleanVar(value=self.windows.docked)
        view.add_checkbutton(label=tr('menu.view.dock'), variable=self.diagram_docked,
                             command=lambda:self.windows.dock_diagram(self.diagram_docked.get()))
        self.theme_var = tk.StringVar(value=theme.name)
        self.language_var = tk.StringVar(value=language())
        self.theme_menu = tk.Menu(view, tearoff=False)
        self.language_menu = tk.Menu(view, tearoff=False)
        view.add_cascade(label=tr('menu.view.theme'), menu=self.theme_menu)
        view.add_cascade(label=tr('menu.view.language'), menu=self.language_menu)
        for name in ('light','dark'):
            self.theme_menu.add_radiobutton(label=tr('theme.'+name), value=name, variable=self.theme_var,
                                            command=lambda name=name:theme.set(name))
        for code, label in (('en','English'),('es','Español')):
            self.language_menu.add_radiobutton(label=label, value=code, variable=self.language_var,
                                               command=lambda code=code:set_language(code))
        self.connect_view_menus()
        self.connect_study_menus()
        self.connect_presentation_menus()
        from .batch_panel import batch_available, BatchWindow
        if batch_available():
            self.menus['calculation'].add_command(label=tr('shell.batch_studies'),command=lambda:BatchWindow(self))
        self.menus['calculation'].add_command(label=tr('shell.compare_cases'),command=self.compare_cases_dialog)
        from .chrome import MenuBar
        self.configure(menu='')
        self.menu_bar=MenuBar(self,self.menus)
        self.menu_bar.pack(fill='x',after=self.brand)

    def bind_shortcuts(self):
        from .shortcuts import install_study_shortcuts
        install_study_shortcuts(self)
        if hasattr(self,'_study_shortcut_hint'):self._study_shortcut_hint.pack_forget()
        self.bind_all('<F5>',lambda event:self.analysis.run_active() if event.widget.winfo_toplevel() in (self,self.windows.window) else None)
        self.bind('<Control-l>', lambda event: self.toggle_output())
        self.bind('<Control-Tab>', lambda event: self.next_page(1))
        self.bind('<Control-Shift-Tab>', lambda event: self.next_page(-1))

    def load_backend(self):
        try:
            self.api = importlib.import_module('power_machine.study')
            from .case_io import load_case
            network = load_case(self.initial_case)
            self.model = network_model(network)
            self.model_path = self.case_path(self.initial_case)
            self.base_model = deepcopy(self.model)
            self.index = self.api.element_index(self.model)
            self.output.index = self.index
            self.catalog = self.api.signal_catalog(self.root_path, model=self.model)
            self.data_manager = DataManager(self.data_host, self.model, self.api.substation_of, self.properties, self.select, self.open_node)
            self.data_manager.pack(fill='both', expand=True)
            from .app_data_views import compact_data_manager
            compact_data_manager(self,self.data_manager)
            self.install_document(Document(benchmark_study(self.api, self.model, self.model_path, self.root_path)))
            self.output.log('INFO', tr('shell.model_loaded_elements_with_pf_id', value0=len(self.index)))
        except Exception:
            self.output.log('ERROR', traceback.format_exc())
            self.state_text.set(tr('shell.study_engine_unavailable'))
            self.placeholder(tr('shell.unable_to_load_the_study_layer_see'))
            for button in self.action_buttons:
                button.configure(state='disabled')

    def placeholder(self, text):
        frame = ttk.Frame(self.book, padding=32)
        ttk.Label(frame, text=tr('shell.network_diagram_7e5f6f'), style='Section.TLabel').pack(anchor='w')
        ttk.Label(frame, text=text, style='Muted.TLabel').pack(anchor='w', pady=20)
        self.book.add(frame, text=tr('shell.network_diagram'))

    def install_document(self, document):
        generated_defaults = document.path is None and not document.modified
        self.close_parity_panels()
        self.parity_results.clear()
        requested_path = document.study.ldf_options.get('gui_model_path')
        if not requested_path and document._base is not None:
            self.base_model = deepcopy(document._base)
            self.model_path = None
        target = (self.root_path / requested_path).resolve() if requested_path else self.model_path
        if target != self.model_path:
            from .case_io import load_case
            self.base_model = network_model(load_case(target or 'ieee39'))
        self.model_path = target
        document.restore_network(self.base_model)
        self.model = document.network.model if document.network.state is not None else deepcopy(self.base_model)
        self.index = self.api.element_index(self.model)
        self.output.index = self.index
        self.catalog = self.api.signal_catalog(self.root_path, model=self.model)
        if not document.study.plots:
            document.study.plots = [default_plot(document.study, self.catalog, self.index)]
            document.study.plots[0]['frequency_base_hz'] = self.model.get('meta', {}).get('f_nom_hz')
            document._saved = document.snapshot()
        self.document = document
        if document.path:
            self.remember_file(document.path)
        self._panel_signature = None
        self.lf_state_text.set(tr('shell.lf_pending'))
        self.result = None
        self.buffer.reset([])
        for view in list(self.diagrams.values()):
            view.destroy()
        for tab in self.book.tabs():
            self.book.nametowidget(tab).destroy()
        self.windows._dock_book = None
        self.diagrams.clear()
        self.plot_pages.clear()
        self.table_pages.clear()
        self.reference = None
        self.update_document(invalidate=False)
        self.analysis.select_study('flow',open_panel=False)
        self.open_diagram()
        self.rebuild_plots()
        self.load_reference()
        from .app_shell import show_start_page
        show_start_page(self)
        self.time_text.set('')
        self.step_text.set('')
        self.progress.configure(value=0)
        self._initial_document_state=(document,document.revision,document.snapshot()) if generated_defaults else None
        self.refresh_calculation_capabilities()

    def update_document(self, invalidate=True):
        if not self.document:
            return
        self.invalidate_panel_results()
        from power_machine.loadflow import _scenario
        self.operational = _scenario(self.model, self.document.study.scenario)
        self.data_manager.model = self.operational
        name = self.display_study_name() + (' *' if self.document.modified else '')
        self.data_manager.case_name = name
        self.data_manager.events = self.document.study.events
        self.data_manager.index = self.index
        self.data_manager.runs = discover_runs(results_root(self.root_path))
        if self.data_manager.document is not self.document:
            self.data_manager.set_document(self.document)
            self.restore_data_table_presentation(self.data_manager)
        self.data_manager.set_result(self.result)
        self.data_manager.populate()
        self.data_manager.tree.delete('active-case') if self.data_manager.tree.exists('active-case') else None
        for case in self.document.to_data()['cases']:
            self.data_manager.tree.insert('studies','end',iid='case:'+case['case_id'],text=name if case['case_id']==self.document.active_case_id else case['name'])
        self.case_label.set(name)
        identity=self.study_identity()
        original=clean_text(self.document.study.name)
        if identity.startswith(original+' · '):
            identity=self.display_study_name()+identity[len(original):]
        self.case_label.set(self.display_study_name())
        if hasattr(self,'analysis'):self.analysis.refresh_document()
        self.title(tr('v3.window.analysis')+' · '+self.case_label.get()+' · Power Machine')
        if invalidate:
            self.bus.publish('network.edited', ids=list(self.index))
            self.bus.publish('results.stale', reason=tr('shell.document_modified'))
            self.result = None
            self.lf_state_text.set(tr('shell.lf_stale'))
            self.state_text.set(tr('shell.document_modified'))
            self.buffer.reset([])
            self.data_manager.set_result(None)
            for table in self.table_pages:
                self.book.tab(table,text=self.book.tab(table,'text').split(tr('shell.stale'))[0]+tr('shell.stale'))
        for diagram in self.diagrams.values():
            diagram.set_operating_state(self.operational)
            if invalidate:
                diagram.set_loadflow_results(None)
        notices = validity_messages(self.document.study, self.root_path, self.index)
        self.validity_text.set(tr('shell.no_pf_comparison_for_the_current_configuration'))
        self.validity_label.configure(style='Warning.TLabel' if notices else 'Status.TLabel')
        self.refresh_data_windows()
        self.refresh_calculation_capabilities()

    def available(self):
        from .app_jobs import document_gate
        if self.busy or self.manager.running or (self.document and document_gate(self.document).running):
            self.output.log('AVISO', tr('shell.wait_for_the_active_calculation_to_finish'))
            return False
        return self.document is not None

    def maybe_save(self):
        if not self.document or not self.document.modified:
            return True
        initial=getattr(self,'_initial_document_state',None)
        if initial and initial[0] is self.document and initial[1]==self.document.revision and initial[2]==self.document.snapshot():
            return True
        answer = messagebox.askyesnocancel(tr('shell.unsaved_study'), tr('shell.the_study_has_changes_save_them'), parent=self)
        if answer is None:
            return False
        return self.save_case() if answer else True

    def new_case(self):
        if self.available() and self.maybe_save():
            case = (benchmark_study(self.api, self.base_model, self.model_path, self.root_path)
                    if self.model_path else benchmark_study(self.api, self.base_model, None, self.root_path))
            self.install_document(Document(case))

    def reference_case(self, case):
        if not self.available() or not self.maybe_save():
            return
        try:
            self.install_document(Document(self.api.reference_study(self.root_path, case)))
            self.output.log('INFO', tr('shell.pf_template_loaded') + case)
        except Exception as exc:
            self.show_error(exc)

    def open_case(self):
        if not self.available() or not self.maybe_save():
            return
        path = filedialog.askopenfilename(parent=self, initialdir=self.root_path, filetypes=[(tr('shell.power_machine_study'), '*.pmstudy')])
        if path:
            try:
                candidate = Document.open(path, self.api.StudyCase)
                from .case_io import load_case
                model = candidate._base or network_model(load_case(candidate.study.ldf_options.get('gui_model_path') or 'ieee39'))
                candidate.restore_network(model)
                active_model = candidate.network.model if candidate.network.state is not None else model
                catalog = self.api.signal_catalog(self.root_path, model=active_model)
                validate_plot_specs(candidate.study.plots, {r['key'] for r in catalog})
                messages = self.api.validate_study(self.root_path, candidate.study, model=active_model)
                errors = [x['message'] for x in messages if x['level'] == 'error']
                if errors:
                    raise ValueError('\n'.join(errors))
                self.install_document(candidate)
            except Exception as exc:
                self.show_error(exc)

    def save_case(self, save_as=False):
        if not self.document:
            return False
        path = self.document.path
        if save_as or path is None or getattr(self.document,'_v1_path',None):
            path = filedialog.asksaveasfilename(parent=self, initialdir=self.root_path, initialfile=self.document.study.name + '.pmstudy',
                                              defaultextension='.pmstudy', filetypes=[(tr('shell.power_machine_study'), '*.pmstudy')])
        if not path:
            return False
        try:
            self.capture_presentation()
            self.document.save(path)
            self.remember_file(path)
            self.update_document(invalidate=False)
            self.output.log('INFO', tr('shell.study_saved_in', value0=path))
            return True
        except Exception as exc:
            self.show_error(exc)
            return False

    def undo(self):
        self.history_step()

    def redo(self):
        self.history_step(redo=True)

    def history_step(self, *, redo=False):
        if not self.available():
            return
        item = self.parity_panels.get('network_editor')
        tools_active = getattr(self,'_tools_network_revision',None)==(id(self.document),self.document.revision)
        network_active = (item is not None and str(item[0]) == str(self.book.select())) or tools_active
        try:
            changed = (self.document.network.step(redo) if network_active else
                       self.document.redo() if redo else self.document.undo())
        except ValueError as exc:
            self.output.log('AVISO', str(exc))
            return
        if changed and tools_active:
            self.network_changed()
            self._tools_network_revision=(id(self.document),self.document.revision)
            if self.selected_id in self.index and hasattr(self,'properties_dock'):
                self.properties_dock.show_element(self.selected_id)
        if changed and not network_active:
            self.update_document()
            self.rebuild_plots()
        if changed:
            self.output.log('INFO', tr('shell.change_redone') if redo else tr('shell.change_undone'))

    def network_changed(self):
        self._tools_network_revision=(id(self.document),self.document.revision)
        self.model = self.document.network.model
        self.index = self.api.element_index(self.model)
        self.output.index = self.index
        self.catalog = self.api.signal_catalog(self.root_path, model=self.model)
        self.update_document()

    def open_parity_panel(self, panel_id):
        return PanelCoordination.open_parity_panel(self, panel_id, context_factory=PanelContext)

    def open_diagram(self, group=None):
        requested_group = group
        host = self.windows.show_diagram()
        if group in self.diagrams:
            host.select(self.diagrams[group])
            return
        try:
            from .diagram import DiagramWidget, diagram_groups
        except ImportError:
            self.placeholder(tr('shell.the_diagram_module_is_not_yet_available'))
            return
        from power_machine.netedit.schema import model_sha256
        layout_path = results_root(self.root_path) / (model_sha256(self.model) + '_layout.json')
        if group is None:
            groups = diagram_groups(self.model)
            if len(groups) == 1:
                group = groups[0]['id']
        view = DiagramWidget(host, self, self.bus, group=group)
        names = {row['id']: row['name'] for row in diagram_groups(self.model)}
        host.add(view, text=tr('shell.ieee_39_single_line_diagram') if self.model.get('meta',{}).get('benchmark')=='ieee39' else names.get(group,tr('integration.single_line_diagram')))
        self.diagrams[requested_group] = view
        host.select(view)
        self.update_idletasks()
        self.restore_diagram_presentation(view,requested_group)
        view.set_result_boxes_visible(self.show_boxes.get())
        if self.result:
            view.set_loadflow_results(self.result)
        host.select(view)

    def select(self, ident, *, publish=True):
        if self.selected_id==ident and ident is not None:return
        self.selected_id = ident
        if ident and hasattr(self,'properties_dock'):
            self.properties_dock.show_element(ident)
        if not ident and hasattr(self,'properties_dock'):self.properties_dock.show_element(None)
        if hasattr(self,'selection_text'):
            self.selection_text.set(element_label(self.index[ident],self.index) if ident in self.index else tr('v3.no_selection'))
        if publish:
            self.bus.publish('selection.changed', ids=[ident] if ident else [], source='main')
        if hasattr(self,'data_manager'):
            if ident is None:
                tree=self.data_manager.tree
                tree.selection_remove(*tree.selection())
                tree.focus('')
            self.data_manager.locate(ident)
        for window,manager in getattr(self,'data_windows',[]):
            if window.winfo_exists():manager.locate(ident)
        for view in self.diagrams.values():
            view.highlight(ident)

    def properties(self, ident):
        if not ident or not self.document:
            return
        if ident not in self.index:
            try:
                from .diagram import diagram_groups
                if ident in {row['id'] for row in diagram_groups(self.model)}:
                    self.open_diagram(ident)
            except ImportError:
                pass
            return
        try:
            from .element_dialog import open_element_dialog
            from .properties_dock import editor_controller
            return open_element_dialog(self,editor_controller(self),ident)
        except ImportError:
            pass
        PropertiesDialog(self, self.index[ident], self.document, self.result, self.update_document,
                         editable=not self.busy and not self.manager.running)

    def context_action(self, ident, action):
        if action == 'properties':
            self.properties(ident)
        elif action == 'locate':
            self.data_manager.locate(ident)
        elif action == 'add_to_plot':
            self.define_variables(ident)
        elif action in ('short_circuit', 'load_step', 'gen_trip', 'switch_open', 'switch_close'):
            if action == 'gen_trip':
                self.output.log('AVISO', tr('shell.generator_disconnection_is_unavailable_in_the_engine'))
                return
            kind = action
            if action.startswith('switch_'):
                item = self.index[ident]
                kind = 'switch' if item.get('class') in ('StaSwitch', 'ElmCoup') else 'switch_element'
            self.events(dict(kind=kind, target_id=ident, action='close' if action == 'switch_close' else 'open'))

    def open_node(self, node):
        if node.startswith(('case:', 'scenario:', 'variant:', 'stage:')):
            kind,ident=node.split(':',1)
            if not self.available():return
            try:
                doc=self.document
                if kind=='case':self.activate_study(ident)
                else:
                    changes={kind+'_id':ident}
                    if kind=='variant':changes['stage_id']=None
                    if kind=='stage':
                        changes['variant_id']=next(v['variant_id'] for v in doc.to_data()['network_variants'] if any(s['stage_id']==ident for s in v['stages']))
                    doc.configure_case(doc.active_case_id,doc.revision,**changes)
                    self.activate_study()
            except Exception as exc:self.show_error(exc)
        elif node.startswith('event:'):
            self.events(event_index=int(node.split(':')[1]))
        elif node.startswith('run:'):
            self.open_run(self.data_manager.runs[int(node.split(':')[1])]['path'])
        elif node == 'events':
            self.events()
        elif node in ('results', 'results-compare'):
            self.choose_runs()
        elif node == 'active-case':
            self.options_dialog()

    def events(self, preset=None, event_index=None):
        if self.available():
            if hasattr(self,'analysis'):
                return self.analysis.show_events(preset,event_index)
            powered = {row['id']: row['energized'] for row in (self.result or {}).get('buses', [])}
            index = self.api.element_index(self.operational)
            dialog = EventsEditor(self, self.document, self.api, self.root_path, self.model, index,
                                  self.update_document, energized=powered, preset=preset)
            if event_index is not None:
                dialog.tree.selection_set(str(event_index))
                dialog.load_selection()
            if preset and 'action' in dialog.params:
                dialog.params['action'].set(tr('integration.close') if preset.get('action') == 'close' else tr('integration.open'))

    def options_dialog(self):
        if not self.available():
            return
        window = tk.Toplevel(self)
        window.title(tr('shell.study_case_options'))
        window.transient(self)
        window.grab_set()
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill='both', expand=True)
        study = self.document.study
        fields = {}
        for n, (key, label, value) in enumerate([
            ('name', tr('shell.study_name'), study.name), ('start', tr('shell.start_time_s'), study.options.t_start_s),
            ('end', tr('shell.end_time_s'), study.options.t_end_s), ('dt', tr('shell.integration_step_ms'), study.options.dt_s * 1000),
            ('every', tr('shell.save_every_n_steps'), study.options.output_every),
            ('tolerance', tr('shell.lf_tolerance_p_u'), study.ldf_options.get('tolerance_pu', 1e-8)),
            ('max_iter', tr('shell.maximum_lf_iterations'), study.ldf_options.get('max_iter', 30)),
        ]):
            ttk.Label(frame, text=label).grid(row=n, column=0, sticky='w', padx=(0, 18), pady=7)
            fields[key] = tk.StringVar(value=str(value))
            ttk.Entry(frame, textvariable=fields[key], width=30).grid(row=n, column=1, sticky='ew')
        q_limits = tk.BooleanVar(value=study.ldf_options.get('q_limits', True))
        ttk.Checkbutton(frame, text=tr('shell.enforce_reactive_power_limits'), variable=q_limits).grid(row=7, column=0, columnspan=2, sticky='w', pady=10)
        def apply():
            try:
                sim, ldf = validate_options(*(fields[k].get() for k in ('start', 'end', 'dt', 'every', 'tolerance', 'max_iter')))
                if not fields['name'].get().strip():
                    raise ValueError(tr('shell.the_name_cannot_be_empty'))
                candidate = self.api.StudyCase.from_json(self.document.snapshot())
                candidate.options = self.api.SimOptions(**sim)
                candidate.ldf_options.update(ldf, q_limits=q_limits.get())
                candidate.name = fields['name'].get().strip()
                errors = [m['message'] for m in self.api.validate_study(self.root_path, candidate, model=self.model) if m['level'] == 'error']
                if errors:
                    raise ValueError('\n'.join(errors))
                def change(case):
                    case.options, case.ldf_options, case.name = candidate.options, candidate.ldf_options, candidate.name
                self.document.edit(change)
                self.update_document()
                window.destroy()
            except (ValueError, TypeError) as exc:
                messagebox.showerror(tr('shell.invalid_options'), clean_text(exc), parent=window)
        ttk.Button(frame, text=tr('shell.apply'), command=apply).grid(row=8, column=1, sticky='e', pady=(10, 0))

    def simulation_dialog(self):
        if not self.available():
            return
        window = tk.Toplevel(self)
        window.title(tr('shell.rms_simulation'))
        window.transient(self)
        window.grab_set()
        frame = ttk.Frame(window, padding=20)
        frame.pack(fill='both', expand=True)
        case = self.document.study
        fields = {}
        for n, (key, label, value) in enumerate([
            ('end', tr('shell.end_time_s'), case.options.t_end_s),
            ('dt', tr('shell.integration_step_ms'), case.options.dt_s * 1000),
            ('every', tr('shell.output_decimation_steps'), case.options.output_every),
        ]):
            ttk.Label(frame, text=label).grid(row=n, column=0, sticky='w', padx=(0, 20), pady=8)
            fields[key] = tk.StringVar(value=str(value))
            ttk.Entry(frame, textvariable=fields[key], width=18).grid(row=n, column=1)
        recalculate = tk.BooleanVar(value=case.ldf_options.get('gui_recalculate_ic', True))
        ttk.Checkbutton(frame, text=tr('shell.recalculate_initial_conditions'), variable=recalculate).grid(row=3, column=0, columnspan=2, sticky='w', pady=10)
        ttk.Label(frame, text=tr('shell.initial_conditions_will_be_computed_automatically_when'), wraplength=430).grid(row=4, column=0, columnspan=2, sticky='w')
        def execute():
            try:
                sim, _ = validate_options(case.options.t_start_s, fields['end'].get(), fields['dt'].get(), fields['every'].get(), 1e-8, 30)
                candidate = self.api.StudyCase.from_json(self.document.snapshot())
                candidate.options = self.api.SimOptions(**sim)
                errors = [m['message'] for m in self.api.validate_study(self.root_path, candidate, model=self.model) if m['level'] == 'error']
                if errors:
                    raise ValueError('\n'.join(errors))
                def change(study):
                    study.options = candidate.options
                    study.ldf_options['gui_recalculate_ic'] = recalculate.get()
                self.document.edit(change)
                self.update_document(invalidate=False)
                window.destroy()
                self.start_simulation()
            except (ValueError, TypeError) as exc:
                messagebox.showerror(tr('shell.invalid_options'), clean_text(exc), parent=window)
        window.fields = fields
        window.recalculate = recalculate
        window.execute_button = ttk.Button(frame, text=tr('shell.run'), command=execute)
        window.execute_button.grid(row=5, column=1, sticky='e', pady=(16, 0))
        ttk.Button(frame, text=tr('shell.cancel'), command=window.destroy).grid(row=5, column=0, sticky='w', pady=(16, 0))
        return window

    def choose_runs(self):
        window = tk.Toplevel(self)
        window.title(tr('shell.saved_results'))
        window.geometry('880x450')
        runs = discover_runs(results_root(self.root_path))
        choices = tk.Listbox(window, selectmode='extended', exportselection=False)
        window.choices = choices
        choices.pack(fill='both', expand=True, padx=12, pady=12)
        for run in runs:
            choices.insert('end', run['name'])
        ttk.Label(window, text=tr('shell.select_one_run_to_open_or_two')).pack(padx=12)
        def open_selected():
            selected = choices.curselection()
            if not 1 <= len(selected) <= 2:
                messagebox.showinfo(tr('integration.results'), tr('shell.select_one_or_two_runs'), parent=window)
                return
            self.open_run(runs[selected[0]]['path'], runs[selected[1]]['path'] if len(selected) == 2 else None)
            window.destroy()
        window.open_button = ttk.Button(window, text=tr('shell.open_selection'), command=open_selected)
        window.open_button.pack(pady=12)
        return window

    def open_run(self, path, comparison=None):
        def loaded(data):
            own, other = data
            keys = set(own.keys)
            selected = [dict(c) for c in self.document.study.plots[0]['curves'] if c['key'] in keys] if self.document.study.plots else []
            if not selected:
                units = {}
                for key in own.keys[1:]:
                    unit = key.rsplit('|', 1)[-1]
                    if unit not in units and len(units) >= 6:
                        continue
                    units.setdefault(unit, len(units))
                    selected.append(dict(key=key, axis=units[unit]))
                    if len(selected) >= 8:
                        break
            spec = dict(name=self.document.study.name+' · RMS', axes=max((c['axis'] for c in selected), default=0) + 1, curves=selected)
            page = PlotPage(self.book, spec, own, other,
                            source_label=Path(path).parent.name,
                            reference_label=Path(comparison).parent.name if comparison else 'PF')
            self.configure_plot_page(page)
            self.book.add(page, text='RMS · ' + spec['name'])
            self.book.select(page)
        def read():
            try:
                data = read_signals(path), read_signals(comparison) if comparison else None
                self.tasks.put(('history', loaded, data, None))
            except Exception:
                self.tasks.put(('history', loaded, None, traceback.format_exc()))
        threading.Thread(target=read, daemon=True, name='power_machine-resultados').start()

    def set_busy(self, busy, text=tr('shell.ready'), simulation=False):
        self.busy = busy
        self.state_text.set(text)
        for button in self.action_buttons:
            enabled = getattr(button, 'action_id', None) in (8, 9, 12)
            button.configure(state='disabled' if busy and not enabled else 'normal')
        self.stop_button.configure(state='normal' if simulation and busy else 'disabled')
        if busy and not simulation:
            self.progress.configure(mode='indeterminate')
            self.progress.start(20)
        else:
            self.progress.stop()
            self.progress.configure(mode='determinate')

    def run_loadflow(self):
        if not self.available():
            return
        document, saved = self.document, self.document.snapshot()
        model = deepcopy(self.model)
        case = self.api.StudyCase.from_json(saved)
        from power_machine.study_capabilities import capabilities
        preflight=capabilities(model,saved)['calculations'].get('loadflow',{})
        if not model.get('buses') or not preflight.get('evaluable',True):
            self.output.log('AVISO',tr('shell.load_flow_preflight_busbars_references_or_electrical'))
            return
        self.output.log('INFO', tr('shell.computing_load_flow_with_q_limits') + (tr('shell.enabled') if case.ldf_options.get('q_limits', True) else tr('shell.disabled')))
        def delivered(result):
            if self.document is document and self.model == model and document.snapshot() == saved:
                self.loadflow_done(result)
            else:
                self.output.log('AVISO', tr('shell.lf_result_discarded_because_the_document_or'))
        self.start_task(tr('shell.computing_load_flow'),
                        lambda: self.api.run_loadflow(self.root_path, case, model=model), delivered)

    def loadflow_done(self, result):
        self.result = result
        self.bus.publish('results.updated', study='loadflow', result_id='loadflow')
        self.data_manager.set_result(result)
        self.archive_result('loadflow',result)
        from power_machine.lf_status import energized_status
        summary = energized_status(result)
        converged = summary['ok']
        usable, status = loadflow_status(result)
        self.state_text.set(status)
        self.lf_state_text.set('LF · ' + status)
        self.output.log('INFO' if converged else 'AVISO' if usable else 'ERROR', tr('shell.iterations_maximum_mismatch_p_u', value0=status, value1=result.get('iterations'), value2=result.get('max_mismatch_pu', 0)))
        self.log_island_summary(result)
        for group, identifiers in summary['deenergized_injections'].items():
            for ident in identifiers:
                item = self.index.get(ident, {'id': ident})
                self.output.log('AVISO', tr('shell.equipment_without_supply') + element_label(item, self.index))
        for gen in result.get('gens', []):
            if gen.get('at_q_limit'):
                self.output.log('AVISO', tr('shell.q_limit_mvar', value0=gen['name'], value1=gen.get('q_mvar')))
        for warning in result.get('warnings', []):
            if self.model_path and 'faltan datos dinámicos' in warning:
                dynamic = self.model_path.parent / 'dynamic_data.json'
                warning = (tr('shell.balanced_ac_load_flow_available_classical_dynamics') +
                           (tr('shell.dynamic_data_json_available') if dynamic.is_file() else tr('shell.no_dynamic_data_json_file')) +
                           tr('shell.pf_rms_model_and_zero_sequence_independent'))
            self.output.log('AVISO', warning)
        for view in self.diagrams.values():
            view.set_loadflow_results(result)
        self.show_results(select=False)
        self.refresh_data_windows()
        self.refresh_calculation_capabilities()
        if self.selftest:
            self._selftest_report['loadflow'] = {key: result.get(key) for key in ('converged', 'iterations', 'max_mismatch_pu')}
            self._selftest_report['loadflow']['island_statuses'] = [i.get('status') for i in result.get('islands', [])]
            self._selftest_report['buses'] = len(result.get('buses', []))
            self._selftest_report['branches'] = len(result.get('branches', []))
            if not usable:
                self.finish_selftest(tr('shell.load_flow_did_not_converge'))
            else:
                self.after(300, self.start_simulation)

    def show_results(self, select=True):
        if not self.result:
            self.output.log('AVISO', tr('shell.run_a_load_flow_to_view_the'))
            return
        for page in self.table_pages:
            page.destroy()
        self.table_pages.clear()
        for kind, label in [('buses', tr('shell.busbars')), ('branches', tr('shell.branches')), ('gens', tr('shell.generators'))]:
            rows=deepcopy(self.result.get(kind, []))
            if kind=='branches':
                from power_machine.loading import branch_loading
                loading=branch_loading(self.operational,self.result)
                for row in rows:
                    item=loading.get(row.get('object_id',row.get('id')), {})
                    row['loading_pct']=item.get('loading_pct')
                    row['i_from_ka']=max((end['i_ka'] for end in item.get('ends',{}).values() if end.get('i_ka') is not None),default=None)
            page = ResultsTable(self.book, kind, rows, self.index,select_callback=self.select)
            self.restore_table_presentation(page)
            self.book.add(page, text='LF · ' + label,area='tables')
            self.table_pages.append(page)
        if select:
            self.book.select(self.table_pages[0])

    def initial_conditions(self):
        if not self.available():
            return
        self.set_busy(True, tr('shell.computing_initial_conditions'))
        try:
            model = self.model
            self.manager.start(self.root_path, self.document.snapshot(), prepare=True, model=model)
        except Exception as exc:
            self.set_busy(False, tr('shell.initial_condition_error'))
            self.show_error(exc)

    def show_ic(self, report):
        self.output.log('INFO', tr('shell.initial_conditions_states_nodes_network_residual_p', value0=report.get('n_states'), value1=report.get('n_nodes'), value2=report.get('network_residual_pu'), value3=report.get('max_derivative')))
        self.output.log('INFO', tr('shell.dsl_coverage_of', value0=report.get('dsl_coupled'), value1=report.get('dsl_total')))
        for frozen in report.get('frozen', []):
            self.output.log('AVISO', tr('shell.frozen_model', value0=frozen))
        for warning in report.get('warnings', []):
            self.output.log('AVISO', warning)
        if not report.get('validated_operating_point'):
            self.validity_text.set(tr('shell.operating_point_differs_from_the_validated_point'))
            self.validity_label.configure(style='Warning.TLabel')
        if self.selftest:
            self._selftest_report['initial_conditions'] = report

    def start_simulation(self):
        if not self.available():
            return
        if self.model.get('meta', {}).get('benchmark') == 'ieee39' and (not self.document.study.events or self.document.study.ldf_options.get('gui_classical_event')):
            self.output.log('INFO', tr('shell.classical_ieee_39_rms_parameters_from_dynamic'))
            if self.model_path is None:
                self.model_path = self.case_path('ieee39')
            profile=self.document.study.ldf_options.get('gui_classical_event','BASE')
            if profile!='BASE':
                events=self.document.study.events
                valid=(len(events)==2 and [e.kind for e in events]==['short_circuit','clear_short_circuit'] and
                       all(e.target_id=='B16' for e in events) and abs(events[0].time_s-1.)<1e-9 and abs(events[1].time_s-1.1)<1e-9 and
                       all(float(events[0].params.get(k,0))==0 for k in ('r_ohm','x_ohm')))
                if not valid:
                    self.output.log('AVISO',tr('shell.preflight_the_events_no_longer_match_the'))
                    return
            self.run_benchmark_dynamic(profile)
            return
        from .case_io import dynamic_directory
        from .native_case import is_native_case
        if dynamic_directory(self.model) is None and not is_native_case(self.model, self.document.snapshot()):
            self.output.log('AVISO', tr('shell.rms_preflight_dynamic_models_and_manifest_json'))
            self.state_text.set(tr('shell.rms_awaiting_dynamic_data'))
            self.rms_recovery_dialog()
            return
        for notice in validity_messages(self.document.study, self.root_path, self.index) + frequency_messages(self.document.study):
            self.output.log('AVISO', notice)
        messages = self.api.validate_study(self.root_path, self.document.study, model=self.model)
        for message in messages:
            self.output.log(message['level'], message['message'])
        if any(message['level'] == 'error' for message in messages):
            if self.selftest:
                self.finish_selftest(tr('shell.the_engine_rejected_the_self_test_case'))
            return
        self.buffer.reset([])
        self._progress_decile = 0
        self.progress.configure(value=0)
        if self.plot_pages:
            self.book.select(self.plot_pages[0])
        self.output.log('INFO', tr('shell.rms_simulation_0_preparing_initial_conditions'))
        self.set_busy(True, tr('shell.starting_rms_simulation'), simulation=True)
        self._run_started = time.monotonic()
        try:
            model = self.model
            self.manager.start(self.root_path, self.document.snapshot(), model=model)
        except Exception as exc:
            self.set_busy(False, tr('shell.simulation_error'))
            self.show_error(exc)

    def rms_recovery_dialog(self):
        """Acciones explícitas para aportar datos, sin modificar bases eléctricas."""
        window = tk.Toplevel(self)
        window.title(tr('int2.rms_recovery_title'))
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text=tr('int2.rms_recovery_detail'), wraplength=530).pack(fill='x', pady=8)
        window.open_model_button = ttk.Button(frame, text=tr('shell.open_model_json'),
                                              command=lambda:(window.destroy(), self.choose_benchmark()))
        window.open_model_button.pack(fill='x', pady=4)
        def included():
            from power_machine.resources import readonly_path
            window.destroy()
            self.open_ieee39(readonly_path('cases', 'ieee39_pf', 'model.json'))
        ttk.Button(frame, text=tr('int2.open_native_case'), command=included).pack(fill='x', pady=4)
        ttk.Button(frame, text=tr('study.close'), command=window.destroy).pack(fill='x', pady=4)
        return window

    def worker_message(self, message):
        kind, *payload = message
        if kind == 'log':
            if payload[1] not in (tr('shell.simulation_finished'), tr('shell.simulation_cancelled')):
                self.output.log(*payload)
        elif kind == 'ic':
            self.show_ic(payload[0])
        elif kind == 'prepared':
            self.set_busy(False, tr('shell.initial_conditions_ready'))
        elif kind == 'header':
            self.buffer.reset(payload[0])
            self.refresh_plot_templates()
        elif kind == 'samples':
            self.buffer.append(payload[0])
        elif kind == 'progress':
            t, end, ms = payload
            start = self.document.study.options.t_start_s
            fraction = max(0, min(1, (t - start) / (end - start))) if end > start else 0
            eta = (end - t) / self.document.study.options.dt_s * ms / 1000
            self.progress.configure(value=100 * fraction)
            self.time_text.set(f't = {t:.3f} / {end:.3f} s')
            for decile in range(getattr(self, '_progress_decile', 0) + 1, int(fraction * 100 + 1e-7) // 10 + 1):
                self.output.log('INFO', tr('shell.rms_t_s_ms_step_remaining_s', value0=decile * 10, value1=t, value2=end, value3=ms, value4=max(0, eta)))
                self._progress_decile = decile
            if self.selftest:
                self._selftest_report['progress_visible'] = bool(self.progress.winfo_viewable())
                self._selftest_report['progress_received'] = True
            self.step_text.set(tr('shell.ms_step', value0=ms))
            self.state_text.set(tr('shell.rms_remaining_s', value0=max(0, eta)))
        elif kind == 'done':
            from .display import elapsed_text
            summary = payload[0]
            if summary.get('signals_csv'):
                from .workspace_links import relative_path,file_sha256
                archived=deepcopy(summary)
                archived['status']='error' if summary.get('failure') else 'partial' if summary.get('cancelled') else 'completed'
                path=Path(summary['signals_csv'])
                archived['provenance']={'input_files':[dict(path=relative_path(path),sha256=file_sha256(path),role='signals')]}
                try:self.archive_result('rms',archived)
                except Exception as exc:self.output.log('ERROR',tr('shell.unable_to_link_rms')+str(exc))
            self.data_manager.runs = discover_runs(results_root(self.root_path))
            self.data_manager.set_document(self.document)
            self.data_manager.set_result(self.result)
            self.data_manager.populate()
            failure = summary.get('failure')
            state = tr('shell.simulation_failed') if failure else tr('shell.simulation_cancelled') if summary.get('cancelled') else tr('shell.simulation_completed')
            self.set_busy(False, state)
            self.after(150,self.refresh_tab_navigation)
            self.output.log('ERROR' if failure else 'INFO', tr('shell.steps_computation_time', value0=state, value1=summary.get('steps'), value2=elapsed_text(summary.get('wall_s'))))
            if summary.get('signals_csv'):
                self.output.log('INFO', tr('shell.signals_saved_in', value0=summary['signals_csv']))
            if failure:
                self.output.log('ERROR', failure)
                if not self.selftest:
                    self._failed_worker_study='rms'
                    self.show_error(failure)
            for page in self.plot_pages:
                page.redraw(force=True)
            if summary.get('event') and summary.get('signals_csv'):
                self.open_run(summary['signals_csv'])
            if self.selftest:
                self._selftest_report['simulation'] = summary
                self._selftest_report['samples'] = len(self.buffer.rows)
                self.after(250, lambda: self.finish_selftest(failure or (None if summary.get('completed') and self.buffer.rows else tr('shell.incomplete_rms'))))
        elif kind == 'error':
            self.set_busy(False, tr('shell.simulation_error'))
            self.output.log('ERROR', payload[0])
            if not self.selftest:
                self._failed_worker_study='rms'
                self.show_error(payload[0])
            if self.selftest:
                self.after(250, lambda: self.finish_selftest(payload[0]))

    def stop(self):
        for _, context in self.parity_panels.values():
            context.worker.cancel()
        if self.manager.running:
            self.manager.cancel()
            self.state_text.set(tr('shell.stopping_after_the_current_step'))
            self.output.log('INFO', tr('shell.cancellation_requested_the_partial_classical_run_is')
                            if self.model_path else tr('shell.cancellation_requested_computed_samples_will_be_retained'))

    def load_reference(self):
        self.reference = None

    def rebuild_plots(self):
        for page in self.plot_pages:
            page.destroy()
        self.plot_pages.clear()
        for index, spec in enumerate(self.document.study.plots):
            page = PlotPage(self.book, spec, self.buffer, self.reference,
                            on_reference_toggle=lambda visible, i=index: self.toggle_reference(i, visible))
            self.configure_plot_page(page)
            self.book.add(page, text=spec.get('name', tr('integration.plots')),area='curves')
            self.plot_pages.append(page)
        self.refresh_plot_templates()

    def toggle_reference(self, page, visible):
        self.document.edit(lambda study: study.plots[page].update(show_pf=visible))
        self.update_title()

    def display_study_name(self):
        """Localize the bundled default name for display, preserving the document."""
        name = clean_text(self.document.study.name)
        if self.model.get('meta', {}).get('benchmark') == 'ieee39' and name in (
                'IEEE 39 · caso base', 'IEEE 39 · base case'):
            return tr('shell.ieee39_base_case')
        return name

    def update_title(self):
        name = self.display_study_name() + (' *' if self.document.modified else '')
        self.case_label.set(name)
        identity=self.study_identity()
        original=clean_text(self.document.study.name)
        if identity.startswith(original+' · '):
            identity=self.display_study_name()+identity[len(original):]
        self.case_label.set(self.display_study_name())
        self.title(tr('v3.window.analysis')+' · '+self.case_label.get()+' · Power Machine')
        if hasattr(self,'analysis'):self.analysis.refresh_document()

    def new_plot(self, count=None):
        if not self.document:
            return
        if count is None:
            count = simpledialog.askinteger(tr('shell.new_plot_page'), tr('shell.number_of_subplots_from_1_to_6'), initialvalue=2, minvalue=1, maxvalue=6, parent=self)
        if count is None:
            return
        spec = dict(name=tr('shell.plots', value0=len(self.document.study.plots) + 1), axes=count, curves=[], show_pf=True)
        self.document.edit(lambda study: study.plots.append(spec))
        self.rebuild_plots()
        self.book.select(self.plot_pages[-1])
        self.update_document(invalidate=False)

    def define_variables(self, element_id=None):
        if not self.document:
            return
        if not self.document.study.plots:
            self.new_plot(2)
        catalog = self.catalog
        live = self.manager.running
        if live:
            acquired = set(self.buffer.keys or self.document.study.monitored)
            catalog = [row for row in catalog if row['key'] in acquired]
        VariablesDialog(self, catalog, self.document.study.plots, self.add_variables, element_id, index=self.index, live=live)

    def add_variables(self, page, axis, keys):
        def change(study):
            curves = study.plots[page].setdefault('curves', [])
            for key in keys:
                row = next((r for r in self.catalog if r['key'] == key), {})
                curve = dict(key=key, axis=axis, label=element_label(self.index.get(row.get('element_id'), {}), self.index))
                if curve not in curves:
                    curves.append(curve)
                if key not in study.monitored:
                    study.monitored.append(key)
            validate_plot_specs(study.plots, {row['key'] for row in self.catalog})
        try:
            self.document.edit(change)
        except ValueError as exc:
            self.show_error(exc)
            return
        self.rebuild_plots()
        self.book.select(self.plot_pages[page])
        self.update_document(invalidate=False)
        for notice in validity_messages(self.document.study, self.root_path, self.index) + frequency_messages(self.document.study):
            self.output.log('AVISO', notice)

    def remove_curve(self):
        if not self.available():
            return
        current = self.current_tab()
        if current not in self.plot_pages or not current.spec.get('curves'):
            return
        window = tk.Toplevel(self)
        window.title(tr('shell.remove_curve'))
        window.transient(self)
        window.grab_set()
        choices = tk.Listbox(window, width=100, height=12)
        choices.pack(fill='both', expand=True, padx=12, pady=12)
        for curve in current.spec['curves']:
            path, variable, unit = curve['key'].rsplit('|', 2)
            choices.insert('end', tr('shell.subplot', value0=curve.get('axis', 0) + 1, value1=path.rsplit(chr(92), 1)[-1], value2=variable, value3=unit))
        def remove():
            selected = choices.curselection()
            if selected:
                page_index = self.plot_pages.index(current)
                self.document.edit(lambda study: study.plots[page_index]['curves'].pop(selected[0]))
                self.rebuild_plots()
                self.update_document(invalidate=False)
                window.destroy()
        ttk.Button(window, text=tr('shell.remove'), command=remove).pack(pady=(0, 12))

    def refresh_plots(self):
        if self._closed:
            return
        current = self.current_tab()
        if current in self.plot_pages:
            current.redraw()
        self.after(250, self.refresh_plots)

    def current_tab(self):
        tab = self.book.select()
        return self.book.nametowidget(tab) if tab else None

    def fit_diagram(self):
        current = self.diagram_page()
        if current is not None:
            current.fit()

    def toggle_boxes(self):
        for view in self.diagrams.values():
            view.set_result_boxes_visible(self.show_boxes.get())

    def export_diagram(self):
        current = self.current_tab()
        if current in self.diagrams.values():
            path = filedialog.asksaveasfilename(parent=self, defaultextension='.ps', filetypes=[('PostScript', '*.ps')])
            if path:
                current.export_postscript(Path(path))

    def close_tab(self):
        self.capture_presentation()
        current = self.current_tab()
        for panel_id, (frame, _) in list(self.parity_panels.items()):
            if current == frame:
                self.close_parity_panel(panel_id)
                return
        if not self.available():
            return
        if current == self.diagrams.get(None):
            return
        if current in self.plot_pages:
            index = self.plot_pages.index(current)
            self.document.edit(lambda study: study.plots.pop(index))
            self.rebuild_plots()
            self.update_document(invalidate=False)
        else:
            for group, view in list(self.diagrams.items()):
                if view == current:
                    del self.diagrams[group]
            if current in self.table_pages:
                self.table_pages.remove(current)
            if current:
                current.destroy()

    def show_validity(self):
        if self.document:
            messages = validity_messages(self.document.study, self.root_path, self.index)
            text = getattr(self,'pf_evidence_text','') or ('\n\n'.join(messages) if messages else tr('shell.within_the_reference_domain_scenario_and_events'))
            for notice in frequency_messages(self.document.study):
                text += tr('shell.frequency_use_notice') + notice
            messagebox.showinfo(tr('shell.validity_domain'), text, parent=self)

    def show_error(self, error):
        from .analysis_workspace import STUDIES
        from .display import public_text
        analysis=getattr(self,'analysis',None)
        key=self.__dict__.pop('_failed_worker_study',getattr(analysis,'active_study','flow'))
        operation=next((tr(label) for study,label,_ in STUDIES if study==key),key)
        operation=self.__dict__.pop('_failed_worker_operation',operation)
        case=self.__dict__.pop('_failed_worker_case',self.case_label.get() if hasattr(self,'case_label') else '—')
        case_token=self.__dict__.pop('_failed_worker_case_token',None)
        recovery=tr('v3.fix.error_recovery', operation=operation, case=case, cause=public_text(clean_text(error)))
        self.output.log('ERROR', recovery)
        if analysis and (case_token is None or case_token==(id(self.document),self.document.active_case_id)):
            analysis.study_states[key]='error'
            analysis._render_states()
            page=getattr(self,'welcome_page',None)
            if page:page.refresh()
        if hasattr(self,'state_text'):self.state_text.set(tr('v3.fix.failed'))
        if analysis:analysis.results.select(analysis.result_hosts['log'])
        if self.selftest:
            self.finish_selftest(str(error))
        else:
            from .display import public_text
            messagebox.showerror('Power Machine', recovery, parent=self)

    def begin_selftest(self):
        if not self.api or not self.document:
            self.finish_selftest(tr('shell.study_layer_unavailable'))
            return
        try:
            study = self.api.reference_study(self.root_path, 'BASE_15s')
            study.options.t_end_s = .2
            self.install_document(Document(study))
            self._selftest_report['diagram_loaded'] = bool(self.diagrams)
            self._selftest_report['element_count'] = len(self.index)
            self._selftest_report['default_page'] = bool(self.plot_pages and self.plot_pages[0].spec['curves'])
            dialog = self.simulation_dialog()
            self._selftest_report['simulation_dialog'] = dialog.title() == 'Simulación RMS'
            dialog.destroy()
            self.run_loadflow()
        except Exception:
            self.finish_selftest(traceback.format_exc())

    def finish_selftest(self, error=None):
        if self._closed:
            return
        if self.manager.running and not error:
            self.after(150, lambda: self.finish_selftest(error))
            return
        self._selftest_report.update(passed=not bool(error), error=clean_text(error) if error else None,
                                     heartbeat_ticks=self._heartbeat, max_heartbeat_gap_s=self._max_tick_gap)
        if not error and not all(self._selftest_report.get(k) for k in ('default_page', 'simulation_dialog', 'progress_visible', 'progress_received')):
            error = tr('shell.incomplete_self_test_dialog_page_or_progress')
            self._selftest_report.update(passed=False, error=error)
        self.exit_code = 1 if error else 0
        path = results_root(self.root_path) / 'selftest.json'
        path.write_text(json.dumps(self._selftest_report, ensure_ascii=False, indent=2, default=str), encoding='utf-8')
        self.destroy_app()

    def close(self):
        if self.closing:
            return
        if not self.maybe_save():
            return
        if self.manager.running:
            if not messagebox.askyesno(tr('shell.active_simulation'), tr('shell.stop_the_simulation_and_close_while_retaining'), parent=self):
                return
            self.closing = True
            self.stop()
        elif self.busy:
            self.closing = True
            self.state_text.set(tr('shell.the_window_will_close_when_the_calculation'))
        else:
            self.destroy_app()

    def destroy_app(self):
        try:
            self.save_window_preferences()
            self.windows.save()
        except (OSError,ValueError,tk.TclError) as exc:self.output.log('AVISO',tr('shell.unable_to_save_the_layout')+str(exc))
        self._closed = True
        self.close_parity_panels()
        self.manager.shutdown()
        self.progress.stop()
        for job in self.tk.call('after', 'info'):
            self.tk.call('after', 'cancel', job)
        self.destroy()

    def report_callback_exception(self, exc, value, tb):
        text = ''.join(traceback.format_exception(exc, value, tb))
        if hasattr(self, 'output'):
            self.output.log('ERROR', text)
        if self.selftest:
            self.finish_selftest(text)
