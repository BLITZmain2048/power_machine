"""Panel de barrido IEC 60909 por barra, sin ecuaciones en la interfaz."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
import csv
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from ..i18n import tr
from ..theme import theme

from .panel_widgets import human_status, study_panel, finish_panel, element_label, AdvancedOptions, ResultTable, format_axes, panel_retranslate, style_plot

PANEL_SPEC = {"title_key": 'integration.short_circuit_by_busbar', "id": "calc_ccbar", "title": tr('integration.short_circuit_by_busbar'), "api_version": 1,
              "factory": "create_panel", "order": 11}
SC_LABEL = tr('integration.positive_sequence_three_phase_short_circuit_partial')


def source_inventory_rows(result):
    """Fuentes declaradas y exclusiones explícitas del mismo estudio de frontera."""
    payload = result.get("payload", {})
    rows = [dict(row) for row in payload.get("inventory", {}).get("sources", [])]
    coverage = payload.get("power_machine_coverage") or {}
    for boundary in coverage.values():
        replacement = boundary.get('replacement_source_id') if isinstance(boundary,dict) else None
        if replacement:
            rows.append({"object_id": replacement, "name": tr('integration.user_supplied_interface_th_venin_equivalent'), "coverage_status": "incluida",
                         "reason": tr('integration.a_single_external_source_represents_the_external')})
    for group in ("excluded_converters", "excluded_branches3", "excluded_unsupported_branches", "excluded_ideal_switches", "excluded_ideal_lines"):
        for row in coverage.get(group, []):
            rows.append({**row, "coverage_status": "excluida", "name": row.get("name", row.get("object_id"))})
    return rows


def build_request(context, options, *, selected_bus_ids=None, include_contributions=False, power_machine_mode=None, thevenin=None, pf_export_folder=None, pf_reference_csv=None, supplemental=None, supplemental_sha256=None, supplemental_path=None):
    """Construye la petición sin leer ni calcular datos eléctricos en Tk."""
    inputs = {"selected_bus_ids": list(selected_bus_ids or ()), "include_contributions": bool(include_contributions)}
    for key, value in (("supplemental", supplemental), ("supplemental_sha256", supplemental_sha256), ("supplemental_path", supplemental_path), ("power_machine_mode", power_machine_mode), ("thevenin", thevenin), ("pf_export_folder", pf_export_folder), ("pf_reference_csv", pf_reference_csv)):
        if value is not None:
            inputs[key] = copy.deepcopy(value)
    return {"schema": "power_machine-sc-sweep-request-v1", "module": "sc_sweep", "root": str(context.root),
            "snapshot": copy.deepcopy(context.get_snapshot()), "options": copy.deepcopy(options),
            "input": inputs}


def export_rows(rows, destination):
    """Exporta la tabla visible sin reemplazar campos ausentes por ceros."""
    fields = ("bus_id", "object_id", "name", "substation", "un_kv", "status", "evaluation_status", "ik_initial_sym_ka",
              "ip_peak_ka", "sk_initial_mva", "r_x", "z1_r_ohm", "z1_x_ohm", "relative_path")
    destination = Path(destination)
    with destination.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in fields})
    return destination


class PanelController:
    """Adaptador ensayable entre el contexto y el motor registrado por integración."""
    def __init__(self, context, on_update=None):
        self.context = context
        self.on_update = on_update or (lambda _message: None)
        self.result = None
        self.job = None

    def submit(self, options, *, selected_bus_ids=None, include_contributions=False, **study_inputs):
        request = build_request(self.context, options, selected_bus_ids=selected_bus_ids,
                                include_contributions=include_contributions, **study_inputs)
        self.job = self.context.submit_job("sc_sweep", request, self.on_message)
        self.context.log("info", tr('integration.short_circuit_sweep_no_pf_validation'))
        return request

    def on_message(self, message):
        if isinstance(message, dict) and message.get("schema") == "power_machine-parity-result-v1":
            self.result = message
        elif isinstance(message, (tuple, list)) and message and message[0] == "result":
            self.result = message[1]
        elif isinstance(message, (tuple, list)) and message and message[0] == "progress":
            self.on_update(message)
            return
        elif isinstance(message, (tuple, list)) and message and message[0] == "error":
            self.context.log("error", str(message[1]))
            self.on_update(message)
            return
        else:
            self.on_update(message)
            return
        self.context.publish_result(self.result)
        self.on_update(self.result)

    def cancel(self):
        if self.job is not None and callable(getattr(self.job, "cancel", None)):
            self.job.cancel()
        else:
            self.context.log("warning", tr('integration.cancellation_is_requested_from_the_job_control'))


def create_panel(parent, context):
    """Crea un formulario desplazable y deja resultados y gráfico en altura útil."""
    frame, form, results = study_panel(parent)
    snapshot = context.get_snapshot()
    buses = [bus for bus in snapshot["model"].get("buses", ()) if bus.get("outserv", 0) != 1]
    display = {element_label(bus, snapshot["model"]): bus["id"]
               for bus in buses}
    levels = sorted({str(bus.get("un_kv")) for bus in buses if bus.get("un_kv") is not None})
    vars_ = {"bus": tk.StringVar(form, value=""), "kv": tk.StringVar(form, value=tr('integration.all')),
             "kind": tk.StringVar(form, value="3ph"), "case": tk.StringVar(form, value="maximum"),
             "branches": tk.BooleanVar(form, value=False), "status": tk.StringVar(form,
             value=tr('integration.no_pf_validation_f85232'))}
    ttk.Label(form, text=tr('integration.iec_60909_sweep_no_pf_validation')).grid(row=0, column=0, columnspan=3, sticky="w")
    ttk.Label(form, text=tr('integration.optional_busbar')).grid(row=1, column=0, sticky="w")
    ttk.Combobox(form, textvariable=vars_["bus"], values=tuple(display), state="readonly").grid(row=1, column=1, sticky="ew")
    ttk.Label(form, text=tr('integration.level_kv')).grid(row=2, column=0, sticky="w")
    ttk.Combobox(form, textvariable=vars_["kv"], values=(tr('integration.all'), *levels), state="readonly").grid(row=2, column=1, sticky="ew")
    ttk.Label(form, text="Falla").grid(row=3, column=0, sticky="w")
    ttk.Combobox(form, textvariable=vars_["kind"], values=("3ph", "ll", "lg", "llg"), state="readonly").grid(row=3, column=1, sticky="ew")
    ttk.Label(form, text=tr('integration.iec_case')).grid(row=4, column=0, sticky="w")
    ttk.Combobox(form, textvariable=vars_["case"], values=("maximum", "minimum"), state="readonly").grid(row=4, column=1, sticky="ew")
    ttk.Checkbutton(form, text=tr('integration.include_branch_contributions'), variable=vars_["branches"]).grid(row=5, column=0, columnspan=2, sticky="w")
    columns = ("name", "substation", "kv", "status", "ik", "ip", "sk", "rx", "evaluation")
    pages = ttk.Notebook(results)
    pages.grid(row=0, column=0, sticky="nsew")
    row_page, source_page, coverage_page = ttk.Frame(pages), ttk.Frame(pages), ttk.Frame(pages)
    pages.add(row_page, text=tr('integration.busbars_6ebb1c'))
    pages.add(source_page, text="Fuentes")
    pages.add(coverage_page, text=tr('integration.coverage_and_comparison'))
    row_page.columnconfigure(0, weight=1)
    row_page.rowconfigure(8, weight=1)
    source_table = ResultTable(source_page, [("object_id", tr('integration.source_id')), ("name", tr('integration.name')), ("coverage_status", tr('integration.inclusion_coverage')), ("reason", tr('integration.reason'))], context.root)
    source_table.pack(fill="both", expand=True)
    coverage_text = tk.Text(coverage_page, height=8, wrap="word", state="disabled")
    coverage_text.pack(fill="both", expand=True)
    tree = ttk.Treeview(row_page, columns=columns, show="headings", height=10)
    for key, title in (("name", tr('integration.busbar')), ("substation", tr('integration.substation')), ("kv", "kV"), ("status", tr('integration.status')),
                       ("ik", "Ik'' [kA]"), ("ip", "ip [kA]"), ("sk", "Sk'' [MVA]"), ("rx", "R/X"), ("evaluation", tr('integration.coverage_per_row'))):
        tree.heading(key, text=title)
        tree.column(key, width=100, stretch=True)
    tree.grid(row=8, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
    form.rowconfigure(8, weight=1)
    try:
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure
        figure = Figure(layout="constrained", figsize=(6, 2.4), dpi=90)
        axis = figure.add_subplot(111)
        style_plot(figure, axis)
        chart = FigureCanvasTkAgg(figure, master=frame.plot_area)
        chart.get_tk_widget().grid(row=0, column=0, columnspan=3, sticky="nsew")
    except (ImportError, tk.TclError):
        figure = axis = chart = None

    rows = []
    def render(result):
        nonlocal rows
        if isinstance(result, (tuple, list)) and result and result[0] == "progress":
            p = result[1]
            vars_["status"].set(tr('integration.of_busbars', value0=p['completed'], value1=p['total']))
            return
        if isinstance(result, (tuple, list)) and result and result[0] == "error":
            vars_["status"].set(tr('integration.error') + str(result[1]))
            return
        rows = result.get("payload", {}).get("rows", []) if isinstance(result, dict) else []
        for item in tree.get_children():
            tree.delete(item)
        for row in rows:
            tree.insert("", "end", values=(row.get("name"), row.get("substation"), row.get("un_kv"), row.get("status"),
                         row.get("ik_initial_sym_ka"), row.get("ip_peak_ka"), row.get("sk_initial_mva"), row.get("r_x"), row.get("evaluation_status")))
        source_table.set_rows(source_inventory_rows(result))
        coverage_text.configure(state="normal")
        coverage_text.delete("1.0", "end")
        coverage_text.insert("end", json.dumps({"coverage": result.get("payload", {}).get("power_machine_coverage"), "by_bus": result.get("payload", {}).get("inventory", {}).get("buses"), "comparison": result.get("provenance", {}).get("comparison")}, ensure_ascii=False, indent=2))
        coverage_text.configure(state="disabled")
        vars_["status"].set(tr('integration.no_pf_validation', value0=human_status(result.get('status', 'sin resultado'))))
        if axis is not None:
            axis.clear()
            usable = [row for row in rows if row.get("ik_initial_sym_ka") is not None]
            axis.bar(range(len(usable)), [row["ik_initial_sym_ka"] for row in usable], color=theme.color("accent"))
            axis.set_ylabel("Ik'' [kA]")
            axis.set_xlabel(tr('integration.buses_with_results'))
            format_axes(axis)
            chart.draw_idle()
    controller = PanelController(context, render)
    supplemental = tk.StringVar(form, '')
    ttk.Label(form, text=tr('integration.sc_source_and_sequence_json_file')).grid(row=10, column=0, sticky='w')
    ttk.Entry(form, textvariable=supplemental).grid(row=10, column=1, sticky='ew')
    def choose_supplemental():
        path = filedialog.askopenfilename(parent=frame, filetypes=[(tr('integration.sc_data'), '*.json')])
        if path:
            supplemental.set(path)
    ttk.Button(form, text=tr('integration.browse'), command=choose_supplemental).grid(row=10, column=2)
    def requested_inputs():
        selected = [display[vars_["bus"].get()]] if vars_["bus"].get() else [bus['id'] for bus in buses if vars_['kv'].get() in ('Todos','All',tr('integration.all')) or str(bus.get('un_kv')) == vars_['kv'].get()]
        inputs = {'selected_bus_ids': selected, 'include_contributions': vars_['branches'].get()}
        if supplemental.get().strip():
            inputs['supplemental_path'] = supplemental.get().strip()
        return inputs
    def requested_options():
        return {"method": "iec60909", "case": vars_["case"].get(), "fault_kind": vars_["kind"].get(),
                "frequency_hz": float(snapshot["model"].get("meta", {}).get("f_nom_hz", 50.0)), "profile": "iec_pf2016_c1_v1" if snapshot["model"].get("meta", {}).get("benchmark") == "ieee39_pf" else "iec_radial_v1"}
    def submit():
        try:
            controller.submit(requested_options(), **requested_inputs())
        except (ValueError, TypeError, KeyError) as exc:
            vars_["status"].set(str(exc))
            frame.show_error(exc)
    frame.preflight_inputs = lambda: {"input": requested_inputs(), "options": requested_options()}
    def save():
        destination = filedialog.asksaveasfilename(parent=frame, defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if destination:
            try:
                if not Path(destination).resolve().is_relative_to(Path(context.root).resolve()):
                    raise ValueError(tr('integration.exports_must_remain_inside_the_project'))
                export_rows(rows, destination)
                vars_["status"].set(tr('integration.csv_exported'))
            except (OSError, ValueError) as exc:
                vars_["status"].set(str(exc))
    action_button(form, 'run', text=tr('integration.run'), command=submit).grid(row=6, column=0, sticky="w")
    action_button(form, 'cancel', text=tr('integration.cancel'), command=controller.cancel).grid(row=6, column=1, sticky="w")
    action_button(form, 'export', text=tr('integration.export_csv'), command=save).grid(row=6, column=2, sticky="w")
    ttk.Label(form, textvariable=vars_["status"], wraplength=800).grid(row=7, column=0, columnspan=3, sticky="w")
    def mark_stale(reason):
        vars_["status"].set(tr('integration.stale_result') + str(reason))
    frame.controller = controller
    frame.result_tree = tree
    frame.results.columnconfigure(0, weight=1)
    frame.results.rowconfigure(0, weight=1)
    frame.plot_area.columnconfigure(0, weight=1)
    frame.plot_area.rowconfigure(0, weight=1)
    return finish_panel(frame, context, controller, tr('integration.iec_60909_busbar_sweep_requires_traceable_source'))
