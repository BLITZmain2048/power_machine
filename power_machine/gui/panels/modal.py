"""Panel modal autónomo, enlazado únicamente mediante el contexto estructural."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
import tkinter as tk
from tkinter import ttk
from power_machine.modal.results import fingerprint
from power_machine.resources import dynamic_catalog_root

from ..i18n import tr
from ..theme import theme
from .panel_widgets import (diagnostic_lines, human_status, finish_panel, element_label,
                            study_panel, ElementSelector, ResultTable, AdvancedOptions,
                            significant, error_result, style_plot)

PANEL_SPEC = {"title_key": 'integration.small_signal_analysis', "id": "modal", "title": tr('integration.small_signal_analysis'), "api_version": 1,
              "factory": "create_panel", "order": 30}


class PanelController:
    """Lógica comprobable sin pantalla, entrega snapshots serializables."""
    def __init__(self, context, display=lambda result: None):
        self.context, self.display = context, display
        self.result = None
        self.job = None

    def start(self, *, profile="full_rms", object_id=None, options=None, input_data=None):
        snapshot = copy.deepcopy(self.context.get_snapshot())
        hashes = snapshot.setdefault("fingerprints", {})
        hashes.setdefault("model_sha256", fingerprint(snapshot["model"]))
        hashes.setdefault("study_sha256", fingerprint(snapshot["study"]))
        hashes.setdefault("variant_sha256", None)
        data = {**(input_data or {}), "profile": profile}
        if object_id:
            data["object_id"] = object_id
        request = {"schema": "pm-parity-request-v1", "module": "modal", "root": str(self.context.root),
                   "snapshot": snapshot, "options": options or {}, "input": data}
        self.job = self.context.submit_job("modal", request, self.on_message)
        return request

    def cancel(self):
        # El contrato mínimo no declara cancel_job. Se usa el handle si panel integration
        # lo devuelve, de otro modo se muestra la ausencia de esa operación.
        if callable(getattr(self.job, "cancel", None)):
            self.job.cancel()
        elif hasattr(self.job, "is_set") and hasattr(self.job, "set"):
            self.job.set()
        else:
            self.context.log("aviso", tr('integration.the_context_does_not_yet_provide_a'))

    def on_message(self, message):
        kind = message[0]
        if kind == "result":
            self.result = message[1]
            self.context.publish_result(self.result)
            self.display(self.result)
        elif kind == "log":
            self.context.log(message[1], message[2])
        elif kind == "error":
            self.context.log("error", str(message[1]))
            self.display(error_result(message[1]))

    def select_state(self, state_index):
        if self.result is not None:
            rows = self.result.get("payload", {}).get("states", [])
            row = next((s for s in rows if s["index"] == state_index), None)
            if row:
                self.context.select_element(row["object_id"])


def create_panel(parent, context) -> ttk.Frame:
    shell, frame, results = study_panel(parent)
    frame.columnconfigure(0, weight=1)
    frame.rowconfigure(3, weight=1)
    status = tk.StringVar(frame)
    profile = tk.StringVar(frame, "full_rms")
    selected = tk.StringVar(frame)
    reduce_frame = tk.BooleanVar(frame, False)
    choices = {element_label(g, context.get_snapshot()["model"]): g["id"]
               for g in context.get_snapshot()["model"].get("gens", [])
               if g.get("class") == "ElmSym" and not g.get("outserv")}
    toolbar = ttk.Frame(frame, padding=theme.pad_m)
    toolbar.grid(row=0, column=0, sticky="ew", padx=6, pady=6)
    profile_label = ttk.Label(toolbar)
    profile_label.grid(row=0, column=0, sticky="w")
    ttk.Combobox(toolbar, textvariable=profile, values=("full_rms", "machine_admittance", "ieee39_classical"),
                 state="readonly", width=22).grid(row=0, column=1, sticky="ew")
    ttk.Combobox(toolbar, textvariable=selected, values=tuple(choices),
                 state="readonly", width=42).grid(row=1, column=1, sticky="ew")
    advanced = AdvancedOptions(toolbar)
    advanced.grid(row=2, column=0, columnspan=2, sticky="ew")
    reduce_check = ttk.Checkbutton(advanced.content, variable=reduce_frame)
    reduce_check.pack(anchor="w")
    columns = ("mode", "sigma", "frequency", "damping", "classification")
    pages = ttk.Notebook(results)
    pages.grid(row=0, column=0, sticky="nsew")
    mode_page, participation_page, detail_page = (ttk.Frame(pages) for _ in range(3))
    for page, row in ((mode_page, 2), (participation_page, 4), (detail_page, 3)):
        pages.add(page, text="")
        page.columnconfigure(0, weight=1)
        page.rowconfigure(row, weight=1)
    table = ttk.Treeview(mode_page, columns=columns, show="headings", height=3)
    for col in columns:
        table.heading(col, text="", command=lambda key=col: sort_modes(key))
        table.column(col, width=100)
    table.grid(row=2, column=0, sticky="nsew", padx=6)
    details = tk.Text(detail_page, height=4, wrap="word", state="disabled")
    details.grid(row=3, column=0, sticky="nsew", padx=6, pady=6)
    participation = ttk.Treeview(participation_page, columns=("state", "object", "unit", "weight"), show="headings", height=3)
    for col in ("state", "object", "unit", "weight"):
        participation.heading(col, text="")
    participation.grid(row=4, column=0, sticky="nsew", padx=6)
    plot_host = ttk.Frame(shell.plot_area)
    plot_host.grid(row=0, column=0, sticky="nsew")
    chart = {"canvas": None, "axis": None}
    mode_rows = []
    sort_state = {"key": "frequency", "reverse": False}

    def sort_modes(key):
        sort_state["reverse"] = not sort_state["reverse"] if sort_state["key"] == key else False
        sort_state["key"] = key
        render_modes()

    def render_modes():
        for item in table.get_children():
            table.delete(item)
        def value(row):
            item = row.get({"mode": "index", "sigma": "decay_rate_per_s", "frequency": "frequency_hz", "damping": "damping_ratio", "classification": "classification"}[sort_state["key"]])
            return (item is None, item if item is not None else 0)
        for mode in sorted(mode_rows, key=value, reverse=sort_state["reverse"]):
            table.insert("", "end", iid=str(mode["index"]), values=(mode["index"], mode["decay_rate_per_s"],
                mode["frequency_hz"], mode["damping_ratio"], mode["classification"]))

    def draw_complex():
        try:
            from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
            from matplotlib.figure import Figure
        except ImportError:
            return
        if chart["canvas"] is not None:
            chart["canvas"].get_tk_widget().destroy()
        figure = Figure(layout="constrained", figsize=(5.5, 3.2), dpi=100)
        axis = figure.add_subplot(111)
        style_plot(figure, axis)
        stable = [mode for mode in mode_rows if mode["decay_rate_per_s"] < 0]
        unstable = [mode for mode in mode_rows if mode["decay_rate_per_s"] >= 0]
        for rows, token, label in ((stable, "ok", tr("panel.modal.stable")), (unstable, "error", tr("panel.modal.unstable"))):
            if rows:
                axis.scatter([r["decay_rate_per_s"] for r in rows],
                             [r["lambda"]["imag"] for r in rows], color=theme.color(token), label=label)
        axis.axhline(0, color=theme.color("border"), linewidth=.8)
        axis.axvline(0, color=theme.color("border"), linewidth=.8)
        axis.set_xlabel(tr("panel.modal.real_axis"))
        axis.set_ylabel(tr("panel.modal.imag_axis"))
        if axis.collections:
            axis.legend()
        chart["canvas"] = FigureCanvasTkAgg(figure, master=plot_host)
        chart["canvas"].draw_idle()
        chart["canvas"].get_tk_widget().pack(fill="both", expand=True)

    results.columnconfigure(0, weight=1)
    results.rowconfigure(0, weight=1)
    def display(result):
        shell.show_state(result)
        status.set(tr("panel.modal.status", status=human_status(result["status"])))
        mode_rows[:] = [mode for mode in result.get("payload", {}).get("modes", []) if mode["table_visible"]]
        render_modes()
        draw_complex()
        messages = diagnostic_lines(result)
        messages += [f"{r.get('object_id')}: {human_status(r.get('status', 'excluido'))} · {r.get('relative_path')}"
                     for r in result.get("payload", {}).get("coverage", [])]
        details.configure(state="normal")
        details.delete("1.0", "end")
        details.insert("end", "\n".join(messages))
        details.configure(state="disabled")

    controller = PanelController(context, display)

    def start():
        meta = context.get_snapshot()["model"].get("meta", {})
        if not modal_base_supported(meta):
            shell.show_error(tr('integration.profile_blocked_requires_100_mva_and_50'))
            return
        if profile.get() == "machine_admittance" and selected.get() not in choices:
            context.log("error", tr("panel.modal.select_machine"))
            return
        status.set(tr("panel.modal.calculating"))
        controller.start(profile=profile.get(), object_id=choices.get(selected.get()),
                         input_data={"reduce_rotor_frame": reduce_frame.get()})

    def show_participation(event=None):
        for item in participation.get_children():
            participation.delete(item)
        if not table.selection() or controller.result is None:
            return
        mode_index = int(table.selection()[0])
        rows = controller.result.get("payload", {}).get("participation")
        if rows is None:
            context.log("aviso", tr("panel.modal.no_participation"))
            return
        for row in sorted((r for r in rows if r["mode_index"] == mode_index),
                          key=lambda r: -r["normalized_abs_p"]):
            participation.insert("", "end", iid=str(row["state_index"]),
                values=(row["variable"], row["object_id"], row["state_unit"], row["normalized_abs_p"]))

    def select(event=None):
        if participation.selection():
            controller.select_state(int(participation.selection()[0]))

    calculate_button = action_button(toolbar, 'run', command=start)
    calculate_button.grid(row=3, column=0, sticky="w")
    cancel_button = action_button(toolbar, 'cancel', command=controller.cancel)
    cancel_button.grid(row=3, column=1, sticky="w")
    ttk.Label(frame, textvariable=status).grid(row=1, column=0, sticky="w", padx=6)
    table.bind("<<TreeviewSelect>>", show_participation)
    participation.bind("<Double-1>", select)
    compatibility = modal_compatibility(context.get_snapshot()["model"], context.root)
    compatibility_label = ttk.Label(frame, text=compatibility, wraplength=850)
    compatibility_label.grid(row=5, column=0, sticky="ew")
    shell.modal_controller = controller
    shell.controller = controller
    def retranslate():
        profile_label.configure(text=tr("panel.modal.profile"))
        if hasattr(shell, 'compatible_case_button'): shell.compatible_case_button.configure(text=tr('int2.open_modal_case'))
        reduce_check.configure(text=tr("panel.modal.reduce_rotor"))
        pages.tab(mode_page, text=tr("panel.modal.modes"))
        pages.tab(participation_page, text=tr("panel.modal.participation"))
        pages.tab(detail_page, text=tr("panel.modal.details"))
        for column, key in (("mode", "mode"), ("sigma", "sigma"), ("frequency", "frequency"), ("damping", "damping"), ("classification", "classification")):
            table.heading(column, text=tr("panel.modal." + key))
        for column, key in (("state", "state"), ("object", "object"), ("unit", "unit"), ("weight", "weight")):
            participation.heading(column, text=tr("panel.modal." + key))
        calculate_button.configure(text=tr("panel.modal.calculate"))
        cancel_button.configure(text=tr("panel.modal.cancel"))
        compatibility_label.configure(text=modal_compatibility(context.get_snapshot()["model"], context.root))
        if controller.result:
            display(controller.result)
        else:
            status.set(tr("panel.modal.not_validated"))
    shell.retranslate = retranslate
    shell.plot_area.columnconfigure(0, weight=1)
    shell.plot_area.rowconfigure(0, weight=1)
    from .panel_widgets import CapabilityGate
    shell.recovery_key = 'int2.select_profile'
    def recover_selection():
        controls = [w for w in CapabilityGate.descendants(shell.options) if isinstance(w, ttk.Combobox)]
        if controls:
            shell.form.reveal(controls[0])
            controls[0].focus_set()
            controls[0].event_generate('<Down>')
    shell.recovery_action = recover_selection
    shell.preflight_inputs = lambda: {"input": {"profile": profile.get(), "object_id": choices.get(selected.get())}}
    shell.modal_profile = profile
    def open_compatible():
        from power_machine.resources import readonly_path
        app = shell.winfo_toplevel()
        app.open_ieee39(readonly_path('cases', 'ieee39_ref', 'model.json'))
        app.open_parity_panel('modal')
        app.parity_panels['modal'][1].panel.modal_profile.set('ieee39_classical')
    compatible_button = ttk.Button(toolbar, text=tr('int2.open_modal_case'), command=open_compatible)
    from power_machine.resources import ref_data_available
    if not ref_data_available(): compatible_button.configure(state="disabled")
    compatible_button.grid(row=4, column=0, columnspan=2, sticky='ew', pady=6)
    shell.compatible_case_button = compatible_button
    panel = finish_panel(shell, context, controller, tr("panel.modal.scope"))
    retranslate()
    return panel


def modal_compatibility(model, root):
    """Resumen de requisitos estructurales, la cobertura final pertenece al motor."""
    from pathlib import Path
    meta = model.get("meta", {})
    base_ok = modal_base_supported(meta)
    base = tr("panel.modal.compatible_bases") if base_ok else tr("panel.modal.blocked_bases")
    missing = [name for name in ("manifest.json", "objects.csv", "attributes.csv", "references.csv")
               if not (dynamic_catalog_root(root) / name).is_file()]
    machines = sum(g.get("class") == "ElmSym" and not g.get("outserv") for g in model.get("gens", []))
    return ("full_rms: " + base + (tr("panel.modal.missing_files", files=", ".join(missing)) if missing else
            tr("panel.modal.continuous_coverage")) + "\n"
            + tr("panel.modal.machine_compatibility", base=base, machines=machines))


def modal_base_supported(meta):
    """La GUI conserva la base y la frecuencia declaradas, sin normalizarlas."""
    import math
    frequency = meta.get("f_nom_hz")
    return (meta.get("s_base_mva") == 100 and not isinstance(frequency, bool)
            and isinstance(frequency, (int, float)) and math.isfinite(frequency)
            and frequency > 0)


def execute_request(request, *, cancel=None, progress=None):
    """Adapta el perfil clásico publicado a la entrada matricial pública del motor.

    La matriz y los modos se calculan en el motor. No contiene ecuaciones GUI.
    Solo admite el modelo de referencia íntegro, sin escenarios modificados.
    """
    from power_machine.modal import execute_request as execute_modal
    if request.get('input', {}).get('profile') != 'ieee39_classical':
        return execute_modal(request, cancel=cancel, progress=progress)
    from pathlib import Path
    from power_machine.resources import readonly_path
    from power_machine.refcompare.modal import native_inputs, LDF_OPTIONS
    from power_machine.modal import build_classical_model
    from power_machine.loadflow import run_loadflow
    from .panel_widgets import error_result
    import json
    try:
        model = request['snapshot']['model']
        directory = readonly_path('cases', 'ieee39_ref')
        reference = json.loads((directory / 'model.json').read_text(encoding='utf-8-sig'))
        if fingerprint(model) != fingerprint(reference) or any(request['snapshot']['study'].get('scenario', {}).values()):
            raise ValueError(tr('int2.modal_reference_only'))
        if not modal_base_supported(model.get('meta', {})):
            raise ValueError(tr('panel.modal.blocked_bases'))
        types, configuration, _ = native_inputs(directory)
        classical = build_classical_model(model, run_loadflow(model, options=LDF_OPTIONS), types, configuration)
        adapted = copy.deepcopy(request)
        adapted['input'] = dict(profile='analytical_linear_dae', x=[0.] * len(classical.states),
                                z=[], fx=classical.a.tolist(), states=[dict(row, scale=1.) for row in classical.states],
                                angular_frame='IEEE39 classical rotor-relative perturbations')
        result = execute_modal(adapted, cancel=cancel, progress=progress)
        result['payload'].setdefault('metadata', {}).update(profile='ieee39_classical',
            f_nom_hz=model['meta']['f_nom_hz'], physical_operating_state=classical.x0.tolist(),
            source='power_machine.modal.build_classical_model', mechanical_convention=classical.mechanical_convention)
        result['gui_validation_label'] = tr('int2.modal_classical_scope')
        import hashlib
        for relative in ('model.json', 'modelo/types.json', 'E39-MOD-01/configuration_final.json'):
            path = Path(directory) / relative
            result['provenance']['inputs'].append(dict(path='cases/ieee39_ref/' + relative,
                                                      sha256=hashlib.sha256(path.read_bytes()).hexdigest()))
        return result
    except (ValueError, KeyError, OSError) as exc:
        return error_result(str(exc))
