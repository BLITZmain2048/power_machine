"""Coordinación de paneles y tareas de la ventana principal."""
from copy import deepcopy
from pathlib import Path
from queue import Empty
import threading
import time
import traceback
from .i18n import tr
from .theme import theme
import tkinter as tk
from tkinter import ttk, filedialog
from .document import Document
from .panel_context import PanelContext, cancel_panel_timers
from .benchmark_actions import benchmark_scene

class PanelCoordination:
    def open_parity_panel(self, panel_id, *, context_factory=PanelContext):
        if not self.available():
            return
        if panel_id in self.parity_panels:
            self.book.select(self.parity_panels[panel_id][0])
            return
        entry = self.panel_registry.entries[panel_id]
        frame = ttk.Frame(self.book)
        context = context_factory(self, self.root_path, lambda: self.document,
                               lambda: self.base_model if panel_id == 'network_editor' else self.model,
                               log=self.output.log, select_element=self.select,
                               on_result=lambda result: self.publish_parity_result(result, panel_id))
        context.external_busy = lambda: self.busy or self.manager.running
        context.on_activity = self.refresh_tab_navigation if hasattr(self, "refresh_tab_navigation") else lambda: None
        if panel_id=='network_editor':
            from power_machine.netedit import new_variant
            from power_machine.netedit.schema import variant_to_dict
            session=self.document.network
            if session.state is None:
                session.load_data(self.base_model,{'schema':'power_machine-gui-network-v1',
                    'variant':variant_to_dict(new_variant(self.base_model)),'drafts':{}})
            context.network_session=session
        panel = None
        try:
            host = ttk.Frame(frame)
            host.pack(fill='both', expand=True)
            panel = self.panel_registry.create(panel_id, host, context)
            if getattr(self, 'model_path', None):
                self.initialize_benchmark_controls(panel_id, panel)
            if hasattr(panel,'actions') and hasattr(panel,'form'):
                panel.actions.pack_configure(before=panel.form)
            if panel_id == 'network_editor' and hasattr(panel, 'controller'):
                if getattr(panel.controller,'session',None) is self.document.network:
                    def changed(event):
                        if event['electrical_changed'] and not context.closed:
                            self.network_changed()
                    context.network_subscription=context.network_session.subscribe(changed)
                else:
                    self.document.network.attach(panel.controller,self.network_changed)
            if panel_id == 'network_editor' and hasattr(self,'configure_net_toolbar'):
                self.configure_net_toolbar(panel)
            if panel_id == 'dsl_editor':
                from .app_shell import EditorTabStrip
                def adapt_tabs(widget):
                    for child in list(widget.winfo_children()):
                        if isinstance(child,ttk.Notebook):
                            EditorTabStrip(child)
                        else:
                            adapt_tabs(child)
                adapt_tabs(panel)
            if hasattr(self,'restore_panel_workspace'):self.restore_panel_workspace(panel_id,panel)
            context.panel = panel
            context.title = entry.display_title()
            panel.pack(fill='both', expand=True)
            self.book.add(frame, text=context.title)
            self.parity_panels[panel_id] = (frame, context)
            self.book.select(frame)
        except Exception as exc:
            if panel_id=='network_editor' and hasattr(context,'network_subscription'):
                context.network_session.unsubscribe(context.network_subscription)
            context.close()
            cancel_panel_timers(frame)
            frame.destroy()
            self.output.log('ERROR', tr('shell.unable_to_open', value0=entry.title, value1=exc))

    def publish_parity_result(self, result, panel_id=None):
        result = deepcopy(result)
        result['stale'] = False
        self.parity_results[result['module']] = result
        if hasattr(self,'bus'):
            self.bus.publish('results.updated', study=result['module'], result_id=str(result.get('job_id',result['module'])))
        if hasattr(self,'archive_result'):
            try:self.archive_result(result['module'],result,panel_id)
            except Exception as exc:self.output.log('ERROR',tr('shell.unable_to_link_the_run')+str(exc))
        if panel_id in self.parity_panels:
            frame, context = self.parity_panels[panel_id]
            self.book.tab(frame, text=context.title)
            context.stale = False
        self.refresh_tab_navigation()

        self.output.log('INFO', f"{result['module']}: {result['status']}, "+result.get('gui_validation_label',tr('shell.no_pf_validation')))
        if result.get('gui_validation_label'):self.validity_text.set(result['gui_validation_label'])

    def initialize_benchmark_controls(self, panel_id, panel):
        """Configura por API pública, sin depender de rótulos ni geometría Tk."""
        configure = getattr(panel, 'configure_case', None)
        if not callable(configure):
            return
        path = self.model_path.parent / 'shortcircuit_data.json'
        config = dict(frequency_hz=self.model['meta']['f_nom_hz'],
                      q_limits=self.document.study.ldf_options.get('q_limits', False),
                      supplemental_path=str(path) if path.is_file() else '')
        if callable(configure):
            configure(config)

    def close_parity_panel(self, panel_id):
        if panel_id in self.parity_panels and not self._closed:self.capture_presentation()
        item = self.parity_panels.pop(panel_id, None)
        if item is not None:
            frame, context = item
            if panel_id=='network_editor':
                if hasattr(context,'network_subscription'):
                    context.network_session.unsubscribe(context.network_subscription)
                elif self.document is not None and self.document.network.controller is not None:
                    self.document.network.detach()
            context.close()
            cancel_panel_timers(frame)
            frame.destroy()

    def close_parity_panels(self):
        for panel_id in list(self.parity_panels):
            self.close_parity_panel(panel_id)

    def start_task(self, name, function, callback):
        self.set_busy(True, name)
        def work():
            try:
                self.tasks.put(('task', callback, function(), None))
            except Exception:
                self.tasks.put(('task', callback, None, traceback.format_exc()))
        threading.Thread(target=work, daemon=True, name='power_machine-calculo').start()

    def poll_tasks(self):
        if self._closed:
            return
        now = time.monotonic()
        self._max_tick_gap = max(self._max_tick_gap, now - self._last_tick)
        self._last_tick = now
        self._heartbeat += 1
        try:
            for _ in range(12):
                kind, callback, value, error = self.tasks.get_nowait()
                if kind == 'task':
                    self.set_busy(False)
                if error:
                    self.output.log('ERROR', error)
                    self.state_text.set(tr('shell.calculation_error'))
                    if self.selftest and kind == 'task':
                        self.finish_selftest(error)
                        return
                else:
                    callback(value)
        except Empty:
            pass
        if self.selftest and now > self._selftest_deadline:
            self.finish_selftest(tr('shell.self_test_time_limit_exceeded'))
            return
        if self.closing and not self.manager.running and not self.busy:
            self.destroy_app()
            return
        self.after(100, self.poll_tasks)


    def install_calculation_menu(self):
        self.calculation_items,self.calculation_details={},{}
        modules=dict(shortcircuit='sc_sweep',calc_ccbar='sc_sweep',contingency='contingency',calc_pvqv='voltage_stability',
            calc_sens='sensitivities',calc_qds='qds_panel',modal='modal',protection='protection',calc_cct='cct',calc_met='dyn_metrics',calc_lfctl='lf_controls')
        calculations = [('shortcircuit', tr('shell.iec_ansi_short_circuit')),
            ('calc_ccbar', tr('shell.short_circuit_by_busbar')), ('contingency', tr('shell.n_1_n_2_contingencies')),
            ('calc_pvqv', tr('shell.pv_qv_voltage_stability')), ('calc_sens', tr('shell.sensitivities')),
            ('calc_qds', tr('shell.quasi_dynamic_simulation')), ('modal', tr('shell.modal_analysis')),
            ('protection', tr('shell.protection')), ('calc_cct', tr('shell.critical_clearing_time')),
            ('calc_met', tr('shell.dynamic_metrics')), ('calc_lfctl', tr('shell.lf_controls_and_limits'))]
        for ident, title in calculations + [('network_editor', tr('shell.network_editor')), ('dsl_editor', tr('shell.dsl_editor'))]:
            menu = self.menus['edit' if ident in ('network_editor', 'dsl_editor') else 'calculation']
            if ident in modules:
                child=tk.Menu(menu,tearoff=False)
                child.add_command(label=tr('shell.open_79d37e')+title,command=lambda key=ident:self.open_parity_panel(key))
                child.add_command(label=tr('shell.restrictions_and_required_data'),command=lambda key=ident:self.show_calculation_details(key))
                menu.add_cascade(label=title+tr('shell.with_restrictions'),menu=child)
                self.calculation_items[ident]=(child,menu.index('end'),title,modules[ident])
                continue
            menu.add_command(label=title, state='normal' if ident in self.panel_registry.entries else 'disabled',
                             command=lambda key=ident: self.open_parity_panel(key))

    def invalidate_panel_results(self):
        from .panel_context import fingerprint
        snapshot = self.document.snapshot()
        # Los cambios de presentación no modifican el punto de operación.
        calculation = {key: value for key, value in snapshot.items()
                       if key not in ('plots', 'name', 'monitored')}
        signature = fingerprint({'study': calculation, 'model': self.model})
        previous = getattr(self, '_panel_signature', None)
        self._panel_signature = signature
        if previous is None or previous == signature:
            return
        reason = tr('shell.the_scenario_network_or_study_options_changed')
        if hasattr(self,'bus'):self.bus.publish('results.stale',reason=reason)
        for result in self.parity_results.values():
            result.update(stale=True, stale_reason=reason, freshness='stale')
        for frame, context in self.parity_panels.values():
            context.invalidate(reason)
            context.stale = True
            self.book.tab(frame, text=context.title + tr('shell.stale_e3a3a5'))
            mark = getattr(context.panel, 'mark_stale', None)
            if callable(mark):
                try:
                    mark(reason)
                except Exception:
                    self.output.log('ERROR', traceback.format_exc())
        self.refresh_tab_navigation()

    def refresh_tab_navigation(self, event=None):
        if not hasattr(self, 'book') or self._closed:
            return
        if hasattr(self.book, 'sync'):
            self.book.sync()
        current = self.book.select()
        state = tr('shell.study_no_result')
        validation = tr('shell.no_pf_comparison_for_the_current_configuration')
        if current:
            page=self.book.nametowidget(current)
            if hasattr(page,'buffer'):
                validation=getattr(page,'gui_validation_label',validation)
                state=tr('shell.rms_running') if self.manager.running else tr('shell.plots_samples', value0=len(page.buffer.rows))
            elif page in self.diagrams.values():
                state=tr('shell.single_line_diagram')
            elif page in self.table_pages:
                state=tr('shell.lf_tables')
        for frame, context in self.parity_panels.values():
            if str(frame) == current:
                if context.worker.running:
                    state = tr('shell.study_computing_or_completing_cancellation')
                elif getattr(context, 'stale', False):
                    state = tr('shell.study_stale')
                elif context.results:
                    result = next(reversed(context.results.values()))
                    from .panels.panel_widgets import human_status
                    state = tr('shell.study_current') + human_status(result.get('status', tr('shell.no_result')))
                    validation=result.get('gui_validation_label',validation)
                else:
                    state = tr('shell.study_pending')
        if hasattr(self, 'stop_button'):
            active = self.manager.running or any(context.worker.running for _,context in self.parity_panels.values())
            self.stop_button.configure(state='normal' if active else 'disabled')
        from .pf_context import comparison_context
        active_page = self.book.nametowidget(current) if current else None
        active_result, active_module = None, None
        for panel_id, (frame, context) in self.parity_panels.items():
            if str(frame) == current:
                active_module = panel_id
                active_result = next(reversed(context.results.values())) if context.results and not getattr(context, 'stale', False) else None
        badge, self.pf_evidence_text = comparison_context(active_page, active_result, active_module)
        if hasattr(self,'validity_text'):self.validity_text.set(badge)
        if hasattr(self,'evidence_button'):self.evidence_button.configure(text=tr('int2.evidence'))
        if hasattr(self, 'study_state_text'):
            self.study_state_text.set(state)

    def select_tab_from_list(self, event=None):
        index = self.tab_selector.current()
        tabs = self.book.tabs()
        if 0 <= index < len(tabs):
            self.book.select(tabs[index])
