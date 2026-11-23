"""Panel de indicadores RMS, autónomo y sin ecuaciones en tkinter."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
import csv
import json
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from power_machine.dyn_metrics.analysis import REFERENCE_PROFILE
from power_machine.dyn_metrics.results import export_indicators_csv

from ..i18n import tr
from ..theme import theme
from .panel_widgets import (human_status, format_axes, study_panel, finish_panel, element_label,
                            AdvancedOptions, error_result, ResultTable, verified_metric_inputs, style_plot)

PANEL_SPEC = {"title_key": 'integration.rms_metrics', "id": "calc_met", "title": tr('integration.rms_metrics'),
              "api_version": 1, "factory": "create_panel", "order": 55}
STATUS_TEXT = {"completed": "completed", "partial": "partial", "unsupported": "unsupported",
               "invalid_input": "invalid_input", "error": "error", "cancelled": "cancelled"}


def metric_detail_rows(result):
    """Presenta cobertura y ajuste publicados, conservando amortiguamiento con signo."""
    units = {"time_outside_band_known_s": "s", "time_outside_band_unknown_s": "s",
             "time_outside_band_s": "s", "settling_known_s": "s", "settling_unknown_s": "s",
             "window_start_s": "s", "window_end_s": "s", "cycles_used": "ciclos",
             "peak_count": "picos", "fit_r2": "1", "residual_rms": "1", "damping_ratio": "1",
             "frequency_hz": "Hz", "method": "texto"}
    rows = []
    for signal in result.get("payload", {}).get("signals", []):
        for key, unit in units.items():
            if key in signal.get("metrics", {}):
                rows.append({"signal": signal.get("signal_id"), "metric": key,
                             "value": signal["metrics"][key], "unit": unit})
    for key in ("events_sha256", "options_sha256"):
        rows.append({"signal": "Procedencia", "metric": key, "value": result.get("provenance", {}).get(key), "unit": "SHA-256"})
    provenance = result.get("provenance", {})
    for name, digest in provenance.get("metadata_sha256", {}).items():
        rows.append({"signal": "Metadatos", "metric": name, "value": digest, "unit": "SHA-256"})
    for source in provenance.get("inputs", []):
        rows.append({"signal": "Entrada", "metric": source.get("path"), "value": source.get("sha256"), "unit": "SHA-256"})
    return rows


def build_request(context, run_directory, *, event_time_s=None, frequency_window_ms=200,
                  selected_signals=None, selected_element_id=None, profile=None):
    """Construye la solicitud sin resolver ni calcular dentro del panel."""
    from power_machine.dyn_metrics.schema import DynMetricOptions
    window_key = next(key for key in DynMetricOptions.__dataclass_fields__ if key.endswith("_window_ms"))
    return {
        "schema": "pm-parity-request-v1", "module": "dyn_metrics",
        "root": str(context.root), "snapshot": copy.deepcopy(context.get_snapshot()),
        "input": {"run_directory": str(run_directory), "selected_signals": list(selected_signals or []),
                  "selected_element_id": selected_element_id},
        "options": {"event_time_s": event_time_s, window_key: frequency_window_ms,
                    "profile": copy.deepcopy(profile or {})},
    }


class PanelController:
    """Adaptador del trabajador, ensayable sin crear una ventana Tk."""
    def __init__(self, context, on_update=None):
        self.context = context
        self.on_update = on_update or (lambda message: None)
        self.result = None
        self.job = None

    def submit(self, *, run_directory, event_time_s=None, frequency_window_ms=200,
               selected_signals=None, selected_element_id=None, profile=None):
        request = build_request(self.context, run_directory, event_time_s=event_time_s,
                                frequency_window_ms=frequency_window_ms, selected_signals=selected_signals,
                                selected_element_id=selected_element_id,
                                profile=profile)
        self.job = self.context.submit_job("dyn_metrics", request, self.on_message)
        self.context.log("info", tr('integration.rms_metrics_no_pf_validation'))
        return request

    def on_message(self, message):
        if isinstance(message, (tuple, list)) and message and message[0] == "error":
            self.on_update(error_result(message[1]))
            return
        if isinstance(message, dict) and message.get("schema") == "pm-parity-result-v1":
            self.result = message
            self.context.publish_result(message)
        elif isinstance(message, (list, tuple)) and message and message[0] == "result":
            self.result = message[1]
            self.context.publish_result(self.result)
        elif isinstance(message, (list, tuple)) and message and message[0] == "log":
            self.context.log(message[1], message[2])
            return
        self.on_update(message if self.result is None else self.result)

    def cancel(self):
        if self.job is not None and callable(getattr(self.job, "cancel", None)):
            self.job.cancel()
        else:
            self.context.log("warning", tr('integration.cancellation_is_requested_from_the_job_control'))


def _bus_labels(context):
    snapshot = context.get_snapshot()
    model = snapshot.get("model", {}) if isinstance(snapshot, dict) else {}
    values = {element_label(item, model): item.get("object_id", item.get("id")) for item in model.get("buses", [])}
    return values


def create_panel(parent, context) -> ttk.Frame:
    """Crea formulario desplazable, tabla, gráfico y acciones de exportación."""
    outer, body, results = study_panel(parent)
    run_directory = tk.StringVar(master=outer, value="power_machine/cases/ieee39/results/BASE")
    event_time = tk.StringVar(master=outer, value="0.0")
    frequency_window = tk.StringVar(master=outer, value="200")
    selected_signals = tk.StringVar(master=outer, value="")
    bus_labels = _bus_labels(context)
    selected_bus = tk.StringVar(master=outer, value="")
    profile_text = tk.StringVar(master=outer, value=json.dumps(REFERENCE_PROFILE, ensure_ascii=False))
    summary = tk.StringVar(master=outer)
    stale = tk.StringVar(master=outer, value="")

    labels = {}
    labels["run"] = ttk.Label(body); labels["run"].grid(row=0, column=0, sticky="w")
    ttk.Entry(body, textvariable=run_directory).grid(row=0, column=1, sticky="ew")
    labels["event"] = ttk.Label(body); labels["event"].grid(row=1, column=0, sticky="w")
    ttk.Entry(body, textvariable=event_time).grid(row=1, column=1, sticky="ew")
    labels["window"] = ttk.Label(body); labels["window"].grid(row=2, column=0, sticky="w")
    ttk.Combobox(body, textvariable=frequency_window, values=("100", "200", "500"), state="readonly").grid(row=2, column=1, sticky="ew")
    labels["bus"] = ttk.Label(body); labels["bus"].grid(row=3, column=0, sticky="w")
    ttk.Combobox(body, textvariable=selected_bus, values=sorted(bus_labels), state="readonly").grid(row=3, column=1, sticky="ew")
    labels["signals"] = ttk.Label(body); labels["signals"].grid(row=4, column=0, sticky="w")
    ttk.Entry(body, textvariable=selected_signals).grid(row=4, column=1, sticky="ew")
    labels["profile"] = ttk.Label(body); labels["profile"].grid(row=5, column=0, sticky="nw")
    profile_entry = tk.Text(body, height=5, width=70)
    profile_entry.insert("1.0", profile_text.get())
    profile_entry.grid(row=5, column=1, sticky="ew")

    columns = ("signal", "indicator", "state", "value", "unit", "threshold")
    pages = ttk.Notebook(results)
    pages.grid(row=0, column=0, sticky="nsew")
    value_page, metric_page = ttk.Frame(pages), ttk.Frame(pages)
    pages.add(value_page, text="")
    pages.add(metric_page, text="")
    value_page.columnconfigure(0, weight=1)
    value_page.rowconfigure(9, weight=1)
    metric_table = ResultTable(metric_page, [("signal", tr("panel.metrics.signal")), ("metric", tr("panel.metrics.field")), ("value", tr("panel.metrics.value_raw")), ("unit", tr("panel.metrics.unit"))], context.root)
    metric_table.pack(fill="both", expand=True)
    table = ttk.Treeview(value_page, columns=columns, show="headings", height=8)
    for key in columns:
        table.heading(key, text="")
        table.column(key, width=120, stretch=True)
    table.grid(row=9, column=0, columnspan=2, sticky="nsew", pady=(6, 0))

    plot_host = ttk.Frame(outer.plot_area)
    plot_host.grid(row=0, column=0, columnspan=2, sticky="nsew")
    body.rowconfigure(10, weight=1)
    chart = {"canvas": None, "axis": None}
    plot_unit = tk.StringVar(outer, "Hz")
    unit_box = ttk.Combobox(outer.plot_area, textvariable=plot_unit, state="readonly", width=15)
    unit_box.grid(row=0, column=0, sticky="w")
    plot_host.grid_configure(row=1)
    outer.plot_area.rowconfigure(1, weight=1)

    def draw(result):
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            return
        if chart["canvas"] is not None:
            chart["canvas"].get_tk_widget().destroy()
        figure = Figure(layout="constrained", figsize=(7, 2.4), dpi=100)
        axis = figure.add_subplot(111)
        rows = result.get("payload", {}).get("indicators", [])
        units = sorted({str(row.get("unit")) for row in rows if row.get("value") is not None})
        unit_box.configure(values=units)
        if plot_unit.get() not in units and units:
            plot_unit.set(units[0])
        evaluable = [row for row in rows if row.get("value") is not None and str(row.get("unit")) == plot_unit.get()][:6]
        style_plot(figure, axis)
        axis.set_ylabel(plot_unit.get())
        axis.bar(range(len(evaluable)), [row["value"] for row in evaluable], color=theme.color("accent"))
        axis.set_xticks(range(len(evaluable)), [human_status(row["indicator_id"].split(":")[-1]) for row in evaluable], rotation=20, ha="right", fontsize=8)
        axis.set_title(tr("panel.metrics.chart_title"), fontsize=9)
        format_axes(axis)
        chart["canvas"] = FigureCanvasTkAgg(figure, master=plot_host)
        chart["canvas"].draw()
        chart["canvas"].get_tk_widget().pack(fill="both", expand=True)

    def update(message):
        if isinstance(message, (list, tuple)) and message and message[0] == "progress":
            progress = message[1]
            summary.set(tr("panel.metrics.progress", phase=progress.get("phase", tr("panel.metrics.calculation")), completed=progress.get("completed", 0), total=progress.get("total", 0)))
            return
        if not isinstance(message, dict) or message.get("module") != "dyn_metrics":
            return
        for child in table.get_children():
            table.delete(child)
        for row in message.get("payload", {}).get("indicators", []):
            table.insert("", "end", values=(row.get("signal_id"), row.get("indicator_id").split(":")[-1],
                                               row.get("state"), row.get("value"), row.get("unit"), row.get("threshold")))
        metric_table.set_rows(metric_detail_rows(message))
        summary.set(tr("panel.metrics.status", status=tr("panel.metrics.status." + STATUS_TEXT.get(message.get("status"), "error"))))
        draw(message)

    controller = PanelController(context, update)
    unit_box.bind("<<ComboboxSelected>>", lambda e: draw(controller.result) if controller.result else None)

    def submit():
        try:
            raw_event = event_time.get().strip()
            event_value = None if not raw_event else float(raw_event)
            profile = json.loads(profile_entry.get("1.0", "end-1c"))
            signal_values = [value.strip() for value in selected_signals.get().split(",") if value.strip()]
            controller.submit(run_directory=run_directory.get().strip(), event_time_s=event_value,
                              frequency_window_ms=int(frequency_window.get()), selected_signals=signal_values,
                              selected_element_id=bus_labels.get(selected_bus.get()),
                              profile=profile)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            summary.set(tr("panel.metrics.invalid", error=exc))
            context.log("error", str(exc))

    def export_csv():
        if controller.result is None:
            summary.set(tr("panel.metrics.no_export"))
            return
        destination = filedialog.asksaveasfilename(parent=outer, title=tr("panel.metrics.export"),
                                                   defaultextension=".csv", filetypes=(("CSV", "*.csv"),))
        if destination:
            if not Path(destination).resolve().is_relative_to(Path(context.root).resolve()):
                summary.set(tr("panel.metrics.export_root"))
                return
            export_indicators_csv(controller.result, destination)
            summary.set(tr("panel.metrics.exported", path=Path(destination).name))

    run_button = action_button(outer.actions, 'run', text=tr('integration.run'), command=submit); run_button.grid(row=1, column=0, sticky="w", padx=(0, theme.pad_s))
    cancel_button = action_button(body, 'cancel', command=controller.cancel); cancel_button.grid(row=6, column=1, sticky="w", pady=4)
    export_button = action_button(body, 'export', command=export_csv); export_button.grid(row=7, column=0, sticky="w")
    ttk.Label(body, textvariable=summary, wraplength=700).grid(row=8, column=0, columnspan=2, sticky="w")
    ttk.Label(body, textvariable=stale, style="Warning.TLabel", wraplength=700).grid(row=11, column=0, columnspan=2, sticky="w")

    def mark_stale(reason: str):
        stale.set(tr("panel.metrics.stale", reason=reason))

    outer.controller = controller
    results.columnconfigure(0, weight=1)
    outer.plot_area.columnconfigure(0, weight=1)
    outer.plot_area.rowconfigure(0, weight=0, minsize=26)
    outer.preflight_inputs = lambda: {**verified_metric_inputs(context.root, run_directory.get()), "input": {"run_directory": run_directory.get()}, "options": {"event_time_s": float(event_time.get()) if event_time.get().strip() else None, "frequency_window_ms": int(frequency_window.get())}}
    results.rowconfigure(0, weight=1)
    def retranslate():
        for key, label in labels.items(): label.configure(text=tr("panel.metrics." + key))
        pages.tab(value_page, text=tr("panel.metrics.indicators")); pages.tab(metric_page, text=tr("panel.metrics.coverage"))
        for column, key in zip(columns, ("signal", "indicator", "state", "value", "unit", "threshold")):
            table.heading(column, text=tr("panel.metrics." + key))
        run_button.configure(text=tr("panel.metrics.run")); cancel_button.configure(text=tr("panel.metrics.cancel")); export_button.configure(text=tr("panel.metrics.export"))
        if controller.result is None: summary.set(tr("panel.metrics.not_validated"))
    outer.retranslate = retranslate
    outer.recovery_key = 'int2.choose_rms_run'
    def choose_run():
        from tkinter import filedialog
        path = filedialog.askdirectory(parent=outer, initialdir=context.root, title=tr('int2.choose_rms_run'))
        if path:
            run_directory.set(path)
    outer.recovery_action = choose_run
    panel = finish_panel(outer, context, controller, tr("panel.metrics.scope"))
    retranslate()
    return panel
