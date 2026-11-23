"""Controles externos del LF, presentación común sin ecuaciones eléctricas."""
from .panel_widgets import action_button
from copy import deepcopy
import json
import tkinter as tk
from tkinter import ttk, filedialog

from power_machine.lf_controls import inventory, preflight, save_proposal
from ..i18n import tr
from .panel_widgets import study_panel, ResultTable, element_label, finish_panel, human_status, diagnostic_lines, panel_retranslate

PANEL_SPEC = dict(id='calc_lfctl', title=tr('panel.loadflow.title'), title_key='panel.loadflow.title', api_version=1,
                  factory='create_panel', order=46)


def build_request(context, inputs, options):
    return dict(schema='power_machine-parity-request-v1', module='lf_controls', root=str(context.root),
                snapshot=deepcopy(context.get_snapshot()), input=deepcopy(inputs), options=deepcopy(options))


class PanelController:
    def __init__(self, context, update):
        self.context, self.on_update = context, update
        self.result, self.job = None, None

    def submit(self, inputs, options):
        request = build_request(self.context, inputs, options)
        preflight(request['snapshot']['model'], inputs, options)
        self.job = self.context.submit_job('lf_controls', request, self.on_message)
        return request

    def on_message(self, message):
        if isinstance(message, (tuple, list)) and message and message[0] == 'result':
            message = message[1]
        if isinstance(message, dict) and message.get('schema') == 'power_machine-lf-controls-result-v1':
            self.result = message
            self.context.publish_result(message)
            self.on_update(message)

    def cancel(self):
        if self.job is not None and hasattr(self.job, 'cancel'):
            self.job.cancel()


def create_panel(parent, context):
    frame, form, results = study_panel(parent)
    model = context.get_snapshot()['model']
    rows = inventory(model)
    items = {x['id']: x for g in ('branches', 'branches3', 'shunts') for x in model.get(g, [])}
    controls = []
    selected = tk.StringVar(frame, '')
    choices = {element_label(items[r['object_id']], model) + ' · ' + r['field']: r for r in rows if r['available']}
    bus_choices = {element_label(b, model): b['id'] for b in model.get('buses', []) if not b.get('outserv')}
    bus = tk.StringVar(frame, '')
    target, deadband = tk.StringVar(frame, ''), tk.StringVar(frame, '')
    maximum = tk.StringVar(frame, '20')
    bands = tk.StringVar(frame, '[]')
    loading = tk.StringVar(frame, '')
    summary = tk.StringVar(frame, tr('panel.loadflow.preflight_idle'))
    labels = [('equipment_channel', selected), ('controlled_bus', bus), ('setpoint', target),
              ('deadband', deadband), ('maximum_iterations', maximum),
              ('voltage_bands', bands), ('loading_limit', loading)]
    for index, (label, var) in enumerate(labels):
        ttk.Label(form, text=tr('panel.loadflow.' + label)).grid(row=index * 2, column=0, sticky='w')
        if index in (0, 1):
            ttk.Combobox(form, textvariable=var, values=list(choices if index == 0 else bus_choices),
                         state='readonly', width=35).grid(row=index * 2 + 1, column=0, sticky='ew')
        else:
            ttk.Entry(form, textvariable=var, width=35).grid(row=index * 2 + 1, column=0, sticky='ew')
    ttk.Label(form, text=tr('panel.loadflow.bands_help'), wraplength=305).grid(row=14, column=0, sticky='w')
    configured = tk.Listbox(form, height=3, exportselection=False)
    configured.grid(row=16, column=0, sticky='ew')
    def add():
        try:
            row = choices[selected.get()]
            ctl = dict(key=row['key'], bus_id=bus_choices[bus.get()], target_pu=float(target.get()), deadband_pu=float(deadband.get()))
            trial = [c for c in controls if c['key'] != ctl['key']] + [ctl]
            preflight(context.get_snapshot()['model'], dict(controls=trial), {})
            controls[:] = trial
            configured.delete(0, 'end')
            for c in controls:
                configured.insert('end', next(r['name'] + ' · ' + r['field'] for r in rows if r['key'] == c['key']) + ' → ' + str(c['target_pu']) + ' p.u.')
            summary.set(tr('panel.loadflow.preflight_ready'))
        except (ValueError, KeyError) as exc:
            summary.set(str(exc))
    add_button = ttk.Button(form, text=tr('panel.loadflow.add_control'), command=add)
    add_button.grid(row=15, column=0, sticky='ew')
    def clear():
        controls.clear()
        configured.delete(0, 'end')
    clear_button = ttk.Button(form, text=tr('panel.loadflow.remove_controls'), command=clear)
    clear_button.grid(row=17, column=0)
    ttk.Label(form, textvariable=summary, wraplength=305).grid(row=18, column=0, sticky='w')
    validation_note = ttk.Label(form, text=tr('panel.loadflow.validation_note'), wraplength=305)
    validation_note.grid(row=19, column=0, sticky='w')
    form.columnconfigure(0, weight=1)
    pages = ttk.Notebook(results)
    pages.pack(fill='both', expand=True)
    tables = {}
    table_specs = (
        ('movements', 'movements', [('name', 'equipment'), ('iteration', 'iteration'), ('initial', 'initial_position'), ('final', 'final_position'), ('u_after_pu', 'voltage_pu')]),
        ('violations', 'violations', [('name', 'element'), ('quantity', 'quantity'), ('value', 'value'), ('limit', 'threshold'), ('severity', 'relative_excess')]),
        ('inventory', 'inventory', [('name', 'equipment'), ('field', 'channel'), ('position', 'position'), ('minimum', 'minimum'), ('maximum', 'maximum'), ('step', 'step'), ('reason', 'restriction')]))
    for key, title_key, columns in table_specs:
        page = ttk.Frame(pages)
        pages.add(page, text=tr('panel.loadflow.' + title_key))
        tables[key] = ResultTable(page, [(name, tr('panel.loadflow.' + label)) for name, label in columns], context.root)
        tables[key].pack(fill='both', expand=True)
    tables['inventory'].set_rows(rows)
    diagnostics = tk.Text(frame.plot_area, height=7, wrap='word')
    diagnostics.pack(fill='both', expand=True)
    def update(result):
        payload = result.get('payload', {})
        for key, table in tables.items():
            table.set_rows(payload.get(key, []))
        frame.set_status(result['status'], freshness=tr('panel.common.current'), validation_label=tr('panel.common.not_validated_pf'))
        summary.set(tr('panel.loadflow.loop', value=human_status(payload.get('stop_reason', result['status']))))
        diagnostics.delete('1.0', 'end')
        diagnostics.insert('end', '\n'.join(diagnostic_lines(result)))
        diagnostics.insert('end', '\n' + tr('panel.common.not_evaluable_count', count=len(payload.get('not_evaluable', []))))
    controller = PanelController(context, update)
    def submit():
        try:
            options = dict(max_iterations=int(maximum.get()), voltage_bands=json.loads(bands.get()),
                           loading_limit_pct=float(loading.get()) if loading.get().strip() else None)
            controller.submit(dict(controls=controls), options)
        except (ValueError, KeyError, TypeError) as exc:
            summary.set(tr('panel.common.preflight_blocked', reason=str(exc)))
    def save():
        if controller.result:
            path = filedialog.asksaveasfilename(parent=frame, initialdir=context.root, defaultextension='.json')
            if path:
                try:
                    save_proposal(controller.result, path, context.root)
                    summary.set(tr('panel.loadflow.proposal_saved'))
                except (ValueError, OSError) as exc:
                    summary.set(str(exc))
    run_button = action_button(form, 'run', text=tr('panel.common.run'), command=submit)
    cancel_button = action_button(form, 'cancel', text=tr('panel.common.cancel'), command=controller.cancel)
    save_button = action_button(form, 'export', text=tr('panel.loadflow.save_proposal'), command=save)
    run_button.grid(row=20, column=0)
    cancel_button.grid(row=21, column=0)
    save_button.grid(row=22, column=0)
    frame.controller, frame.tables = controller, tables
    frame.submit = submit
    frame.preflight_inputs = lambda: {"input": {"controls": deepcopy(controls)}, "options": dict(max_iterations=int(maximum.get()), voltage_bands=json.loads(bands.get()), loading_limit_pct=float(loading.get()) if loading.get().strip() else None)}
    def retranslate():
        add_button.configure(text=tr('panel.loadflow.add_control'))
        clear_button.configure(text=tr('panel.loadflow.remove_controls'))
        validation_note.configure(text=tr('panel.loadflow.validation_note'))
        run_button.configure(text=tr('panel.common.run'))
        cancel_button.configure(text=tr('panel.common.cancel'))
        save_button.configure(text=tr('panel.loadflow.save_proposal'))
        for key, title_key, columns in table_specs:
            pages.tab(pages.tabs()[list(tables).index(key)], text=tr('panel.loadflow.' + title_key))
            for name, label in columns:
                tables[key].tree.heading(name, text=tr('panel.loadflow.' + label))
    panel_retranslate(frame, retranslate)
    return finish_panel(frame, context, controller, tr('panel.loadflow.scope'))
