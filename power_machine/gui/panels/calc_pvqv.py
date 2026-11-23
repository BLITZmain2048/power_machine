"""Panel PV/QV, autónomo y sin ecuaciones eléctricas en tkinter."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
import csv
import math
from dataclasses import asdict
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from power_machine.voltage_stability import PVOptions, QVOptions
from ..i18n import tr
from ..theme import theme

from .panel_widgets import (format_axes, study_panel, finish_panel, element_label, AdvancedOptions,
                            ResultTable, result_pages, panel_retranslate, style_plot)

PANEL_SPEC = {"id": "calc_pvqv", "title": tr("panel.pvqv.title"), "title_key": "panel.pvqv.title",
              "api_version": 1, "factory": "create_panel", "order": 35}
STATUS_TEXT = {"completed": "completed", "partial": "partial", "invalid_input": "invalid_input",
               "unsupported": "unsupported", "error": "error", "cancelled": "cancelled"}


def build_request(context, inputs: dict, options: PVOptions | QVOptions) -> dict:
    """Construye una solicitud aislada desde el snapshot del contexto."""
    return {"schema": "power_machine-voltage-stability-request-v1", "module": "voltage_stability",
            "root": str(context.root), "snapshot": copy.deepcopy(context.get_snapshot()),
            "input": copy.deepcopy(inputs), "options": asdict(options)}


class PanelController:
    """Puente ensayable entre widgets y el trabajador del integrador."""
    def __init__(self, context, on_update=None):
        self.context = context
        self.on_update = on_update or (lambda message: None)
        self.result = None
        self.job = None

    def submit(self, inputs: dict, options: PVOptions | QVOptions):
        request = build_request(self.context, inputs, options)
        self.job = self.context.submit_job("voltage_stability", request, self.on_message)
        self.context.log("info", tr("panel.pvqv.log_started"))
        return request

    def on_message(self, message):
        if isinstance(message, dict) and message.get("schema") == "power_machine-voltage-stability-result-v1":
            self.result = message
        elif isinstance(message, (list, tuple)) and message and message[0] == "result":
            self.result = message[1]
        elif isinstance(message, (list, tuple)) and message and message[0] == "log":
            self.context.log(message[1], message[2])
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
            self.context.log("warning", tr("panel.common.cancel_from_job_control"))


def _station_label(item, buses=None):
    from ..presentation import station
    return station(item) or station((buses or {}).get(item.get("bus"), {}))


def _bus_label(bus):
    return element_label(bus, {"buses": [bus]})


def _load_label(load, buses):
    return element_label(load, {"buses": list(buses.values())})


def _scrollable(parent):
    """Formulario desplazable, mantiene resultados fuera de la zona de scroll."""
    shell = ttk.Frame(parent)
    canvas = tk.Canvas(shell, highlightthickness=0)
    bar = ttk.Scrollbar(shell, orient="vertical", command=canvas.yview)
    canvas.configure(yscrollcommand=bar.set)
    form = ttk.Frame(canvas)
    window = canvas.create_window((0, 0), window=form, anchor="nw")
    form.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
    canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
    canvas.grid(row=0, column=0, sticky="nsew")
    bar.grid(row=0, column=1, sticky="ns")
    shell.columnconfigure(0, weight=1)
    shell.rowconfigure(0, weight=1)
    return shell, form



def pv_detail_rows(payload):
    """Valores publicados por el motor, sin agregación eléctrica en la interfaz."""
    rows = []
    for index, point in enumerate(payload.get("points", [])):
        for key in ("q_limited_generator_ids", "reactive_limit_mode", "real_generator_limits"):
            if key in point:
                rows.append({"point": index, "field": key, "value": point[key], "unit": tr("panel.common.declaration")})
        for key, value in point.get("load_breakdown", {}).items():
            rows.append({"point": index, "field": key, "value": value, "unit": "Mvar" if key.endswith("mvar") else "MW" if key.endswith("mw") else "ID"})
        for link in point.get("interface_220kv", {}).get("links", []):
            for key, unit in (("p_out_of_interface_mw", "MW"), ("q_out_of_interface_mvar", "Mvar")):
                rows.append({"point": index, "field": str(link.get("circuit")) + " " + key, "value": link.get(key), "unit": unit})
    for key in ("selection", "selected_loads", "excluded_unresolved_loads", "initialization_mode", "reactive_limit_mode", "real_generator_limits"):
        if key in payload:
            rows.append({"point": tr("panel.common.scope"), "field": key, "value": payload[key], "unit": tr("panel.common.declaration")})
    return rows


def create_panel(parent, context) -> ttk.Frame:
    frame, form, result_box = study_panel(parent)
    snapshot = context.get_snapshot()
    model = snapshot.get("model", {})
    buses = {item["id"]: item for item in model.get("buses", []) if not item.get("outserv", 0)}
    loads = [item for item in model.get("loads", []) if not item.get("outserv", 0)]
    bus_labels = {_bus_label(item): item["id"] for item in buses.values()}
    load_labels = {_load_label(item, buses): item["id"] for item in loads}
    kind = tk.StringVar(master=frame, value="PV")
    dispatch = tk.StringVar(master=frame, value="reference")
    q_limits = tk.BooleanVar(master=frame, value=True)
    step_mw = tk.StringVar(master=frame, value="10")
    min_step_mw = tk.StringVar(master=frame, value="0.25")
    max_increase_mw = tk.StringVar(master=frame, value="500")
    power_factor = tk.StringVar(master=frame, value="")
    station_filter = tk.StringVar(master=frame, value=tr("panel.common.all_stations"))
    qv_bus_label = tk.StringVar(master=frame, value=next(iter(bus_labels), ""))
    qv_start = tk.StringVar(master=frame, value="1.10")
    qv_end = tk.StringVar(master=frame, value="0.80")
    qv_step = tk.StringVar(master=frame, value="0.01")
    qv_q_min = tk.StringVar(master=frame, value="")
    qv_q_max = tk.StringVar(master=frame, value="")
    initialization = tk.StringVar(frame, tr("panel.pvqv.initialization_flat"))
    reactive_mode = tk.StringVar(frame, tr("panel.pvqv.reactive_idealized"))
    initialization_choices = (tr("panel.pvqv.initialization_flat"), tr("panel.pvqv.initialization_warm"))
    reactive_choices = (tr("panel.pvqv.reactive_idealized"), tr("panel.pvqv.reactive_generator_limits"))
    for row, title, var, choices in ((16, "initialization", initialization, initialization_choices),
                                     (17, "reactive_mode", reactive_mode, reactive_choices)):
        ttk.Label(form, text=tr("panel.pvqv." + title)).grid(row=row, column=0, sticky="w")
        ttk.Combobox(form, textvariable=var, values=choices, state="readonly").grid(row=row, column=1, sticky="ew")
    ttk.Label(form, text=tr("panel.pvqv.selection_note"), wraplength=300).grid(row=18, column=0, columnspan=2)
    summary = tk.StringVar(master=frame, value=tr("panel.pvqv.initial_summary"))
    stale = tk.StringVar(master=frame, value="")

    ttk.Label(form, text=tr("panel.pvqv.curve_type")).grid(row=0, column=0, sticky="w")
    ttk.Combobox(form, textvariable=kind, values=("PV", "QV"), state="readonly", width=16).grid(row=0, column=1, sticky="ew")
    ttk.Label(form, text=tr("panel.pvqv.loads")).grid(row=1, column=0, sticky="nw")
    load_list = tk.Listbox(form, selectmode="extended", exportselection=False, height=6)
    for label in load_labels:
        load_list.insert("end", label)
    from power_machine.elements import load_pq_mw
    has_scopes = any(item.get("scope") == "sistema_interno" for item in loads)
    default_ids = {item["id"] for item in loads if (not has_scopes or item.get("scope") == "sistema_interno")
                   and load_pq_mw(item)[0] > 0}
    for index, ident in enumerate(load_labels.values()):
        if ident in default_ids:
            load_list.selection_set(index)
    if load_list.curselection():
        load_list.see(load_list.curselection()[0])
    load_list.grid(row=1, column=1, sticky="ew")
    stations = (tr("panel.common.all_stations"), *sorted({_station_label(item, buses) for item in loads}))
    ttk.Label(form, text=tr("panel.pvqv.select_station")).grid(row=2, column=0, sticky="w")
    ttk.Combobox(form, textvariable=station_filter, values=stations, state="readonly").grid(row=2, column=1, sticky="ew")
    ttk.Label(form, text=tr("panel.pvqv.monitored_buses")).grid(row=3, column=0, sticky="nw")
    monitor_list = tk.Listbox(form, selectmode="extended", exportselection=False, height=6)
    for label in bus_labels:
        monitor_list.insert("end", label)
    monitor_list.grid(row=3, column=1, sticky="ew")
    ttk.Label(form, text=tr("panel.pvqv.dispatch")).grid(row=4, column=0, sticky="w")
    dispatch_choices = (tr("panel.pvqv.dispatch_reference"), tr("panel.pvqv.dispatch_proportional"))
    dispatch.set(dispatch_choices[0])
    ttk.Combobox(form, textvariable=dispatch, values=dispatch_choices, state="readonly", width=16).grid(row=4, column=1, sticky="ew")
    ttk.Checkbutton(form, text=tr("panel.common.consider_q_limits"), variable=q_limits).grid(row=5, column=1, sticky="w")
    for row, text, variable in ((6, "initial_step", step_mw), (7, "minimum_step", min_step_mw),
                                (8, "maximum_increase", max_increase_mw), (9, "power_factor", power_factor),
                                (11, "qv_start", qv_start), (12, "qv_end", qv_end),
                                (13, "qv_step", qv_step),
                                (14, "q_min", qv_q_min), (15, "q_max", qv_q_max)):
        ttk.Label(form, text=tr("panel.pvqv." + text)).grid(row=row, column=0, sticky="w")
        ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew")
    ttk.Label(form, text=tr("panel.pvqv.qv_bus")).grid(row=10, column=0, sticky="w")
    ttk.Combobox(form, textvariable=qv_bus_label, values=tuple(bus_labels), state="readonly").grid(row=10, column=1, sticky="ew")
    form.columnconfigure(1, weight=1)

    pages = result_pages(result_box)
    curve_page = ttk.Frame(pages)
    curve_page.columnconfigure(0, weight=1)
    curve_page.rowconfigure(0, weight=1)
    pages.add(curve_page, text=tr("panel.pvqv.curve"))
    detail_page = ttk.Frame(pages)
    pages.add(detail_page, text=tr("panel.pvqv.demand_interface"))
    details = ResultTable(detail_page, [("point", tr("panel.common.point")), ("field", tr("panel.common.quantity_circuit")), ("value", tr("panel.common.value")), ("unit", tr("panel.common.unit"))], context.root)
    details.pack(fill="both", expand=True)
    table = ttk.Treeview(curve_page, columns=("x", "u", "q", "state"), show="headings", height=12)
    table_headers = (("x", "x"), ("u", "voltage"), ("q", "source_q"), ("state", "state"))
    for column, title in table_headers:
        table.heading(column, text=tr("panel.pvqv." + title))
        table.column(column, width=125, stretch=True)
    table.grid(row=0, column=0, sticky="nsew")
    result_box.columnconfigure(0, weight=1)
    result_box.rowconfigure(0, weight=1)
    plot_holder = ttk.Frame(frame.plot_area)
    plot_holder.grid(row=0, column=0, sticky="nsew")
    plot_canvas = {"widget": None, "axes": None}

    def draw(payload):
        if plot_canvas["widget"] is None:
            try:
                from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
                from matplotlib.figure import Figure
                figure = Figure(layout="constrained", figsize=(5.4, 2.6), dpi=100)
                plot_canvas["axes"] = figure.add_subplot(111)
                plot_canvas["widget"] = FigureCanvasTkAgg(figure, master=plot_holder)
                plot_canvas["widget"].get_tk_widget().grid(row=0, column=0, sticky="nsew")
                plot_holder.columnconfigure(0, weight=1)
            except Exception as exc:
                summary.set(tr("panel.common.plot_error", reason=exc))
                return
        axes = plot_canvas["axes"]
        axes.clear()
        if payload.get("curve_type") == "PV":
            points = payload.get("points", [])
            if points:
                axes.plot([row.get("load_breakdown", {}).get("selected_p_mw") for row in points], [row["monitored_buses"][0]["u_pu"] if row["monitored_buses"] else None for row in points], marker="o")
                axes.set_xlabel(tr("panel.pvqv.selected_demand"))
                axes.set_ylabel(tr("panel.pvqv.voltage"))
        else:
            points = payload.get("points", [])
            axes.plot([row["voltage_set_pu"] for row in points], [row["q_source_mvar"] for row in points], marker="o")
            axes.set_xlabel(tr("panel.pvqv.voltage_setpoint"))
            axes.set_ylabel(tr("panel.pvqv.fictitious_source_q"))
        format_axes(axes)
        style_plot(plot_canvas["widget"].figure, axes)
        plot_canvas["widget"].draw_idle()

    def update(message):
        if isinstance(message, (tuple, list)) and message and message[0] == "progress":
            state = message[1]
            summary.set(tr("panel.common.calculating_count", phase=state.get('phase', tr('panel.common.calculating')), completed=state.get('completed', 0), total=state.get('total', 0)))
            return
        if not isinstance(message, dict) or message.get("schema") != "power_machine-voltage-stability-result-v1":
            return
        for item in table.get_children():
            table.delete(item)
        payload = message.get("payload", {})
        details.set_rows(pv_detail_rows(payload))
        if payload.get("curve_type") == "PV":
            for point in payload.get("points", []):
                u_value = point["monitored_buses"][0]["u_pu"] if point.get("monitored_buses") else None
                table.insert("", "end", values=(point.get("load_breakdown", {}).get("selected_p_mw"), u_value, "", tr("panel.common.converged") if point.get("converged") else tr("panel.common.not_converged")))
        else:
            for point in payload.get("points", []) + payload.get("failed_points", []):
                table.insert("", "end", values=(point.get("voltage_set_pu"), point.get("voltage_actual_pu"), point.get("q_source_mvar"), tr("panel.common.converged") if point.get("converged") else tr("panel.common.not_converged")))
        summary.set(tr("panel.pvqv.result_summary", status=tr("panel.common." + STATUS_TEXT.get(message.get("status"), "error"))))
        draw(payload)

    controller = PanelController(context, update)

    def selected_ids(widget, labels):
        return [labels[widget.get(index)] for index in widget.curselection()]

    def load_selection():
        ids = (selected_ids(load_list, load_labels) if station_filter.get() == tr("panel.common.all_stations") else
               [item["id"] for item in loads if _station_label(item, buses) == station_filter.get()])
        return {"kind": "list", "load_ids": ids} if ids else None

    def submit():
        try:
            if kind.get() == "PV":
                monitored = selected_ids(monitor_list, bus_labels)
                inputs = {"curve_type": "PV"}
                if load_selection() is not None:
                    inputs["load_selection"] = load_selection()
                if monitored:
                    inputs["monitored_bus_ids"] = monitored
                pf_text = power_factor.get().strip()
                initial_step, minimum_step, maximum_increase = float(step_mw.get()), float(min_step_mw.get()), float(max_increase_mw.get())
                if not all(math.isfinite(value) and value > 0 for value in (initial_step, minimum_step, maximum_increase)):
                    raise ValueError(tr("panel.pvqv.invalid_pv_steps"))
                if minimum_step > initial_step:
                    raise ValueError(tr("panel.pvqv.minimum_step_too_large"))
                factor = float(pf_text) if pf_text else None
                if factor is not None and not (math.isfinite(factor) and 0 < factor <= 1):
                    raise ValueError(tr("panel.pvqv.invalid_power_factor"))
                options = PVOptions(initial_step_mw=initial_step, minimum_step_mw=minimum_step,
                                    maximum_increase_mw=maximum_increase, power_factor=factor,
                                    generator_dispatch="reference" if dispatch.get() == dispatch_choices[0] else "proportional",
                                    initialization_mode="flat" if initialization.get() == initialization_choices[0] else "warm_from_last", q_limits=q_limits.get())
            else:
                inputs = {"curve_type": "QV", "qv_bus_id": bus_labels[qv_bus_label.get()]}
                low_text, high_text = qv_q_min.get().strip(), qv_q_max.get().strip()
                start, end, step = float(qv_start.get()), float(qv_end.get()), float(qv_step.get())
                if not all(math.isfinite(value) and value > 0 for value in (start, end, step)) or start <= end:
                    raise ValueError(tr("panel.pvqv.invalid_qv_range"))
                options = QVOptions(voltage_start_pu=start, voltage_end_pu=end, voltage_step_pu=step,
                                    reactive_limit_mode="idealized" if reactive_mode.get() == reactive_choices[0] else "active_generator_limits", source_q_min_mvar=float(low_text) if low_text else None,
                                    source_q_max_mvar=float(high_text) if high_text else None)
            stale.set("")
            controller.submit(inputs, options)
        except (ValueError, KeyError, TypeError) as exc:
            summary.set(str(exc))
            context.log("error", str(exc))

    def export_csv():
        if not controller.result:
            summary.set(tr("panel.common.no_result_export"))
            return
        target = filedialog.asksaveasfilename(parent=frame, initialdir=str(context.root), defaultextension=".csv",
                                              filetypes=(("CSV", "*.csv"),))
        if not target:
            return
        path, root = Path(target).resolve(), Path(context.root).resolve()
        if not path.is_relative_to(root):
            summary.set(tr("panel.common.export_inside_workspace"))
            return
        payload = controller.result.get("payload", {})
        with path.open("w", encoding="utf-8", newline="") as stream:
            if payload.get("curve_type") == "PV":
                writer = csv.DictWriter(stream, fieldnames=("total_load_p_mw", "increase_mw", "load_factor", "bus_id", "u_pu", "u_kv", "converged"))
                writer.writeheader()
                for point in payload.get("points", []):
                    for bus in point.get("monitored_buses", []):
                        writer.writerow({**{key: point.get(key) for key in ("total_load_p_mw", "increase_mw", "load_factor", "converged")}, **{key: bus.get(key) for key in ("bus_id", "u_pu", "u_kv")}})
            else:
                writer = csv.DictWriter(stream, fieldnames=("voltage_set_pu", "voltage_actual_pu", "q_source_mvar", "converged"))
                writer.writeheader()
                writer.writerows({key: point.get(key) for key in writer.fieldnames} for point in payload.get("points", []))
        summary.set(tr("panel.common.csv_exported", path=path.relative_to(root)))

    run_button = action_button(form, 'run', text=tr('panel.pvqv.run_curve'), command=submit)
    cancel_button = action_button(form, 'cancel', text=tr('panel.common.cancel'), command=controller.cancel)
    export_button = action_button(form, 'export', text=tr('panel.common.export_csv'), command=export_csv)
    run_button.grid(row=20, column=0, sticky="w")
    cancel_button.grid(row=21, column=0, sticky="e")
    export_button.grid(row=22, column=1, sticky="w")
    ttk.Label(form, textvariable=stale, foreground=theme.color("error"), wraplength=600).grid(row=23, column=0, columnspan=2, sticky="w")
    ttk.Label(form, textvariable=summary, wraplength=850).grid(row=24, column=0, columnspan=2, sticky="w")
    frame.columnconfigure(0, weight=1)
    frame.columnconfigure(1, weight=2)
    frame.rowconfigure(0, weight=1)

    def mark_stale(reason: str):
        stale.set(tr("panel.common.stale_result", reason=reason))
    frame.controller = controller
    frame.results.columnconfigure(0, weight=1)
    frame.results.rowconfigure(0, weight=1)
    frame.plot_area.columnconfigure(0, weight=1)
    frame.plot_area.rowconfigure(0, weight=1)
    def preflight_inputs():
        return {"input": {"curve_type": kind.get(), "qv_bus_id": bus_labels.get(qv_bus_label.get()), "load_selection": load_selection()},
                "options": {"power_factor": float(power_factor.get()) if power_factor.get().strip() else None,
                            "generator_dispatch": "reference" if dispatch.get() == dispatch_choices[0] else "proportional",
                            "initialization_mode": "flat" if initialization.get() == initialization_choices[0] else "warm_from_last",
                            "reactive_limit_mode": "idealized" if reactive_mode.get() == reactive_choices[0] else "active_generator_limits"}}
    frame.preflight_inputs = preflight_inputs
    def retranslate():
        run_button.configure(text=tr("panel.pvqv.run_curve"))
        cancel_button.configure(text=tr("panel.common.cancel"))
        export_button.configure(text=tr("panel.common.export_csv"))
        for column, title in table_headers:
            table.heading(column, text=tr("panel.pvqv." + title))
        pages.tab(curve_page, text=tr("panel.pvqv.curve"))
        pages.tab(detail_page, text=tr("panel.pvqv.demand_interface"))
    panel_retranslate(frame, retranslate)
    return finish_panel(frame, context, controller, tr('panel.pvqv.scope'))
