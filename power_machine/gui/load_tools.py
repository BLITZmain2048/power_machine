"""Escalado explícito de demanda como un único comando reversible."""
import math
import tkinter as tk
from tkinter import ttk, messagebox
from .i18n import tr


def scaling_preview(model, ids, p_factor, q_factor, *, operational=None):
    from power_machine.netedit.commands import edit_command
    if any(not math.isfinite(v) or v < 0 for v in (p_factor, q_factor)):
        raise ValueError(tr('tools.factor_error'))
    effective={l['id']:l for l in (operational or model).get('loads',[])}
    commands, before, after = [], [0., 0.], [0., 0.]
    for load in model.get('loads', []):
        active=effective.get(load['id'],load)
        if load['id'] not in ids or active.get('outserv'):
            continue
        p, q = float(load['p_mw']), float(load['q_mvar'])
        scale = float(active.get('scale', 1))
        before[0] += float(active['p_mw']) * scale
        before[1] += float(active['q_mvar']) * scale
        after[0] += float(active['p_mw']) * scale * p_factor
        after[1] += float(active['q_mvar']) * scale * q_factor
        commands.append(edit_command(model, 'update', 'loads', load['id'],
                                     dict(p_mw=p*p_factor, q_mvar=q*q_factor,
                                          params={**load.get('params', {}), 'plini':p*p_factor,'qlini':q*q_factor})))
    return commands, before, after


def open_load_tools(app):
    from .presentation import station
    win = tk.Toplevel(app)
    win.title(tr('tools.scale_loads'))
    win.geometry('650x480')
    form = ttk.Frame(win, padding=18)
    form.pack(fill='both', expand=True)
    from .properties_dock import ensure_session
    model = ensure_session(app).model
    document, revision = app.document, app.document.revision
    buses = {b['id']: b for b in model['buses']}
    scopes = ('all','zone','station','voltage','selection')
    scope, value = tk.StringVar(win,tr('tools.scope.all')), tk.StringVar(win)
    p, q, together = tk.StringVar(win,'1.0'), tk.StringVar(win,'1.0'), tk.BooleanVar(win,True)
    ttk.Label(form,text=tr('tools.scope')).pack(anchor='w')
    combo = ttk.Combobox(form,textvariable=scope,values=[tr('tools.scope.'+s) for s in scopes],state='readonly')
    combo.pack(fill='x')
    combo.current(0)
    ttk.Label(form,text=tr('tools.scope_value')).pack(anchor='w')
    values = ttk.Combobox(form,textvariable=value,state='readonly')
    values.pack(fill='x',pady=8)
    def scope_key():return scopes[combo.current()]
    def category(load):
        bus = buses.get(load.get('bus'),{})
        return {'zone':str(load.get('zone',bus.get('zone',''))), 'station':station(load) or station(bus),
                'voltage':str(bus.get('un_kv',''))}.get(scope_key(),'')
    def refresh(_=None):
        choices=sorted({category(l) for l in model['loads'] if category(l)})
        values.configure(values=choices)
        value.set(tr('tools.scope.all') if scope_key()=='all' else choices[0] if choices else tr('tools.scope.selection') if scope_key()=='selection' else '')
        values.configure(state='disabled' if scope_key() in ('all','selection') else 'readonly')
    combo.bind('<<ComboboxSelected>>',refresh)
    refresh()
    entries={}
    for key,var in [('tools.p_factor',p),('tools.q_factor',q)]:
        ttk.Label(form,text=tr(key)).pack(anchor='w',pady=(8,0))
        entries[key]=ttk.Entry(form,textvariable=var)
        entries[key].pack(fill='x')
    ttk.Checkbutton(form,text=tr('tools.keep_pf'),variable=together).pack(anchor='w',pady=10)
    def sync_q(*args):
        if together.get():q.set(p.get())
        entries['tools.q_factor'].configure(state='disabled' if together.get() else 'normal')
    p.trace_add('write',sync_q)
    together.trace_add('write',sync_q)
    sync_q()
    win.q_entry=entries['tools.q_factor']
    win.together=together
    win.scope_combo=combo
    win.value_combo=values
    summary = tk.StringVar(win,tr('tools.preview_first'))
    ttk.Label(form,textvariable=summary,wraplength=590).pack(fill='x',pady=12)
    def preview():
        selected = set()
        for view in app.diagrams.values():selected.update(getattr(view,'selected_ids',set()))
        if app.selected_id:selected.add(app.selected_id)
        ids={l['id'] for l in model['loads'] if scope_key()=='all' or
             (scope_key()=='selection' and l['id'] in selected) or
             (scope_key() not in ('all','selection') and value.get() and category(l)==value.get())}
        result=scaling_preview(model,ids,float(p.get()),float(p.get()) if together.get() else float(q.get()),operational=app.operational)
        summary.set(tr('tools.preview_one' if len(result[0])==1 else 'tools.preview',count=len(result[0]),p=result[1][0],q=result[1][1],p2=result[2][0],q2=result[2][1]))
        return result[0]
    def apply():
        if not app.available():return
        try:
            if app.document is not document or document.revision!=revision:
                raise ValueError(tr('integration.stale_network_revision'))
            commands=preview()
            if not commands:raise ValueError(tr('tools.no_loads'))
            result=app.document.network.commit(commands,app.document.network.revision,tr('tools.scale_loads'))
            if not result['accepted']:raise ValueError(str(result.get('diagnostics')))
            app.network_changed()
            app._tools_network_revision=(id(app.document),app.document.revision)
            if app.selected_id in app.index and hasattr(app,'properties_dock'):
                app.properties_dock.show_element(app.selected_id)
            win.destroy()
        except (ValueError,TypeError) as exc:messagebox.showerror(tr('tools.scale_loads'),str(exc),parent=win)
    def show_preview():
        try:preview()
        except (ValueError,TypeError) as exc:summary.set(str(exc))
    win.scope=scope
    win.scope_value=value
    win.p_factor=p
    win.q_factor=q
    win.preview_button=ttk.Button(form,text=tr('tools.preview_action'),command=show_preview)
    win.preview_button.pack(side='left')
    ttk.Button(form,text=tr('edit.apply'),command=apply).pack(side='right')
    from .theme import theme
    theme.apply(win)
    return win
