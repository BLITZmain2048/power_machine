"""Panel CCT autónomo, las simulaciones pertenecen a power_machine.cct."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
import csv
from dataclasses import asdict
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from power_machine.cct import CCTInput, CCTOptions
from power_machine.cct.results import fingerprint

from ..i18n import tr
from ..theme import theme
from .panel_widgets import (human_status, study_panel, finish_panel, element_label,
                            AdvancedOptions, error_result, format_axes, ResultTable, style_plot)

PANEL_SPEC = {"title_key": 'integration.critical_clearing_time', "id": "calc_cct", "title": tr('integration.critical_clearing_time'), "api_version": 1,
              "factory": "create_panel", "order": 25}


def _substation(item):
    from ..presentation import station
    return station(item)


def bus_labels(model):
    """Etiquetas visibles con nombre, subestación, nivel y clave interna."""
    labels = {}
    for bus in model.get("buses", []):
        if bus.get("outserv"):
            continue
        label = element_label(bus, model)
        labels[label] = bus["id"]
    return labels


def branch_labels(model):
    labels = {tr("panel.cct.no_opening"): None}
    for branch in model.get("branches", []) + model.get("branches3", []):
        if branch.get("outserv"):
            continue
        label = element_label(branch, model)
        labels[label] = branch["id"]
    return labels


def build_request(context, options, input_data):
    snapshot = copy.deepcopy(context.get_snapshot())
    hashes = snapshot.setdefault("fingerprints", {})
    hashes.setdefault("model_sha256", fingerprint(snapshot["model"]))
    hashes.setdefault("study_sha256", fingerprint(snapshot["study"]))
    hashes.setdefault("variant_sha256", None)
    return {"schema": "pm-parity-request-v1", "module": "cct", "root": str(context.root),
            "snapshot": snapshot, "options": asdict(options), "input": asdict(input_data)}


def execute_panel_request(request, *, cancel=None, progress=None):
    """Normaliza únicamente el transporte de progreso CCT y RMS al trabajador."""
    from power_machine.cct import execute_request
    def forward(*values):
        if len(values) == 1:
            progress(values[0])
        elif len(values) == 3:
            time_s, end_s, ms_per_step = values
            progress({"phase": tr('integration.rms_integration'), "time_s": time_s,
                      "end_s": end_s, "ms_per_step": ms_per_step})
    return execute_request(request, cancel=cancel, progress=forward if progress else None)


class PanelController:
    """Controlador ensayable que nunca hace cálculo en el hilo Tk."""

    def __init__(self, context, display=None):
        self.context, self.display = context, display or (lambda result: None)
        self.job, self.result = None, None

    def submit(self, options, input_data):
        request = build_request(self.context, options, input_data)
        self.job = self.context.submit_job("cct", request, self.on_message)
        self.context.log("INFO", tr('integration.rms_cct_no_ref_validation'))
        return request

    def cancel(self):
        if callable(getattr(self.job, "cancel", None)):
            self.job.cancel()
        elif hasattr(self.job, "set"):
            self.job.set()
        else:
            self.context.log('AVISO', tr('integration.cancellation_is_requested_through_the_application_job'))

    def on_message(self, message):
        if isinstance(message, (tuple, list)) and message:
            if message[0] == "result":
                self.result = message[1]
            elif message[0] == "progress":
                self.display({"progress": message[1]})
                return
            elif message[0] == "log":
                self.context.log(message[1], message[2])
                return
            elif message[0] == "error":
                self.context.log("ERROR", str(message[1]))
                self.display(error_result(message[1]))
                return
            else:
                return
        elif isinstance(message, dict) and message.get("schema") == "pm-parity-result-v1":
            self.result = message
        else:
            return
        self.context.publish_result(self.result)
        self.display(self.result)


def _result_rows(result):
    payload = result.get("payload", {})
    rows = []
    for name, item in ((tr('integration.stable'), payload.get("stable_frontier")), ("Inestable", payload.get("unstable_frontier"))):
        if item:
            rows.append((name, item.get("clearing_s"), item.get("max_angle_from_coi_deg"),
                         item.get("min_recovery_voltage_pu"), item.get("stable")))
    return rows


def create_panel(parent, context):
    """Formulario desplazable y zona de resultados visible en 1000 x 600."""
    frame, form, result = study_panel(parent)
    model = context.get_snapshot()["model"]
    buses, branches = bus_labels(model), branch_labels(model)
    default_bus = next(iter(buses), "")
    if model.get("meta", {}).get("benchmark") == "ieee39":
        default_bus = next((key for key, value in buses.items() if value == "B16"), default_bus)
    values = {"engine": tk.StringVar(frame, "auto"), "bus": tk.StringVar(frame, default_bus),
              "branch": tk.StringVar(frame, tr("panel.cct.no_opening")), "lower": tk.StringVar(frame, "0.05"),
              "upper": tk.StringVar(frame, "0.35"), "resolution": tk.StringVar(frame, "5"),
              "end": tk.StringVar(frame, "5"), "step": tk.StringVar(frame, "2"),
              "angle": tk.StringVar(frame, "180"), "voltage": tk.StringVar(frame, "0.90"),
              "window": tk.StringVar(frame, "0.20"), "inception": tk.StringVar(frame, "1.0"),
              "rf": tk.StringVar(frame, ""), "xf": tk.StringVar(frame, "")}
    fields = (("engine", "engine", ("auto", "ieee39_classical", "power_machine_rms")),
              ("bus", "fault_bus", tuple(buses)), ("branch", "clearing_opening", tuple(branches)),
              ("lower", "stable_limit", None), ("upper", "unstable_limit", None),
              ("resolution", "resolution", None), ("end", "horizon", None), ("step", "rms_step", None),
              ("angle", "angle", None), ("voltage", "voltage", None), ("window", "window", None),
              ("inception", "inception", None), ("rf", "rf", None), ("xf", "xf", None))
    field_labels = {}
    for row, (key, label, choices) in enumerate(fields):
        field_labels[key] = ttk.Label(form)
        field_labels[key].grid(row=row, column=0, sticky="w", pady=2)
        widget = ttk.Combobox(form, textvariable=values[key], values=choices, state="readonly") if choices else ttk.Entry(form, textvariable=values[key])
        widget.grid(row=row, column=1, sticky="ew", pady=2)
    domain_label = ttk.Label(form, wraplength=315)
    domain_label.grid(row=len(fields), column=0, columnspan=2, sticky="w", pady=(7, 3))
    form.columnconfigure(1, weight=1)
    state = tk.StringVar(frame)
    ttk.Label(result, textvariable=state, wraplength=620).grid(row=0, column=0, sticky="w")
    pages = ttk.Notebook(result)
    pages.grid(row=1, column=0, sticky="nsew")
    frontier_page, trial_page = ttk.Frame(pages), ttk.Frame(pages)
    pages.add(frontier_page, text="")
    pages.add(trial_page, text="")
    frontier_page.columnconfigure(0, weight=1)
    frontier_page.rowconfigure(1, weight=1)
    trial_table = ResultTable(trial_page, [("clearing_s", tr("panel.cct.clearing")), ("phase", tr("panel.cct.phase")), ("classification", tr("panel.cct.classification")), ("reason", tr("panel.cct.reason"))], context.root)
    trial_table.pack(fill="both", expand=True)
    table = ttk.Treeview(frontier_page, columns=("time", "angle", "voltage", "stable"), show="headings", height=4)
    for key in ("time", "angle", "voltage", "stable"):
        table.heading(key, text="")
    table.grid(row=1, column=0, sticky="ew", pady=(5, 4))
    plot_host = ttk.Frame(frame.plot_area)
    plot_host.grid(row=0, column=0, sticky="nsew")
    figure_canvas, axes = None, None
    try:
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        figure = Figure(layout="constrained", figsize=(5.5, 3.5), dpi=100)
        axes = figure.add_subplot(111)
        style_plot(figure, axes)
        figure_canvas = FigureCanvasTkAgg(figure, master=plot_host)
        figure_canvas.get_tk_widget().pack(fill="both", expand=True)
    except Exception as exc:
        ttk.Label(plot_host, text=tr("panel.cct.plot_unavailable", error=exc)).pack(anchor="w")

    def display(message):
        nonlocal axes
        if "progress" in message:
            item = message["progress"]
            state.set(tr("panel.cct.progress", phase=item.get("phase", tr("panel.cct.simulation")), completed=item.get("completed", 0)))
            return
        status = human_status(message.get("status", "sin resultado"))
        trial_table.set_rows(message.get("payload", {}).get("trials", []))
        payload = message.get("payload", {})
        count = payload.get("simulation_count", len(payload.get("trials", [])))
        interval = tr("panel.cct.interval", lower=values["lower"].get(), upper=values["upper"].get(), resolution=values["resolution"].get())
        state.set(tr("panel.cct.result", status=status, count=count, interval=interval))
        trials = payload.get("trials", [])
        if status == "Parcial" and trials and all(row.get("classification") == "stable" for row in trials):
            state.set(state.get() + "\n" + tr("panel.cct.no_unstable"))
        for item in table.get_children():
            table.delete(item)
        refinement = payload.get("step_refinement", {})
        if refinement:
            state.set(state.get() + "\n" + tr("panel.cct.refinement", base=refinement.get("base_step_ms"), reduced=refinement.get("reduced_step_ms"), consistent=tr("panel.cct.yes") if refinement.get("consistent") else tr("panel.cct.no")))
        if refinement.get("consistent") is False:
            state.set(tr("panel.cct.refinement_failed"))
        frontier_rows = [] if refinement.get("consistent") is False else _result_rows(message)
        for label, clearing, angle, voltage, stable in frontier_rows:
            table.insert("", "end", values=(clearing,
                         angle, voltage,
                         label if stable else label + tr('integration.fails')))
        if axes is not None:
            axes.clear()
            for label, item in (("estable", payload.get("stable_frontier")), ("inestable", payload.get("unstable_frontier"))):
                if not item:
                    continue
                trajectory = item.get("trajectory", [])
                if trajectory:
                    axes.plot([row["time_s"] for row in trajectory],
                              [max(abs(value) for value in row["rotor_angles_from_coi_deg"]) for row in trajectory], label=label)
            axes.set_xlabel(tr("panel.cct.time_axis"))
            axes.set_ylabel(tr("panel.cct.angle_axis"))
            style_plot(axes.figure, axes)
            if axes.lines:
                axes.legend()
            format_axes(axes)
            figure_canvas.draw_idle()

    controller = PanelController(context, display)
    if hasattr(context, "worker"):
        resolver = context.worker.resolver
        context.worker.resolver = lambda module: execute_panel_request if module == "cct" else resolver(module)
    def submit():
        try:
            branch_id = branches.get(values["branch"].get())
            input_data = CCTInput(engine=values["engine"].get(), fault_bus_id=buses.get(values["bus"].get(), ""),
                                  fault_inception_s=float(values["inception"].get()),
                                  clearing_action="open_branch" if branch_id else "simple", clearing_branch_id=branch_id,
                                  fault_r_ohm=float(values["rf"].get()) if values["rf"].get().strip() else None,
                                  fault_x_ohm=float(values["xf"].get()) if values["xf"].get().strip() else None)
            options = CCTOptions(lower_clearing_s=float(values["lower"].get()), upper_clearing_s=float(values["upper"].get()),
                                 resolution_ms=float(values["resolution"].get()), end_s=float(values["end"].get()),
                                 step_ms=float(values["step"].get()), max_angle_from_coi_deg=float(values["angle"].get()),
                                 recovery_min_voltage_pu=float(values["voltage"].get()), recovery_window_s=float(values["window"].get()))
            state.set(tr("panel.cct.calculating"))
            controller.submit(options, input_data)
        except (ValueError, TypeError) as exc:
            state.set(tr("panel.cct.invalid", error=exc))
            context.log("ERROR", str(exc))

    def export_csv():
        if not controller.result:
            state.set(tr("panel.cct.no_export"))
            return
        path = filedialog.asksaveasfilename(parent=frame, initialdir=context.root, defaultextension=".csv",
                                            filetypes=(("CSV", "*.csv"),))
        if not path:
            return
        destination, root = Path(path).resolve(), Path(context.root).resolve()
        if not destination.is_relative_to(root):
            state.set(tr("panel.cct.export_root"))
            return
        with destination.open("x", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(("frontier", "time_s", "coi_angle_deg", "max_angle_from_coi_deg", "min_voltage_pu"))
            for label, item in (("stable", controller.result.get("payload", {}).get("stable_frontier")),
                                ("unstable", controller.result.get("payload", {}).get("unstable_frontier"))):
                for row in (item or {}).get("trajectory", []):
                    writer.writerow((label, row["time_s"], row["coi_angle_deg"],
                                     max(abs(value) for value in row["rotor_angles_from_coi_deg"]), row["min_voltage_pu"]))
        state.set(tr("panel.cct.exported", path=destination.relative_to(root)))

    run_button = action_button(frame.actions, 'run', text=tr('integration.run'), command=submit)
    run_button.grid(row=1, column=0, sticky="w", padx=(0, theme.pad_s))
    cancel_button = action_button(form, 'cancel', command=controller.cancel)
    cancel_button.grid(row=len(fields)+1, column=1, sticky="w", pady=(8, 2))
    export_button = action_button(form, 'export', command=export_csv)
    export_button.grid(row=len(fields)+2, column=0, sticky="w")
    frame.controller = controller
    def mark_stale(reason: str):
        state.set(tr("panel.cct.stale", reason=reason))
    frame.results.columnconfigure(0, weight=1)
    frame.results.rowconfigure(0, weight=0)
    frame.results.rowconfigure(1, weight=1)
    frame.plot_area.columnconfigure(0, weight=1)
    frame.plot_area.rowconfigure(0, weight=1)
    frame.preflight_inputs = lambda: {"input": {"engine": values["engine"].get(), "fault_bus_id": buses.get(values["bus"].get()), "fault_inception_s": float(values["inception"].get()), "clearing_branch_id": branches.get(values["branch"].get()), "fault_r_ohm": float(values["rf"].get()) if values["rf"].get().strip() else None, "fault_x_ohm": float(values["xf"].get()) if values["xf"].get().strip() else None}, "options": {"step_ms": float(values["step"].get()), "end_s": float(values["end"].get())}}
    def retranslate():
        for key, label in field_labels.items():
            label.configure(text=tr("panel.cct." + dict(fields)[key] if False else "panel.cct." + next(value for name, value, _ in fields if name == key)))
        domain_label.configure(text=tr("panel.cct.domain"))
        pages.tab(frontier_page, text=tr("panel.cct.frontiers"))
        pages.tab(trial_page, text=tr("panel.cct.trials"))
        for key, text_key in (("time", "clearing"), ("angle", "angle_result"), ("voltage", "voltage_result"), ("stable", "criterion")):
            table.heading(key, text=tr("panel.cct." + text_key))
        run_button.configure(text=tr("panel.cct.run")); cancel_button.configure(text=tr("panel.cct.cancel")); export_button.configure(text=tr("panel.cct.export"))
        if not controller.result: state.set(tr("panel.cct.no_result"))
    frame.retranslate = retranslate
    from .panel_widgets import CapabilityGate
    frame.recovery_key = 'int2.select_engine'
    def recover_selection():
        controls = [w for w in CapabilityGate.descendants(frame.options) if isinstance(w, ttk.Combobox)]
        if controls:
            frame.form.reveal(controls[0])
            controls[0].focus_set()
            controls[0].event_generate('<Down>')
    frame.recovery_action = recover_selection
    panel = finish_panel(frame, context, controller, tr("panel.cct.scope"))
    retranslate()
    return panel
