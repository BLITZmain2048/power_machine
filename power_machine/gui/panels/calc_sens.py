"""Panel de sensibilidades, sin ecuaciones duplicadas fuera de power_machine."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
import math
from dataclasses import asdict
from pathlib import Path
import tkinter as tk
from tkinter import ttk

from power_machine.sensitivities import SensitivityOptions
from power_machine.sensitivities.results import export_csv

from ..i18n import tr
from ..theme import theme

from .panel_widgets import (human_status, format_axes, study_panel, finish_panel, element_label,
                            AdvancedOptions, error_result, panel_retranslate, style_plot)

PANEL_SPEC = {"id": "calc_sens", "title": tr("panel.sensitivity.title"), "title_key": "panel.sensitivity.title",
              "api_version": 1, "factory": "create_panel", "order": 31}


def _label(item):
    return f"{item.get('name', item['id'])} | {item['id']} | {item.get('un_kv', '—')} kV"


def build_request(context, options: SensitivityOptions):
    return {"schema": "power_machine-parity-request-v1", "module": "sensitivities",
            "root": str(context.root), "snapshot": copy.deepcopy(context.get_snapshot()),
            "options": asdict(options), "input": {}}


class PanelController:
    """Controlador ensayable, entrega el cálculo al trabajador del contexto."""

    def __init__(self, context, on_update=None):
        self.context = context
        self.on_update = on_update or (lambda result: None)
        self.job = None
        self.result = None

    def submit(self, options):
        request = build_request(self.context, options)
        self.job = self.context.submit_job("sensitivities", request, self.on_message)
        self.context.log("info", tr("panel.sensitivity.log_started"))
        return request

    def on_message(self, message):
        if isinstance(message, dict) and message.get("schema") == "power_machine-parity-result-v1":
            result = message
        elif isinstance(message, (list, tuple)) and message and message[0] == "result":
            result = message[1]
        elif isinstance(message, (list, tuple)) and message and message[0] == "progress":
            self.on_update({"progress": message[1]})
            return
        elif isinstance(message, (list, tuple)) and message and message[0] == "log":
            self.context.log(message[1], message[2])
            return
        else:
            self.on_update(message)
            return
        self.result = result
        self.context.publish_result(result)
        self.on_update(result)

    def cancel(self):
        if self.job is not None and callable(getattr(self.job, "cancel", None)):
            self.job.cancel()
        else:
            self.context.log("warning", tr("panel.common.cancel_from_job_control"))


def create_panel(parent, context) -> ttk.Frame:
    """Crea un formulario desplazable y una zona de tabla apta para 1000 x 600."""
    outer, form, results = study_panel(parent)
    snapshot = context.get_snapshot()
    buses = [row for row in snapshot["model"].get("buses", []) if row.get("outserv", 0) != 1]
    branches = [row for row in snapshot["model"].get("branches", []) if row.get("outserv", 0) != 1]
    bus_labels = {element_label(row, snapshot["model"]): row["id"] for row in buses}
    branch_labels = {element_label(row, snapshot["model"]): row["id"] for row in branches}
    variables = {
        "bus": tk.StringVar(outer, value=next(iter(bus_labels), "")),
        "branch": tk.StringVar(outer, value=next(iter(branch_labels), "")),
        "step_p": tk.StringVar(outer, value="1.0"),
        "step_q": tk.StringVar(outer, value="1.0"),
        "q_limits": tk.BooleanVar(outer, value=True),
        "summary": tk.StringVar(outer, value=tr("panel.common.not_validated_pf")),
        "stale": tk.StringVar(outer, value=""),
    }
    fields = (("injection_bus", "bus", tuple(bus_labels)),
              ("lodf_branch", "branch", tuple(branch_labels)),
              ("step_p", "step_p", None), ("step_q", "step_q", None))
    for row, (title, key, values) in enumerate(fields):
        ttk.Label(form, text=tr("panel.sensitivity." + title)).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        widget = ttk.Combobox(form, textvariable=variables[key], values=values, state="readonly") if values else ttk.Entry(form, textvariable=variables[key])
        widget.grid(row=row, column=1, sticky="ew", pady=2)
    q_limits_button = ttk.Checkbutton(form, text=tr("panel.common.consider_q_limits"), variable=variables["q_limits"])
    q_limits_button.grid(row=4, column=0, sticky="w")
    note = ttk.Label(form, text=tr("panel.sensitivity.note"), wraplength=780)
    note.grid(row=5, column=0, columnspan=3, sticky="w", pady=(4, 1))
    ttk.Label(form, textvariable=variables["stale"], foreground=theme.color("error"), wraplength=780).grid(row=6, column=0, columnspan=3, sticky="w")
    controls = ttk.Frame(form)
    controls.grid(row=7, column=0, columnspan=3, sticky="ew", pady=3)
    tree = ttk.Treeview(results, columns=("name", "ptdf", "lodf", "predicted", "actual", "error", "h", "h2", "difference"), show="headings", height=13)
    headings = {"name": "branch", "ptdf": "ac_derivative", "lodf": "lodf",
                "predicted": "predicted_p", "actual": "contingency_p", "error": "error_mw", "h": "derivative_h", "h2": "derivative_half_h", "difference": "difference"}
    for key, title in headings.items():
        tree.heading(key, text=tr("panel.sensitivity." + title))
        tree.column(key, width=130 if key != "name" else 220, anchor="e" if key != "name" else "w")
    tree.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=(6, 0))
    summary_label = ttk.Label(form, textvariable=variables["summary"], wraplength=920)
    summary_label.grid(row=20, column=0, columnspan=2, sticky="w")
    plot_holder = ttk.Frame(outer.plot_area)
    plot_holder.grid(row=0, column=0, columnspan=2, sticky="nsew")
    plot_canvas = {"widget": None}

    def numeric(value):
        return value

    def sort_rows(key, descending=False):
        rows = [(tree.set(item, key), item) for item in tree.get_children("")]
        def value(row):
            try:
                return float(row[0])
            except ValueError:
                return float("-inf") if descending else float("inf")
        for _raw, item in sorted(rows, key=value, reverse=descending):
            tree.move(item, "", "end")
        tree.heading(key, command=lambda: sort_rows(key, not descending))

    for key in headings:
        tree.heading(key, command=lambda current=key: sort_rows(current))

    def draw_plot(rows):
        if plot_canvas["widget"] is not None:
            plot_canvas["widget"].destroy()
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
            visible = sorted((row for row in rows if row.get("ptdf_mw_per_mw") is not None),
                             key=lambda row: abs(row["ptdf_mw_per_mw"]), reverse=True)[:10]
            figure = Figure(layout="constrained", figsize=(7.6, 2.0), dpi=100)
            axis = figure.add_subplot(111)
            axis.bar(range(len(visible)), [row["ptdf_mw_per_mw"] for row in visible], color=theme.color("accent"))
            axis.set_ylabel(tr("panel.sensitivity.ac_derivative"), fontsize=9)
            axis.set_xticks(range(len(visible)), [f"{i+1}: {str(row.get('name') or row['id'])[-10:]}" for i, row in enumerate(visible)], rotation=25, ha="right", fontsize=8)
            format_axes(axis)
            style_plot(figure, axis)
            widget = FigureCanvasTkAgg(figure, master=plot_holder).get_tk_widget()
            widget.pack(fill="both", expand=True)
            plot_canvas["widget"] = widget
        except (ImportError, tk.TclError):
            ttk.Label(plot_holder, text=tr("panel.common.plot_unavailable")).pack(anchor="w")

    def update(message):
        if isinstance(message, dict) and "progress" in message:
            progress = message["progress"]
            variables["summary"].set(tr("panel.common.calculating_progress", stage=progress.get('stage', ''), percent=100 * progress.get('fraction', 0)))
            return
        if not isinstance(message, dict) or message.get("schema") != "power_machine-parity-result-v1":
            return
        for item in tree.get_children(""):
            tree.delete(item)
        payload = message.get("payload", {})
        conclusions_blocked = payload.get("evaluable", {}).get("conclusions_blocked", False) or any(not v for v in payload.get("control_regime_consistent", {}).values())
        rows = payload.get("branches", [])
        if conclusions_blocked:
            rows = [{**row, **{key: None for key in row if key.endswith(("_mw", "_mw_per_mw"))}} for row in rows]
        for row in sorted(rows, key=lambda item: abs(item.get("ptdf_mw_per_mw") or 0), reverse=True):
            tree.insert("", "end", values=(row.get("name", row.get("id")), numeric(row.get("ptdf_mw_per_mw")),
                                               numeric(row.get("lodf_mw_per_mw")), numeric(row.get("predicted_post_outage_p_mw")),
                                               numeric(row.get("actual_post_outage_p_mw")), numeric(row.get("lodf_error_mw")), row.get("ac_transfer_derivative_h_mw_per_mw"), row.get("ac_transfer_derivative_h2_mw_per_mw"), row.get("ac_transfer_step_difference_mw_per_mw")))
        verification = message.get("payload", {}).get("lodf_verification", {})
        variables["summary"].set(tr("panel.sensitivity.summary", status=human_status(message.get('status', 'unknown')), error=numeric(verification.get('max_abs_error_mw'))))
        fd = message.get("payload", {}).get("finite_difference", {})
        compensator = message.get("payload", {}).get("compensating_generator", {})
        reference = compensator.get("reference_bus_id")
        reference_label = next((label for label, ident in bus_labels.items() if ident == reference), reference)
        variables["summary"].set(variables["summary"].get() + "\n" + tr("panel.sensitivity.details", step=fd.get('step_p_mw'), half_step=fd.get('half_step_p_mw'), compensator=compensator.get('name', compensator.get('generator_id')), reference=reference_label, lodf=tr('panel.common.evaluable') if verification.get('evaluable') else tr('panel.common.not_evaluable')))
        if conclusions_blocked:
            variables["summary"].set(tr("panel.sensitivity.conclusions_blocked") + " " + variables["summary"].get())
        draw_plot(rows)

    controller = PanelController(context, update)

    def submit():
        try:
            step_p, step_q = float(variables["step_p"].get()), float(variables["step_q"].get())
            if not (math.isfinite(step_p) and step_p > 0 and math.isfinite(step_q) and step_q > 0):
                raise ValueError(tr("panel.sensitivity.invalid_steps"))
            options = SensitivityOptions(injection_bus_id=bus_labels[variables["bus"].get()],
                                         outage_branch_id=branch_labels[variables["branch"].get()],
                                         step_p_mw=step_p, step_q_mvar=step_q,
                                         q_limits=variables["q_limits"].get())
            variables["stale"].set("")
            controller.submit(options)
        except (KeyError, ValueError, TypeError) as exc:
            variables["summary"].set(str(exc))
            context.log("error", str(exc))

    def export():
        if controller.result is None:
            variables["summary"].set(tr("panel.common.run_before_export"))
            return
        if controller.result.get("payload", {}).get("evaluable", {}).get("conclusions_blocked"):
            variables["summary"].set(tr("panel.sensitivity.export_blocked"))
            return
        try:
            target = export_csv(controller.result, Path(context.root) / "sensitivities", root=context.root)
            variables["summary"].set(tr("panel.common.csv_exported", path=target.relative_to(Path(context.root).resolve())))
        except (OSError, ValueError) as exc:
            variables["summary"].set(str(exc))
            context.log("error", str(exc))

    run_button = action_button(controls, 'run', text=tr('panel.common.run'), command=submit)
    cancel_button = action_button(controls, 'cancel', text=tr('panel.common.cancel'), command=controller.cancel)
    export_button = action_button(controls, 'export', text=tr('panel.common.export_csv'), command=export)
    run_button.pack(side="left")
    cancel_button.pack(side="left", padx=5)
    export_button.pack(side="left")
    form.columnconfigure(1, weight=1)
    outer.columnconfigure(0, weight=1)
    outer.rowconfigure(1, weight=1)
    outer.controller = controller
    results.columnconfigure(0, weight=1)
    outer.plot_area.columnconfigure(0, weight=1)
    outer.plot_area.rowconfigure(0, weight=1)
    outer.preflight_inputs = lambda: {"options": {"injection_bus_id": bus_labels.get(variables["bus"].get()), "outage_branch_id": branch_labels.get(variables["branch"].get())}}
    def retranslate():
        q_limits_button.configure(text=tr("panel.common.consider_q_limits"))
        note.configure(text=tr("panel.sensitivity.note"))
        run_button.configure(text=tr("panel.common.run"))
        cancel_button.configure(text=tr("panel.common.cancel"))
        export_button.configure(text=tr("panel.common.export_csv"))
        for name, title in headings.items():
            tree.heading(name, text=tr("panel.sensitivity." + title))
    panel_retranslate(outer, retranslate)
    return finish_panel(outer, context, controller, tr('panel.sensitivity.scope'))
