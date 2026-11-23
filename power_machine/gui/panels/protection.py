"""Panel propio de protecciones, ajustes privados y TCC Canvas sin registro global."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import os
import math
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, ttk
import uuid

from power_machine.protection import CurveSpec, RelayStage, CoordinationPair, standard_curve
from power_machine.protection.io_exports import read_protection_exports, compile_stages, template_points, TIME_COLUMNS
from power_machine.protection.settings import new_workspace, effective_stages, revise_settings, save_settings, load_settings, confined_path
from power_machine.protection.results import fingerprint, export_svg, export_points_csv, tcc_geometry
from power_machine.protection_bridge import tcc_fault_markers
from power_machine.resources import output_root

from ..i18n import tr
from ..theme import theme
from .panel_widgets import finish_panel, element_label, study_panel, ElementSelector, ResultTable, AdvancedOptions, significant, error_result

PANEL_SPEC = {"title_key": 'integration.protection_and_tcc_coordination', "id": "protection", "title": tr('integration.protection_and_tcc_coordination'), "api_version": 1,
              "factory": "create_panel", "order": 50}
# Alias de lectura para pruebas heredadas. Ya no apunta fuera del proyecto.
SOURCE_ROOT = output_root() / "imports" / "protection"


def _exports_directory(root):
    """Carpeta de importación explícita, sin ascender a una fuente externa heredada."""
    configured = os.environ.get("power_machine_PROTECTION_EXPORTS_DIR")
    path = Path(configured).resolve() if configured else Path(root).resolve() / "imports" / "protection"
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError(tr('integration.protection_import_folder_must_remain_within_the'))
    return path


def result_text(result):
    if "progress" in result:
        return tr('integration.protection')+str(result["progress"])
    payload = result.get("payload", {})
    lines = [tr('integration.state')+result.get("status", "sin resultado"), tr('integration.no_pf_validation_f85232'),
             tr('integration.currents')+str(payload.get("current_scope", tr('integration.no_network')))]
    for pair in payload.get("coordination", {}).get("pairs", []):
        margin, current = pair.get("minimum_margin_s"), pair.get("minimum_current_a")
        label = pair["primary_stage_id"]+" → "+pair["backup_stage_id"]
        lines.append(tr('integration.minimum_margin_s_at_a', value0=label, value1=margin, value2=current) if margin is not None else label+tr('integration.no_coverage_119f68'))
    for pair in payload.get("fault_pairs", []) + payload.get("pair_results", []):
        label = pair.get("primary_stage_id", "?")+" → "+pair.get("backup_stage_id", "?")
        margin = pair.get("margin_s")
        coverage = pair.get("coverage", tr('integration.no_coverage'))
        lines.append(tr('integration.margin_s_coverage', value0=label, value1=margin, value2=coverage) if margin is not None else label+": "+coverage)
    for diag in result.get("diagnostics", []):
        lines.append(f"{diag['level']} [{diag['code']}] {diag.get('object_id') or ''}: {diag['message']}")
    return "\n".join(lines)


def draw_tcc(canvas, series, fault_markers=()):
    """Dibuja datos con huecos conservados, ejes log10 y leyenda por ID."""
    canvas.delete("all")
    width, height = max(320, canvas.winfo_width() if hasattr(canvas, "winfo_width") else 900), max(160, canvas.winfo_height() if hasattr(canvas, "winfo_height") else 600)
    # La geometría del motor se transforma solo en coordenadas de pantalla.
    geometry = tcc_geometry(series)
    left, right, top, bottom = 70, width - 25, 55, height - 48
    sx = lambda x: left + (x - 80) / 600 * (right - left)
    sy = lambda y: top + (y - 65) / 470 * (bottom - top)
    canvas.configure(background=theme.color("surface"))
    canvas.create_line(left, top, left, bottom, right, bottom, fill=theme.color("text"))
    unit = "secundaria" if series and series[0].get("current_unit") == "A_secondary" else "primaria"
    canvas.create_text((left+right)/2, height-12, text=tr('integration.current_a_log10', value0=unit))
    canvas.create_text(8, 8, anchor="nw", text=tr('integration.time_s_log10_no_pf_validation'))
    previous = -1000
    for x, label in geometry["ticks_x"]:
        if sx(x) - previous >= 65:
            canvas.create_text(sx(x), bottom+14, text=label)
            previous = sx(x)
    previous = -1000
    for y, label in sorted(geometry["ticks_y"]):
        if sy(y) - previous >= 18:
            canvas.create_text(left-8, sy(y), text=label, anchor="e")
            previous = sy(y)
    for row in geometry["series"]:
        for segment in row["segments"]:
            if len(segment) >= 2:
                canvas.create_line(*[v for x, y in segment for v in (sx(x), sy(y))], fill=theme.color("accent"), dash=row["dash"])
    labels = " / ".join(str(r["label"]) for r in geometry["series"])
    canvas.create_text(left, 30, anchor="nw", width=max(50, width-left-10),
                       text=(labels[:90] + "..." if len(labels)>90 else labels))
    valid = [current for row in series for current, time in row.get("points", ())
             if current is not None and time is not None and current > 0 and time > 0]
    if valid:
        low, high = min(valid), max(valid)
        if low == high:
            low, high = low / 10, high * 10
        for index, marker in enumerate(fault_markers):
            current = marker.get("current_primary_a")
            if not isinstance(current, (int, float)) or current <= 0:
                continue
            x = left + (math.log10(current) - math.log10(low)) / (math.log10(high) - math.log10(low)) * (right-left)
            if left <= x <= right:
                canvas.create_line(x, top, x, bottom, fill=theme.color("warn"), dash=(3, 2))
                label = str(marker.get("label", "Falla"))[:45]
                right_side = x > (left + right) / 2
                canvas.create_text(x - 3 if right_side else x + 3, top+12+14*(index % 4),
                                   anchor="e" if right_side else "w", text=label)


class PanelController:
    """Lógica ensayable con contexto falso, sin referencias de widgets en requests."""

    def __init__(self, context, display=None):
        self.context, self.display = context, display
        self.workspace = new_workspace()
        self.inventory = None
        self.coverage = []
        self.series = []
        self.fault_markers = []
        self.pair_rows = []
        self.last_result = None

    def invalidate(self):
        self.last_result = None
        self.series = []
        self.fault_markers = []
        self.pair_rows = []
        if self.display:
            self.display({"status": tr('integration.no_current_calculation'), "payload": {}, "diagnostics": []})

    def import_exports(self, directory, mapping, time_semantics):
        path = Path(directory).resolve()
        root = Path(self.context.root).resolve()
        if not path.is_relative_to(root):
            raise ValueError(tr('integration.export_must_be_within_the_project_import'))
        self.inventory = read_protection_exports(path)
        model = self.context.get_snapshot()["model"]
        stages, self.coverage, diagnostics = compile_stages(self.inventory, mapping, time_semantics=time_semantics, model=model)
        self.workspace = new_workspace(stages, source_manifest_sha256=self.inventory["source_manifest_sha256"], model_sha256=fingerprint(model))
        self.invalidate()
        self.context.log("INFO", tr('integration.inventory_stages_with_evaluable_mapping', value0=len(self.inventory['stages']), value1=len(stages)))
        return diagnostics

    def begin_import(self, directory, mapping, time_semantics, *, schedule, on_done):
        """Ingestión en hilo de lectura. Solo poll en hilo principal aplica el snapshot."""
        path = Path(directory).resolve()
        root = Path(self.context.root).resolve()
        if not path.is_relative_to(root):
            raise ValueError(tr('integration.export_must_be_within_the_project_import'))
        model = deepcopy(self.context.get_snapshot()["model"])
        initial_model_hash = fingerprint(model)
        initial_workspace_hash = fingerprint(self.workspace)
        mapping = deepcopy(mapping)
        mailbox = queue.Queue()

        def read_source():
            try:
                inventory = read_protection_exports(path)
                stages, coverage, diagnostics = compile_stages(inventory, mapping, time_semantics=time_semantics, model=model)
                mailbox.put((inventory, stages, coverage, diagnostics, None))
            except (ValueError, OSError, KeyError, TypeError) as exc:
                mailbox.put((None, (), (), (), str(exc)))

        def poll():
            try:
                inventory, stages, coverage, diagnostics, error = mailbox.get_nowait()
            except queue.Empty:
                schedule(poll)
                return
            if error:
                self.context.log("ERROR", error)
                return
            if fingerprint(self.context.get_snapshot()["model"]) != initial_model_hash or fingerprint(self.workspace) != initial_workspace_hash:
                self.context.log('AVISO', tr('integration.model_or_settings_changed_during_ingestion_snapshot'))
                return
            self.inventory, self.coverage = inventory, coverage
            self.workspace = new_workspace(stages, source_manifest_sha256=inventory["source_manifest_sha256"], model_sha256=initial_model_hash)
            self.invalidate()
            self.context.log("INFO", tr('integration.inventory_stages_with_evaluable_mapping', value0=len(inventory['stages']), value1=len(stages)))
            on_done(diagnostics)

        worker = threading.Thread(target=read_source, daemon=True)
        worker.start()
        schedule(poll)
        return worker

    def edit(self, stage_id, changes, units):
        self.workspace = revise_settings(self.workspace, stage_id, changes, units=units,
            provenance={"origin": "usuario_workspace_protection"})
        self.invalidate()

    def select(self, stage_id):
        stage = next((s for s in effective_stages(self.workspace) if s.object_id == stage_id), None)
        if stage and stage.protected_element_id:
            self.context.select_element(stage.protected_element_id)

    def submit(self, current_range_a, pairs=(), *, shortcircuit_result=None, terminal_mapping=None):
        snapshot = deepcopy(self.context.get_snapshot())
        self.invalidate()
        data = {"workspace": deepcopy(self.workspace), "workspace_sha256": fingerprint(self.workspace),
                "current_range_a": list(current_range_a), "pairs": [asdict(p) for p in pairs]}
        if shortcircuit_result is not None:
            data.update(shortcircuit_result=deepcopy(shortcircuit_result), shortcircuit_sha256=fingerprint(shortcircuit_result),
                        terminal_mapping=deepcopy(terminal_mapping or {}))
        request = {"schema": "power_machine-parity-request-v1", "module": "protection", "root": str(self.context.root),
                   "snapshot": snapshot, "options": {}, "input": data}
        return self.context.submit_job("protection", request, self.on_message)

    def on_message(self, message):
        if message[0] == "result":
            result = message[1]
            if result.get("status") in ("invalid_input", "unsupported", "numerical_failure"):
                for diag in result.get("diagnostics", []):
                    self.context.log("ERROR", f"[{diag['code']}] {diag['message']}")
            snapshot = self.context.get_snapshot()
            if (result.get("model_sha256") != fingerprint(snapshot["model"])
                or result.get("study_sha256") != fingerprint(snapshot["study"])
                or result.get("variant_sha256") != snapshot["fingerprints"].get("variant_sha256")
                or result.get("payload", {}).get("workspace_sha256") != fingerprint(self.workspace)):
                self.context.log('AVISO', tr('integration.result_from_earlier_settings_or_model_not'))
                return
            self.last_result = result
            self.series = result.get("payload", {}).get("series", [])
            payload = result.get("payload", {})
            bridge_rows = payload.get("fault_currents", [])
            if isinstance(bridge_rows, dict):
                bridge_rows = [{**value, "fault_id": "CC", "stage_id": key, "coverage": "covered"}
                               for key, value in bridge_rows.items()]
            self.fault_markers = tcc_fault_markers(bridge_rows)
            self.pair_rows = payload.get("pair_results", payload.get("fault_pairs", []))
            self.context.publish_result(result)
            self.context.log("INFO", tr('integration.protection')+result["status"]+tr('integration.no_pf_validation_cfb5ca'))
            if self.display:
                self.display(result)
        elif message[0] == "progress" and self.display:
            self.display({"progress": message[1]})
        elif message[0] == "log":
            self.context.log(message[1], message[2])
        elif message[0] == "error":
            self.context.log("ERROR", str(message[1]))
            if self.display:
                self.display(error_result(message[1]))

    def mark_stale(self, reason: str):
        """Contrato común de panel, sin mantener visible un cálculo anterior."""
        self.invalidate()
        if self.display:
            self.display({"status": "obsoleto", "payload": {}, "diagnostics": [
                {"level": "warning", "code": "stale", "message": reason}]})


def create_panel(parent, context) -> ttk.Frame:
    shell, frame, results = study_panel(parent)
    variables = {key: tk.StringVar(frame, value=value) for key, value in (
        ("stage", ""), ("pickup", "100"), ("dial", "0.1"), ("delay", ""),
        ("enabled", "true"), ("family", "iec"), ("curve", "very_inverse"),
        ("minimum", "50"), ("maximum", "10000"), ("primary", ""), ("backup", ""),
        ("breaker", "0.05"), ("margin", "0.3"), ("semantics", "time_with_pickup_s"), ("template", ""))}
    choices = {"enabled": ("true", "false"), "family": ("iec", "ieee", "definite", "instantaneous"),
        "curve": ("normal_inverse", "moderately_inverse", "very_inverse", "extremely_inverse", "long_inverse"),
        "semantics": TIME_COLUMNS}
    labels = (("stage", tr('integration.stage_id')), ("pickup", tr('integration.primary_pickup_a')), ("dial", tr('integration.iec_tms_ieee_td_1')),
        ("delay", tr('integration.definite_delay_s')), ("enabled", tr('integration.stage_enabled')), ("family", "Familia"),
        ("curve", tr('integration.analytical_profile')), ("minimum", tr('integration.minimum_current_primary_a')),
        ("maximum", tr('integration.maximum_current_primary_a')), ("primary", tr('integration.primary_id')), ("backup", tr('integration.backup_id')),
        ("breaker", tr('integration.circuit_breaker_time_s')), ("margin", tr('integration.required_margin_s')), ("semantics", tr('integration.imported_time')),
        ("template", tr('integration.template_curve_id')))
    advanced = AdvancedOptions(frame) if parent is not None else None
    if advanced:
        advanced.grid(row=len(labels), column=0, columnspan=2, sticky="ew")
    for row, (key, label) in enumerate(labels):
        owner = frame if not advanced or key in ("stage", "pickup", "dial", "family", "curve", "minimum", "maximum") else advanced.content
        ttk.Label(owner, text=label).grid(row=row, column=0, sticky="w")
        widget = ttk.Combobox(owner, textvariable=variables[key], values=choices[key], state="readonly") if key in choices else ttk.Entry(owner, textvariable=variables[key])
        widget.grid(row=row, column=1, sticky="ew")
    canvas = tk.Canvas(shell.plot_area, width=900, height=260, background=theme.color("surface"))
    canvas.grid(row=0, column=0, columnspan=4, sticky="nsew")
    output = tk.Text(results, width=65, height=4, state="disabled")
    output.grid(row=1, column=0, columnspan=4, sticky="nsew")
    pair_table = None
    if parent is not None:
        ttk.Label(results, text=tr('integration.protection_pairs_per_fault_margin_and_coverage')).grid(
            row=2, column=0, columnspan=4, sticky="w")
        pair_table = ResultTable(results, (
            ("fault_id", "Falla"), ("primary_stage_id", "Principal"),
            ("backup_stage_id", "Respaldo"), ("margin_s", tr('integration.margin_s')),
            ("required_margin_s", tr('integration.required_margin_s')), ("coverage", tr('integration.coverage'))),
            root=context.root, height=5)
        pair_table.grid(row=3, column=0, columnspan=4, sticky="nsew")
        pair_table.set_column_specs((
            {"key": "margin_s", "unit": "s", "significant_digits": 5, "missing_text": tr('integration.not_evaluable')},
            {"key": "required_margin_s", "unit": "s", "significant_digits": 5, "missing_text": tr('integration.no_data')},
        ))

    results.columnconfigure(0, weight=1)
    shell.plot_area.columnconfigure(0, weight=1)
    shell.plot_area.rowconfigure(0, weight=1)
    results.rowconfigure(1, weight=1)
    def display(result):
        if hasattr(shell, "show_state"):
            shell.show_state(result)
        output.configure(state="normal")
        output.delete("1.0", "end")
        output.insert("end", result_text(result))
        output.configure(state="disabled")
        if pair_table is not None:
            pair_table.set_rows(controller.pair_rows)
        if "progress" not in result:
            draw_tcc(canvas, controller.series, controller.fault_markers)

    controller = PanelController(context, display)
    shell.protection_controller = controller
    if parent is not None:
        canvas.bind("<Configure>", lambda e: draw_tcc(canvas, controller.series))
    selectors = {}
    def refresh_selectors():
        if parent is None:
            return
        model = context.get_snapshot()["model"]
        elements = {x["id"]: x for group in ("branches", "branches3", "gens", "loads", "buses") for x in model.get(group, [])}
        rows = []
        for stage in effective_stages(controller.workspace):
            item = dict(elements.get(stage.protected_element_id, {}))
            item.update(id=stage.object_id, object_id=stage.object_id, name=item.get("name", tr('integration.analytical_profile')) + " / " + stage.object_id)
            rows.append(item)
        for key, row in (("stage", 0), ("primary", 9), ("backup", 10)):
            owner = frame if key == "stage" else advanced.content
            value = variables[key].get()
            if key in selectors:
                selectors[key].set_items(rows, model, preserve_id=False)
                selectors[key].set_id(value)
            else:
                for old in owner.grid_slaves(row=row, column=1):
                    old.destroy()
                selectors[key] = ElementSelector(owner, rows, model, variables[key])
                selectors[key].grid(row=row, column=1, sticky="ew")
    refresh_selectors()

    def guarded(action):
        def execute():
            try:
                action()
            except (ValueError, KeyError, OSError, TypeError, StopIteration) as exc:
                context.log("ERROR", str(exc))
                shell.show_error(exc)
        return execute

    def add_stage():
        family = variables["family"].get()
        curve = standard_curve(family, variables["curve"].get()) if family in ("iec", "ieee") else CurveSpec(family, family, {}, {"origin": "usuario_analítico"})
        stage = RelayStage("power_machine_pro_"+uuid.uuid4().hex, None, "analytical_workspace", None, None,
            float(variables["pickup"].get()), float(variables["dial"].get()) if family in ("iec", "ieee") else None,
            curve, True, "phase", definite_delay_s=float(variables["delay"].get()) if family == "definite" else None)
        data = deepcopy(controller.workspace)
        data["stages"][stage.object_id] = asdict(stage)
        data["revision"] += 1
        controller.workspace = data
        variables["stage"].set(stage.object_id)
        refresh_selectors()
        controller.invalidate()

    def edit():
        changes = {"enabled": variables["enabled"].get() == "true",
                   "pickup_primary_a": float(variables["pickup"].get())}
        stage = next(s for s in effective_stages(controller.workspace) if s.object_id == variables["stage"].get())
        if stage.curve.family in ("iec", "ieee"):
            changes["time_dial"] = float(variables["dial"].get())
        if stage.curve.family == "definite":
            changes["definite_delay_s"] = float(variables["delay"].get())
        controller.edit(stage.object_id, changes, {k: {"enabled": "bool", "pickup_primary_a": "A", "time_dial": "1", "definite_delay_s": "s"}[k] for k in changes})
        controller.select(stage.object_id)

    def submit():
        pairs = ()
        if variables["primary"].get() or variables["backup"].get():
            pairs = (CoordinationPair(variables["primary"].get(), variables["backup"].get(),
                     float(variables["breaker"].get()), float(variables["margin"].get())),)
        controller.submit((float(variables["minimum"].get()), float(variables["maximum"].get())), pairs)

    def save():
        path = filedialog.asksaveasfilename(parent=frame, initialdir=context.root, defaultextension=".json")
        if path:
            save_settings(controller.workspace, confined_path(context.root, path))

    def load():
        path = filedialog.askopenfilename(parent=frame, initialdir=context.root, filetypes=(("Workspace", "*.json"),))
        if path:
            controller.workspace = load_settings(confined_path(context.root, path))
            controller.invalidate()
            variables["stage"].set(next(iter(controller.workspace["stages"]), ""))
            refresh_selectors()

    def ingest():
        directory = filedialog.askdirectory(parent=frame, initialdir=str(_exports_directory(context.root)))
        if not directory:
            return
        mapping_path = filedialog.askopenfilename(parent=frame, initialdir=context.root, title=tr('integration.optional_mapping_json_within_the_project'))
        mapping = json.loads(confined_path(context.root, mapping_path).read_text(encoding="utf-8-sig")) if mapping_path else {}
        semantics = variables["semantics"].get()
        def done(diagnostics):
            variables["stage"].set(next(iter(controller.workspace["stages"]), ""))
            refresh_selectors()
            sample = next((s for s in controller.inventory["stages"] if s["points"]), None)
            if sample:
                variables["template"].set(sample["curve_id"])
                data = template_points(controller.inventory, sample["curve_id"], time_semantics=semantics)
                controller.series = [{**data, "label": data["curve_id"]+tr('integration.template')}]
                draw_tcc(canvas, controller.series, controller.fault_markers)
        context.log("INFO", tr('integration.reading_protection_inventory'))
        controller.begin_import(directory, mapping, semantics, schedule=lambda callback: frame.after(100, callback), on_done=done)

    def export(kind):
        path = filedialog.asksaveasfilename(parent=frame, initialdir=context.root, defaultextension="."+kind)
        if path:
            (export_svg if kind == "svg" else export_points_csv)(controller.series, confined_path(context.root, path))

    def show_template():
        if controller.inventory is None:
            raise ValueError(tr('integration.import_an_inventory_before_selecting_a_template'))
        data = template_points(controller.inventory, variables["template"].get(), time_semantics=variables["semantics"].get())
        controller.invalidate()
        controller.series = [{**data, "label": data["curve_id"]+tr('integration.template')}]
        draw_tcc(canvas, controller.series, controller.fault_markers)

    actions = ((tr('integration.add_analytical_profile'), add_stage), (tr('integration.apply_settings'), edit), (tr('integration.calculate_tcc_and_margin'), submit),
        (tr('integration.import_run_and_mapping'), ingest), (tr('integration.save_new_revision'), save), (tr('integration.open_settings'), load),
        (tr('integration.export_csv'), lambda: export("csv")), (tr('integration.export_svg'), lambda: export("svg")),
        (tr('integration.view_template_by_id'), show_template))
    for index, (label, action) in enumerate(actions):
        from .panel_widgets import action_button
        button = (action_button(frame, 'run' if index == 2 else 'export', text=label, command=guarded(action))
                  if index in (2,6) else ttk.Button(frame, text=label, command=guarded(action)))
        button.grid(row=len(labels)+1+index//2, column=index%2, sticky="ew")
    ttk.Label(frame, text=tr('integration.static_overcurrent_templates_without_mapping_no_pf')).grid(row=len(labels)+6, column=0, columnspan=3, sticky="w")
    frame.columnconfigure(1, weight=1)
    frame.columnconfigure(3, weight=3)
    frame.rowconfigure(len(labels)+6, weight=1)
    return finish_panel(shell, context, controller, tr('integration.iec_ieee_overcurrent_curves_and_static_margin'))
