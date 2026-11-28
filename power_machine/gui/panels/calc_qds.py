"""Panel de simulación cuasi-dinámica, sin ecuaciones en tkinter."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
import csv
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from power_machine.qds_panel.adapter import MODULE, RESULT_SCHEMA

from ..i18n import tr
from ..theme import theme
from .panel_widgets import format_axes, study_panel, finish_panel, element_label, AdvancedOptions, ResultTable, verified_qds_inputs, result_pages, style_plot

PANEL_SPEC = {"title_key": 'integration.quasi_dynamic_simulation', "id": "calc_qds", "title": tr('integration.quasi_dynamic_simulation'),
              "api_version": 1, "factory": "create_panel", "order": 70}
STATUS_TEXT = {"completed": tr('integration.calculated'), "partial": tr('integration.partial'), "unsupported": tr('integration.unsupported'),
               "invalid_input": tr('integration.invalid_input'), "error": "Error", "cancelled": tr('integration.cancelled')}


DEMAND_KEYS = ("demand_requested_p_mw", "demand_requested_q_mvar", "demand_supplied_p_mw",
               "demand_supplied_q_mvar", "demand_unsupplied_p_mw", "demand_unsupplied_q_mvar", "balance_p_mw")


def qds_columns():
    return [
        ("demand", tr('integration.demand'), [("t_s", tr('integration.time_s')), *zip(DEMAND_KEYS, (tr('integration.requested_p_mw'), tr('integration.requested_q_mvar'), tr('integration.supplied_p_mw'), tr('integration.supplied_q_mvar'), tr('integration.unsupplied_p_mw'), tr('integration.unsupplied_q_mvar'), tr('integration.balance_mw')))]),
        ("windings", tr('integration.windings'), [("t_s", tr('integration.time_s')), ("element", tr('integration.element_49fa9f')), ("end", tr('integration.terminal')), ("loading_pct", tr('integration.loading')), ("rating_ka", tr('integration.published_rating_ka'))]),
        ("interface", "interface", [("t_s", tr('integration.time_s')), ("element", "Enlace"), ("p_mw", tr('integration.p_leaving_interface_mw')), ("q_mvar", tr('integration.q_leaving_interface_mvar'))]),
        ("islands", tr('integration.islands'), [("t_s", tr('integration.time_s')), ("island_id", tr('integration.island')), ("demand_supplied_p_mw", tr('integration.supplied_demand_mw')), ("generation_p_mw", tr('integration.generation_mw')), ("loss_p_mw", tr('integration.losses_mw')), ("balance_p_mw", tr('integration.balance_mw'))]),
        ("coverage", tr('integration.coverage'), [("kind", tr('integration.quantity')), ("element", tr('integration.element_49fa9f')), ("known_hours", tr('integration.known_duration_h')), ("unknown_hours", tr('integration.unknown_duration_h')), ("violation_hours", tr('integration.outside_threshold_h'))])]


def qds_detail_rows(payload):
    """Aplana campos publicados sin resolver ecuaciones ni sustituir ausencias."""
    result = {key: [] for key, _, _ in qds_columns()}
    identities = {row.get("object_id"): row for field in ("voltage_by_bus", "branch_loading") for row in payload.get(field, [])}
    def identity_label(ident, row):
        return f"{row.get('name', ident)} [{ident}]" if ident else row.get("name")
    def sample(values, index):
        return values[index] if isinstance(values, list) and index < len(values) else None
    for index, time in enumerate(payload.get("time", [])):
        t_s = time.get("t_s")
        result["demand"].append({"t_s": t_s, **{key: sample(payload.get(key), index) for key in DEMAND_KEYS}})
        aggregate = sample(payload.get("interface_220kv"), index) or {}
        result["interface"].append({"t_s": t_s, "element": tr('integration.aggregate_220_kv_interface'), "p_mw": aggregate.get("p_out_of_interface_mw"), "q_mvar": aggregate.get("q_out_of_interface_mvar")})
        for link in payload.get("interface_links", []):
            result["interface"].append({"t_s": t_s, "element": link.get("circuit", link.get("link_id")),
                                    "p_mw": sample(link.get("p_out_of_interface_mw"), index), "q_mvar": sample(link.get("q_out_of_interface_mvar"), index)})
        for branch in payload.get("branch_loading", []):
            for end, loading in (sample(branch.get("end_loading_pct"), index) or {}).items():
                result["windings"].append({"t_s": t_s, "element": identity_label(branch.get("object_id"), branch), "end": end,
                                           "loading_pct": loading, "rating_ka": branch.get("end_ratings_ka", {}).get(end)})
        for island in sample(payload.get("balance_by_island"), index) or []:
            result["islands"].append({"t_s": t_s, **island})
    for kind, field in ((tr('integration.voltage'), "voltage_out_of_band_hours_by_bus"), (tr('integration.load'), "branch_overload_hours_by_branch")):
        for element, coverage in payload.get("summary", {}).get(field, {}).items():
            result["coverage"].append({"kind": kind, "element": identity_label(element, identities.get(element, {})), **coverage})
    return result


def export_qds_tables(payload, destination):
    """Paquete CSV por dominio, precisión completa y unidades en cada encabezado."""
    rows = qds_detail_rows(payload)
    destination = Path(destination)
    for key, _, columns in qds_columns():
        target = destination if key == "demand" else destination.with_name(destination.stem + "_" + key + ".csv")
        with target.open("x", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([label for _, label in columns])
            writer.writerows([[row.get(column) for column, _ in columns] for row in rows[key]])


def build_request(context, *, profile_name="S1_diaria_15min", profile_kind="builtin_ieee39",
                  manifest_path="", series_name="", model_path="power_machine/cases/ieee39/model.json", step_s=None,
                  horizon_s=None, dispatch="reference", policy="continue", output_dir="qds"):
    """Crea una petición serializable, el adaptador verifica modelo y hashes."""
    snapshot = copy.deepcopy(context.get_snapshot())
    profile = {"kind": profile_kind}
    if profile_kind == "builtin_ieee39":
        profile["name"] = profile_name
    else:
        profile.update(manifest_path=manifest_path, name=series_name, model_path=model_path)
    configuration = {"dispatch": dispatch, "policy": policy, "warm_start": True}
    if step_s is not None:
        configuration["step_s"] = step_s
    if horizon_s is not None:
        configuration["horizon_s"] = horizon_s
    return {"schema": "pm-parity-request-v1", "module": MODULE, "root": str(context.root),
            "snapshot": snapshot, "input": {"profile": profile, "configuration": configuration,
                                                "output_dir": output_dir}}


class PanelController:
    """Coordina mensajes del trabajador y conserva el resultado del panel."""
    def __init__(self, context, on_update=None):
        self.context = context
        self.on_update = on_update or (lambda result: None)
        self.result = None
        self.job = None
        self.stale_reason = None

    def submit(self, **kwargs):
        self.stale_reason = None
        request = build_request(self.context, **kwargs)
        self.job = self.context.submit_job(MODULE, request, self.on_message)
        self.context.log("info", tr('integration.quasi_dynamic_simulation_no_ref_validation'))
        return request

    def on_message(self, message):
        if isinstance(message, (tuple, list)) and message:
            if message[0] == "progress":
                self.on_update({"progress": message[1]})
                return
            if message[0] == "error":
                self.on_update({"error": str(message[1])})
                return
            if message[0] == "result":
                message = message[1]
        if isinstance(message, dict) and message.get("schema") == RESULT_SCHEMA:
            self.result = message
            self.context.publish_result(message)
            self.on_update(message)
        else:
            self.on_update({"error": tr('integration.incompatible_qds_response')})

    def cancel(self):
        if self.job is not None and callable(getattr(self.job, "cancel", None)):
            self.job.cancel()
        else:
            self.context.log("warning", tr('integration.cancellation_is_requested_through_the_application_job'))

    def mark_stale(self, reason: str):
        self.stale_reason = str(reason)
        self.on_update({"stale": self.stale_reason})


def _bus_label(row):
    return element_label({**row, "id": row.get("object_id", row.get("id"))}, {"buses": []})


def create_panel(parent, context) -> ttk.Frame:
    """Construye controles desplazables y resultados legibles en 1000 x 600."""
    frame, form, results = study_panel(parent)
    stale_var = tk.StringVar(frame, value=tr("panel.qds.not_validated"))
    status_var = tk.StringVar(frame, value=tr("panel.qds.initial"))
    profile_var = tk.StringVar(frame, value="S1_diaria_15min")
    dispatch_var = tk.StringVar(frame, value="reference")
    step_var = tk.StringVar(frame, value="")
    horizon_var = tk.StringVar(frame, value="")
    manifest_var = tk.StringVar(frame, value="")
    series_var = tk.StringVar(frame, value="")
    model_path_var = tk.StringVar(frame, value="power_machine/cases/ieee39/model.json")
    bus_var = tk.StringVar(frame, value="")
    variable_var = tk.StringVar(frame, value=tr('integration.voltage_p_u'))

    fields = (("profile", profile_var, ("S1_diaria_15min", "S2_semanal_1h", "S3_rampa_estres_1min")),
              ("dispatch", dispatch_var, ("reference", "proportional")), ("step", step_var, None),
              ("horizon", horizon_var, None), ("manifest", manifest_var, None),
              ("series", series_var, None), ("model", model_path_var, None))
    field_labels = {}
    for row, (key, variable, values) in enumerate(fields):
        field_labels[key] = ttk.Label(form)
        field_labels[key].grid(row=row, column=0, sticky="w", pady=2)
        widget = (ttk.Combobox(form, textvariable=variable, values=values, state="readonly") if values
                  else ttk.Entry(form, textvariable=variable))
        widget.grid(row=row, column=1, sticky="ew", pady=2)
    manifest_note = ttk.Label(form, wraplength=290)
    manifest_note.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(4, 6))
    form.columnconfigure(1, weight=1)

    pages = result_pages(results)
    summary_page = ttk.Frame(pages)
    pages.add(summary_page, text="")
    summary_page.columnconfigure(0, weight=1)
    summary_page.rowconfigure(0, weight=1)
    detail_tables = {}
    for key, title, columns in qds_columns():
        page = ttk.Frame(pages)
        pages.add(page, text="")
        detail_tables[key] = ResultTable(page, columns, context.root)
        detail_tables[key].pack(fill="both", expand=True)
    summary = ttk.Treeview(summary_page, columns=("value",), show="tree headings", height=6)
    summary.heading("#0", text="")
    summary.heading("value", text="")
    summary.column("#0", width=230, stretch=True)
    summary.column("value", width=180, stretch=True)
    summary.grid(row=0, column=0, columnspan=3, sticky="nsew")
    bus_label = ttk.Label(form); bus_label.grid(row=30, column=0, sticky="w", pady=(5, 0))
    bus_box = ttk.Combobox(form, textvariable=bus_var, state="readonly")
    bus_box.grid(row=31, column=0, columnspan=3, sticky="ew")
    variable_label = ttk.Label(form); variable_label.grid(row=32, column=0, sticky="w")
    variable_box = ttk.Combobox(form, textvariable=variable_var,
                                values=(tr('integration.voltage_p_u'), tr('integration.voltage_kv'), tr('integration.losses_mw'), tr('integration.interface_exchange_mw')),
                                state="readonly")
    variable_box.grid(row=33, column=0, columnspan=3, sticky="ew")

    plot_holder = ttk.Frame(frame.plot_area)
    plot_holder.grid(row=0, column=0, columnspan=3, sticky="nsew", pady=(4, 0))
    plot_state = {"canvas": None, "figure": None, "bus_labels": {}}

    def show_summary(result):
        for item in summary.get_children():
            summary.delete(item)
        payload = result.get("payload", {})
        qss = payload.get("qss_summary", {})
        values = ((tr('integration.status'), STATUS_TEXT.get(result.get("status"), result.get("status"))),
                  (tr('integration.computed_time_points'), f"{payload.get('completed_count', 0)} / {payload.get('total_count', 0)}"),
                  (tr('integration.energy_losses_mwh'), qss.get("loss_energy", {}).get("mwh")),
                  (tr('integration.busbars_outside_band'), payload.get("summary", {}).get("voltage_violation_bus_count")),
                  (tr('integration.overloaded_branches'), payload.get("summary", {}).get("overloaded_branch_count")),
                  (tr('integration.unavailable_ratings'), payload.get("summary", {}).get("branches_without_rating")))
        for label, value in values:
            summary.insert("", "end", text=label, values=(tr('integration.no_data') if value is None else value,))
        for key, rows in qds_detail_rows(payload).items():
            detail_tables[key].set_rows(rows)
        base = {row["id"]: row for row in context.get_snapshot()["model"].get("buses", [])}
        labels = {element_label({**base.get(row.get("object_id"), {}), **row, "id": row.get("object_id")}, {"buses": []}): row.get("object_id") for row in payload.get("voltage_by_bus", [])}
        plot_state["bus_labels"] = labels
        bus_box.configure(values=tuple(labels))
        if labels:
            bus_var.set(next(iter(labels)))

    def draw_plot(*_):
        result = controller.result
        if result is None:
            return
        payload = result.get("payload", {})
        times_h = [row["t_s"] / 3600.0 for row in payload.get("time", [])]
        if not times_h:
            return
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            status_var.set(tr('integration.matplotlib_is_unavailable_for_this_plot'))
            return
        if plot_state["canvas"] is not None:
            plot_state["canvas"].get_tk_widget().destroy()
        figure = Figure(layout="constrained", figsize=(5.2, 2.8), dpi=100)
        axis = figure.add_subplot(111)
        style_plot(figure, axis)
        selected = variable_var.get()
        selection = variable_box.current()
        if selection in (0,1):
            selected_id = plot_state["bus_labels"].get(bus_var.get())
            row = next((item for item in payload.get("voltage_by_bus", []) if item.get("object_id") == selected_id), None)
            values = row.get("u_pu" if "p.u." in selected else "u_kv", []) if row else []
            axis.plot(times_h, values, label=row.get("name", tr("panel.qds.no_bus")) if row else tr("panel.qds.no_bus"), color=theme.color("accent"))
            axis.set_ylabel("U [p.u.]" if "p.u." in selected else "U [kV]")
        elif selection == 2:
            axis.plot(times_h, payload.get("loss_p_mw", []), label=tr("panel.qds.losses"), color=theme.color("warn"))
            axis.set_ylabel(tr('integration.losses_mw'))
        else:
            axis.plot(times_h, [row.get("p_out_of_interface_mw") for row in payload.get("interface_220kv", [])],
                      label=tr("panel.qds.interface_out"), color=theme.color("accent"))
            axis.set_ylabel(tr('integration.interface_output_p_mw'))
        axis.set_xlabel(tr('integration.time_h'))
        format_axes(axis)
        axis.grid(True, alpha=.3)
        axis.legend(loc="best")
        tk_canvas = FigureCanvasTkAgg(figure, master=plot_holder)
        tk_canvas.draw()
        tk_canvas.get_tk_widget().grid(row=0, column=0, sticky="nsew")
        plot_holder.columnconfigure(0, weight=1)
        plot_state.update(canvas=tk_canvas, figure=figure)

    def update(message):
        if "progress" in message:
            value = message["progress"]
            status_var.set(tr('integration.computing_time_points', value0=value.get('completed', 0), value1=value.get('total', 0)))
        elif "error" in message:
            status_var.set(message["error"])
        elif "stale" in message:
            stale_var.set(tr('integration.stale_result') + message["stale"])
        elif message.get("schema") == RESULT_SCHEMA:
            show_summary(message)
            status_var.set(STATUS_TEXT.get(message.get("status"), message.get("status", tr('integration.no_state'))))
            stale_var.set(tr('integration.no_ref_validation_f85232'))
            draw_plot()

    controller = PanelController(context, update)

    def submit():
        try:
            step = float(step_var.get()) if step_var.get().strip() else None
            horizon = float(horizon_var.get()) if horizon_var.get().strip() else None
            external = manifest_var.get().strip()
            controller.submit(profile_name=profile_var.get(),
                              profile_kind="csv_manifest" if external else "builtin_ieee39",
                              manifest_path=external, series_name=series_var.get().strip(),
                              model_path=model_path_var.get().strip(), step_s=step, horizon_s=horizon,
                              dispatch=dispatch_var.get())
        except (ValueError, TypeError, OSError) as exc:
            status_var.set(str(exc))
            context.log("error", str(exc))

    def export_csv():
        if controller.result is None:
            status_var.set(tr('integration.there_is_no_result_to_export'))
            return
        target = filedialog.asksaveasfilename(parent=frame, initialdir=context.root,
                                              defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if not target:
            return
        root = Path(context.root).resolve()
        destination = Path(target).resolve()
        try:
            if not destination.is_relative_to(root):
                raise ValueError(tr('integration.export_must_remain_within_the_project_folder'))
            payload = controller.result["payload"]
            export_qds_tables(payload, destination)
            status_var.set(tr('integration.csv_exported_aedab9') + str(destination.relative_to(root)))
        except (OSError, ValueError) as exc:
            status_var.set(str(exc))
            context.log("error", str(exc))

    action_button(frame.actions, 'run', text=tr('integration.run'), command=submit).grid(row=1, column=0, sticky="w", pady=3)
    action_button(form, 'cancel', text=tr('integration.cancel'), command=controller.cancel).grid(row=len(fields)+1, column=1, sticky="w", pady=3)
    action_button(results, 'export', text=tr('integration.export_csv'), command=export_csv).grid(row=6, column=0, sticky="w", pady=(4, 0))
    ttk.Label(results, textvariable=status_var, wraplength=560).grid(row=1, column=0, sticky="w")
    ttk.Label(form, textvariable=stale_var, wraplength=920).grid(row=20, column=0, columnspan=3, sticky="w", pady=(4, 0))
    bus_box.bind("<<ComboboxSelected>>", draw_plot)
    variable_box.bind("<<ComboboxSelected>>", draw_plot)
    frame.columnconfigure(0, weight=0)
    frame.columnconfigure(2, weight=1)
    frame.rowconfigure(0, weight=1)
    results.columnconfigure(0, weight=1)
    frame.controller = controller
    frame.results.columnconfigure(0, weight=1)
    frame.results.rowconfigure(0, weight=1)
    frame.plot_area.columnconfigure(0, weight=1)
    frame.plot_area.rowconfigure(0, weight=1)
    def preflight_inputs():
        external = manifest_var.get().strip()
        profile = {"kind": "csv_manifest", "manifest_path": external, "name": series_var.get(), "model_path": model_path_var.get()} if external else {"kind": "builtin_ieee39", "name": profile_var.get()}
        return {**verified_qds_inputs(context.root, context.get_snapshot()["model"], profile), "input": {"profile": profile}}
    frame.preflight_inputs = preflight_inputs
    def retranslate():
        selected_index = variable_box.current()
        variable_box.configure(values=tuple(tr(key) for key in (
            'integration.voltage_p_u','integration.voltage_kv','integration.losses_mw','integration.interface_exchange_mw')))
        variable_box.current(max(0,selected_index))
        for key, label in field_labels.items(): label.configure(text=tr("panel.qds." + key))
        manifest_note.configure(text=tr("panel.qds.manifest_note")); bus_label.configure(text=tr("panel.qds.bus")); variable_label.configure(text=tr("panel.qds.variable"))
        for tab, key in zip(pages.tabs(), ("summary", *[key for key, _, _ in qds_columns()])):
            pages.tab(tab, text=tr("panel.qds.tab." + key))
        summary.heading("#0", text=tr("panel.qds.quantity")); summary.heading("value", text=tr("panel.qds.result"))
    frame.retranslate = retranslate
    panel = finish_panel(frame, context, controller, tr("panel.qds.scope"))
    retranslate()
    return panel
