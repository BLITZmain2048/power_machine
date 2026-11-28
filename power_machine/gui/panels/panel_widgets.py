"""Componentes Tk de estudios. Solo presentación, sin ecuaciones eléctricas."""
import csv
import math
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog
from power_machine.resources import dynamic_catalog_root

from ..i18n import tr, translate_text
from ..theme import theme


def panel_retranslate(widget, callback):
    """Registra una actualización de idioma sin alterar la API de un panel."""
    previous = getattr(widget, "retranslate", None)
    def refresh():
        if callable(previous):
            previous()
        callback()
    widget.retranslate = refresh
    return widget


def style_plot(figure, axes):
    """Aplica la paleta semántica actual a una figura ya creada."""
    colors = theme.color
    figure.set_facecolor(colors("surface"))
    axes.set_facecolor(colors("surface"))
    for spine in axes.spines.values():
        spine.set_color(colors("border"))
    axes.tick_params(colors=colors("text_muted"), labelcolor=colors("text"))
    axes.xaxis.label.set_color(colors("text"))
    axes.yaxis.label.set_color(colors("text"))
    axes.title.set_color(colors("text"))
    axes.grid(True, color=colors("grid"), alpha=.8)


def panel_status_text(value):
    """Traduce estados de presentación heredados sin cambiar su valor canónico."""
    key = {"sin resultado": "no_result", "sin cálculo": "no_calculation",
           "obsoleto": "stale", "vigente": "current", "completo": "complete",
           "parcial": "partial", "no soportado": "unsupported",
           "error de entrada": "input_error", "error": "error",
           "sin validar contra la referencia externa": "not_validated_ref"}.get(str(value))
    return tr("panel.common." + key) if key else human_status(value)


def significant(value):
    """Cuatro cifras significativas, ausencias explícitas y cero conservado."""
    if value is None:
        return tr("panel.common.no_data")
    if isinstance(value, (float, int)) and not isinstance(value, bool):
        return format(value, ".4g") if math.isfinite(value) else tr("panel.common.no_data")
    return human_status(value)


def human_status(value):
    for field, label in (("p_out_of_interface_mw", tr('integration.outgoing_interface_p')), ("q_out_of_interface_mvar", tr('integration.outgoing_interface_q'))):
        if str(value).endswith(" " + field):
            return str(value)[:-len(field)] + label
    return {"almost_neutral": tr('int2.almost_neutral'), "almost neutral": tr('int2.almost_neutral'),
            "uncertain": tr('int2.uncertain'),
            "completed": tr('integration.completed'), "partial": tr('integration.partial'), "unsupported": tr('integration.unsupported'),
            "not_evaluable": tr('integration.not_evaluable'), "invalid_input": tr('integration.invalid_input'), "error": "Error",
            "not_converged": tr('integration.not_converged'), "unsupplied": tr('integration.unsupplied'), "unsolved": tr('integration.unsolved'),
            "solved": "Resuelto", "stable": tr('integration.stable'), "unstable": "Inestable", "declared": "Declarada",
            "excluded": "Excluida", "included": "Incluida", "warning": "Advertencia", "info": tr('integration.information'),
            "min_pu": tr('integration.minimum_voltage'), "max_pu": tr('integration.maximum_voltage'),
            "recovery_time_s": tr('integration.recovery_time'), "nadir_hz": tr('integration.minimum_frequency'),
            "zenith_hz": tr('integration.maximum_frequency'), "power_machinef_hz_s": "power_machineF",
            "power_machinef_abs_max_hz_per_s": tr('integration.maximum_absolute_power_machinef'),
            "q_limited_generator_ids": tr('integration.generators_at_q_limits'),
            "selected_p_mw": tr('integration.selected_p_demand'), "selected_q_mvar": tr('integration.selected_q_demand'),
            "nonselected_p_mw": tr('integration.unselected_p_demand'), "nonselected_q_mvar": tr('integration.unselected_q_demand'),
            "selected_load_ids_solved": tr('integration.solved_selected_loads'),
            "nonselected_load_ids_solved": tr('integration.solved_unselected_loads'),
            "initialization_mode": tr('integration.initialization_mode'), "reactive_limit_mode": tr('integration.q_limit_mode'),
            "real_generator_limits": tr('integration.real_generator_limits'), "selection": tr('integration.selection'),
            "selected_loads": tr('integration.selected_loads'), "excluded_unresolved_loads": tr('integration.excluded_unresolved_loads'),
            "time_outside_band_known_s": tr('integration.known_time_outside_band'),
            "time_outside_band_unknown_s": tr('integration.unknown_time_outside_band'),
            "time_outside_band_s": tr('integration.time_outside_band'), "settling_known_s": tr('integration.known_settling_time'),
            "settling_unknown_s": tr('integration.unknown_settling_time'), "window_start_s": tr('integration.window_start'),
            "window_end_s": tr('integration.window_end'), "cycles_used": tr('integration.cycles_used'), "peak_count": tr('integration.peak_count'),
            "fit_r2": tr('integration.fit_r'), "residual_rms": tr('integration.rms_residual'), "damping_ratio": "Amortiguamiento",
            "frequency_hz": tr('integration.frequency'), "method": tr('integration.method'), "events_sha256": tr('integration.event_fingerprint'),
            "options_sha256": tr('integration.option_fingerprint'), "hv": tr('integration.high_voltage'), "mv": tr('integration.medium_voltage'),
            "lv": tr('integration.low_voltage'), "from": "Origen", "to": tr('integration.destination')}.get(str(value), translate_text(str(value)))


class ScrollForm(ttk.Frame):
    """Formulario vertical. content recibe grid o pack. Rueda local y teclado."""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, height=330, highlightthickness=0, takefocus=True)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.hint = ttk.Label(self, text=tr('int2.more_options'), style='Muted.TLabel', wraplength=302)
        self.hint.grid(row=1,column=0,columnspan=2,sticky='ew')
        self.canvas.configure(yscrollcommand=lambda first,last: self._scroll_status(bar,first,last))
        self.rowconfigure(0,weight=1)
        self.columnconfigure(0,weight=1)
        self.canvas.grid(row=0,column=0,sticky="nsew")
        bar.grid(row=0,column=1,sticky="ns")
        self.content = ttk.Frame(self.canvas, padding=6)
        self.window = self.canvas.create_window(0, 0, anchor="nw", window=self.content)
        self.content.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.window, width=e.width))
        self.bind('<Configure>',lambda e:self.hint.configure(wraplength=max(200,e.width-14)) if e.widget is self else None,add='+')
        self.canvas.bind("<MouseWheel>", self.wheel)
        self.canvas.bind("<Next>", lambda e: self.canvas.yview_scroll(1, "pages"))
        self.canvas.bind("<Prior>", lambda e: self.canvas.yview_scroll(-1, "pages"))
    def _scroll_status(self, bar, first, last):
        bar.set(first,last)
        overflow=self.content.winfo_reqheight()>self.winfo_height()+1
        if overflow and float(last)<0.999:
            if not self.hint.winfo_manager():self.hint.grid(row=1,column=0,columnspan=2,sticky='ew')
        else:self.hint.grid_remove()

    def wheel(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")
        return "break"
    def wire(self):
        def visit(widget):
            widget.bind("<MouseWheel>", self.wheel, add="+")
            widget.bind("<FocusIn>", lambda e, w=widget: self.reveal(w), add="+")
            for child in widget.winfo_children():
                visit(child)
        visit(self.content)
    def reveal(self, widget):
        self.update_idletasks()
        y = widget.winfo_rooty() - self.content.winfo_rooty()
        top = self.canvas.canvasy(0)
        if y < top or y + widget.winfo_height() > top + self.canvas.winfo_height():
            self.canvas.yview_moveto(max(0, y - 10) / max(1, self.content.winfo_height()))


class StudyPanel(ttk.Frame):
    """options y results son contenedores. mark_stale conserva las tablas históricas."""
    def __init__(self, parent):
        super().__init__(parent, padding=6)
        self.freshness = "sin resultado"
        self.coverage = "sin cálculo"
        self.validation_label = "sin validar contra la referencia externa"
        self.state_text = tk.StringVar(self)
        ttk.Label(self, textvariable=self.state_text).pack(fill="x")
        body = ttk.Frame(self)
        body.pack(fill="both", expand=True)
        analysis=getattr(self.winfo_toplevel(),'analysis',None)
        self._analysis_external=analysis is not None
        left = ttk.Frame(analysis.options_host if analysis is not None else body, width=340)
        if analysis is None:left.pack(side="left", fill="y")
        else:
            def dispose_external(event):
                if event.widget is self and left.winfo_exists():
                    from ..panel_context import cancel_panel_timers
                    cancel_panel_timers(left)
                    left.destroy()
            self.bind('<Destroy>',dispose_external,add='+')
        left.pack_propagate(False)
        self.form_host = left
        left._study_panel_owner=self
        self.form = ScrollForm(left)
        self.form.pack(fill="both", expand=True)
        self.options = self.form.content
        self.actions = ttk.Frame(left, padding=(2, 6))
        self.actions.pack(fill="x", side="bottom")
        self.split = ttk.Panedwindow(body, orient="vertical")
        self.split.pack(side="left", fill="both", expand=True, padx=(8, 0))
        self.results = ttk.Frame(self.split)
        self.plot_area = ttk.Frame(self.split)
        self.split.add(self.results, weight=1)
        self.split.add(self.plot_area, weight=1)
        self._split_size = None
        def resize_results(event):
            size = (event.width, event.height)
            if size != self._split_size:
                self._split_size = size
                self.after_idle(lambda: self.split.sashpos(0, int(self.split.winfo_height() * .48)) if self.winfo_exists() else None)
        self.split.bind("<Configure>", resize_results)
        def keep_results_visible(event):
            height = self.split.winfo_height()
            minimum = min(150, int(height * .4))
            if height > 120 and (self.results.winfo_height() < minimum or self.plot_area.winfo_height() < minimum):
                self.after_idle(lambda: self.split.sashpos(0, int(self.split.winfo_height() * .48)) if self.winfo_exists() else None)
        self.results.bind("<Configure>", keep_results_visible)
        self.plot_area.bind("<Configure>", keep_results_visible)
        self.set_status("sin cálculo", freshness="sin resultado", validation_label=self.validation_label)
        self.after_idle(self.form.wire)
    def mark_stale(self, reason):
        self.set_status(self.coverage, freshness="obsoleto", validation_label=self.validation_label, reason=reason)
        if hasattr(self, "refresh_capabilities"):
            self.refresh_capabilities()
    def set_status(self, status, *, freshness, validation_label, reason="", coverage=None):
        self.freshness = freshness
        self.coverage = coverage or {"completed": "completo", "partial": "parcial", "unsupported": "no soportado",
                                   "invalid_input": "error de entrada", "error": "error"}.get(status, status)
        self.validation_label = validation_label
        self.state_text.set(" | ".join((panel_status_text(freshness).upper(),
                                        panel_status_text(self.coverage),
                                        panel_status_text(validation_label))))
        self.status_reason = str(reason)
        if hasattr(self, "diagnostics") and reason:
            self.set_diagnostics([{"message": str(reason)}])
    def retranslate(self):
        self.state_text.set(" | ".join((panel_status_text(self.freshness).upper(),
                                        panel_status_text(self.coverage),
                                        panel_status_text(self.validation_label))))
        if hasattr(self, "capability_gate"):
            self.capability_gate.refresh()
    def show_state(self, result):
        self.set_status(result.get("status", tr('integration.calculating')), freshness=self.freshness,
                        validation_label=self.validation_label)
    def show_error(self, message):
        self.set_status("error", freshness=self.freshness, validation_label=self.validation_label, reason=message)
    def set_diagnostics(self, rows, on_select=None):
        self.diagnostic_rows = list(rows)
        if hasattr(self, "diagnostics"):
            self.diagnostics.set_result({"diagnostics": self.diagnostic_rows})
            if on_select:
                self.diagnostics.text.bind("<Double-1>", lambda e: on_select(self.diagnostic_rows))


def study_panel(parent):
    """Devuelve panel, formulario y resultados. Admite dobles estructurales sin Tcl."""
    if parent is None:
        frame = ttk.Frame(parent)
        frame.plot_area = ttk.Frame(frame)
        frame.mark_stale = lambda reason: setattr(frame, "status_reason", str(reason))
        return frame, frame, frame
    panel = StudyPanel(parent)
    return panel, panel.options, panel.results


class AdvancedOptions(ttk.Frame):
    """Opciones plegables. content permite componer controles con grid."""
    def __init__(self, parent):
        super().__init__(parent)
        self.content = ttk.Frame(self)
        self.open = tk.BooleanVar(self, False)
        self.toggle_button = ttk.Checkbutton(self, text=tr("panel.common.advanced_options"), variable=self.open, command=self.toggle)
        self.toggle_button.pack(anchor="w")
        self.retranslate = lambda: self.toggle_button.configure(text=tr("panel.common.advanced_options"))
    def toggle(self):
        if self.open.get():
            self.content.pack(fill="x")
        else:
            self.content.pack_forget()


def element_label(item, model):
    from ..presentation import element_label as display_label
    return display_label(item, model)


def _selector_choices(items, model):
    choices,terms={},{}
    for item in items:
        label=element_label(item,model)
        ident=item.get('object_id',item.get('id'))
        if label in choices:label+=f' [{ident}]'
        choices[label]=ident
        terms[label]=' '.join(str(v or '') for v in (label,ident,item.get('name'))).casefold()
    return choices,terms


class ElementSelector(ttk.Combobox):
    """Búsqueda incremental por etiqueta e ID. id_variable recibe solo la identidad."""
    def __init__(self, parent, items, model, id_variable, **kwargs):
        self.id_variable = id_variable
        self.label_variable = tk.StringVar(parent)
        self.choices,self.search_terms = _selector_choices(items,model)
        super().__init__(parent, textvariable=self.label_variable, values=tuple(self.choices), **kwargs)
        self.set_id(id_variable.get())
        def sync(*args):
            value=id_variable.get()
            self.label_variable.set(next((k for k,v in self.choices.items() if v==value),value))
        self._id_trace=id_variable.trace_add('write',sync)
        self.bind('<Destroy>',lambda event: id_variable.trace_remove('write',self._id_trace) if event.widget is self else None,add='+')
        self.bind("<<ComboboxSelected>>", self.choose)
        self.bind("<KeyRelease>", self.search)
    def set_id(self, value):
        self.id_variable.set(value)
        self.label_variable.set(next((k for k, v in self.choices.items() if v == value), value))
    def choose(self, event=None):
        self.id_variable.set(self.choices.get(self.get(), ""))
    def search(self, event=None):
        query = self.get().casefold()
        self.configure(values=[k for k in self.choices if query in self.search_terms[k]])
        typed=self.get()
        self.choose()
        self.label_variable.set(typed)
    def set_items(self, items, model, preserve_id=True):
        previous = self.get_id() if preserve_id else None
        self.choices,self.search_terms = _selector_choices(items,model)
        self.configure(values=tuple(self.choices))
        self.set_id(previous if previous in self.choices.values() else "")
    def get_id(self):
        return self.choices.get(self.get())


class ResultTable(ttk.Frame):
    """columns = pares clave/rótulo con unidad. CSV conserva valores crudos y vacíos."""
    def __init__(self, parent, columns, root=None, height=4):
        super().__init__(parent)
        self.columns, self.root, self.rows = [(key,translate_text(label)) for key,label in columns], root, []
        self.predicate = None
        self.specs = {}
        self.tree = ttk.Treeview(self, columns=[k for k, _ in columns], show="headings", height=height)
        for key, label in columns:
            self.tree.heading(key, text=label, command=lambda k=key: self.sort(k))
            self.tree.column(key, width=max(130, len(label)*7+20), minwidth=max(100, len(label)*7+20), stretch=True)
        self.tree.grid(row=0, column=0, sticky="nsew")
        ttk.Scrollbar(self, orient="vertical", command=self.tree.yview).grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=self._yscroll)
        self.vbar = self.grid_slaves(row=0, column=1)[0]
        xbar = ttk.Scrollbar(self, orient="horizontal", command=self.tree.xview)
        xbar.grid(row=1, column=0, sticky="ew")
        self.tree.configure(xscrollcommand=xbar.set)
        ttk.Button(self, text=tr('integration.export_table_csv'), command=self.browse_export).grid(row=2, column=0, sticky="w")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.reverse = False
    def _yscroll(self, first, last):
        if hasattr(self, "vbar"):
            self.vbar.set(first, last)
    def set_rows(self, rows):
        self.rows = list(rows)
        self._render()
    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for row in self.visible_rows():
            values = []
            for key, _ in self.columns:
                spec = self.specs.get(key, {})
                value = row.get(key)
                values.append(spec.get("missing_text", tr('integration.no_data')) if value is None else
                              format(value, f".{spec.get('significant_digits', 4)}g") if isinstance(value, (int, float)) else human_status(value))
            self.tree.insert("", "end", values=values)
    def visible_rows(self):
        return [row for row in self.rows if not self.predicate or self.predicate(row)]
    def set_filter(self, predicate):
        self.predicate = predicate
        self._render()
    def set_column_specs(self, specs):
        self.specs = {row["key"]: dict(row) for row in specs}
        for key, label in self.columns:
            spec = self.specs.get(key, {})
            self.tree.heading(key, text=spec.get("label", label) + (f" [{spec['unit']}]" if spec.get("unit") else ""))
        self._render()
    def sort(self, key):
        def order(row):
            value = row.get(key)
            return (value is None, isinstance(value, str), value if value is not None else "")
        present = [r for r in self.rows if r.get(key) is not None]
        absent = [r for r in self.rows if r.get(key) is None]
        self.set_rows(sorted(present, key=order, reverse=self.reverse) + absent)
        self.reverse = not self.reverse
    def export_csv(self, path, *, scope="visible"):
        if scope not in ("visible", "all"):
            raise ValueError(tr('integration.invalid_csv_scope'))
        path = Path(path).resolve()
        if self.root and not path.is_relative_to(Path(self.root).resolve()):
            raise ValueError(tr('integration.csv_must_remain_within_the_project'))
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow([self.tree.heading(key, "text") for key, _ in self.columns])
            writer.writerows([[row.get(k) for k, _ in self.columns] for row in (self.visible_rows() if scope == "visible" else self.rows)])
    def browse_export(self):
        path = filedialog.asksaveasfilename(parent=self, initialdir=self.root, defaultextension=".csv")
        if path:
            self.export_csv(path)


def error_result(message):
    """Diagnóstico local de transporte, jamás se publica como resultado del motor."""
    return {"status": "error", "payload": {}, "diagnostics": [
        {"level": "error", "code": "worker_error", "message": str(message)}]}


CONCLUSION_SCOPE = {
    "calc_lfctl": tr('integration.may_propose_discrete_movements_and_check_explicit'),
    "contingency": tr('integration.may_establish_island_solutions_isolated_demand_and'),
    "calc_pvqv": tr('integration.pv_response_depends_on_the_internal_growth'),
    "calc_sens": tr('integration.may_establish_local_derivatives_and_approximation_error'),
    "calc_ccbar": tr('integration.may_establish_solutions_of_qualified_profiles_with'),
    "shortcircuit": tr('integration.may_establish_solutions_of_qualified_profiles_with_9b5b78'),
    "calc_cct": tr('integration.may_classify_simulated_points_using_the_stated'),
    "calc_met": tr('integration.may_establish_extrema_and_temporal_metrics_with'),
    "calc_qds": tr('integration.may_establish_quasi_static_evolution_and_known'),
    "modal": tr('integration.modes_of_the_declared_continuous_profile_and'),
    "dsl_editor": tr('integration.compilation_and_isolated_testing_of_the_supported')}

DIAGNOSTIC_LABELS = {
    "unsupplied": tr('integration.de_energized_islands'), "unsolved": tr('integration.islands_without_a_solution_reference'),
    "not_converged": tr('integration.energized_islands_without_convergence'), "rating_missing": tr('integration.missing_rating_or_untraceable_unit'),
    "q_limits_missing": tr('integration.reactive_limits_missing'), "unsupported": tr('integration.outside_the_supported_domain'),
    "omitted_loads_shunts": tr('integration.loads_and_magnetization_omitted_in_the_short'),
    "missing_reference": tr('integration.angular_reference_missing'), "numerical_failure": tr('integration.numerical_method_failed'),
    "worker_error": tr('integration.calculation_failed'), "cct_partial": tr('integration.search_without_a_qualified_cct_interval'),
    "unsupported_peak_profile": tr('integration.peak_current_outside_coverage'),
    "unsupported_breaking_profile": tr('integration.breaking_current_outside_coverage'),
    "unsupported_thermal_profile": tr('integration.thermal_current_outside_coverage'),
    "equivalent_loads_selected": tr('integration.explicit_equivalent_load_selection_extended_scope'),
    "control_regime_changed": tr('integration.control_regime_changed_conclusions_blocked')}


def diagnostic_lines(result):
    """Agrupa mensajes de presentación, conservando originales en el detalle técnico."""
    import re
    rows = result.get("diagnostics", [])
    base = result.get("payload", {}).get("base_unresolved_islands", [])
    lines = [tr('integration.warning_de_energized_islands_in_the_base', value0=len(base))] if base and all(row.get("status") in ("unsupplied", "unsolved", "sin_referencia") for row in base) else [tr('integration.warning_unresolved_islands_in_the_base_case', value0=len(base))] if base else []
    groups = {}
    for row in rows:
        code = row.get("code", "")
        if base and code in ("unsupplied", "unsolved"):
            continue
        level = {"warning": "Advertencia", "warn": "Advertencia", "error": "Error", "info": tr('integration.information')}.get(str(row.get("level", "info")).lower(), tr('integration.information'))
        message = str(row.get("message", ""))
        label = DIAGNOSTIC_LABELS.get(code)
        if label is None:
            label = message if re.search(r"[áéíóúñ¿]|\b(El|La|Los|Las|No|Sin|Se|Falta|Datos|Perfil|Barra|[Bb]andas|Límite|Resultado|Corriente)\b", message) else tr('integration.calculation_requires_reviewing_data_coverage_or_configuration')
            for english, spanish in (("unsupplied", "sin suministro"), ("not_converged", "sin convergencia"), ("unsupported", "no soportado"), ("completed", "completado"), ("partial", "parcial")):
                label = re.sub(r"\b" + english + r"\b", spanish, label)
        groups[(level, label)] = groups.get((level, label), 0) + 1
    for (level, label), count in groups.items():
        lines.append(f"{level}: {label}" + (tr('integration.grouped_notices', value0=count) if count > 1 else ""))
    return lines or [tr('integration.no_calculation_notices')]


class DiagnosticText(ttk.Frame):
    """Texto diagnóstico desplazable. set_result conserva códigos y objetos."""
    def __init__(self, parent, height=6):
        super().__init__(parent)
        self.text = tk.Text(self, height=height, wrap="word", state="disabled")
        self.text.grid(row=0, column=0, sticky="nsew")
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview)
        bar.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=bar.set)
        ttk.Button(self, text=tr('integration.original_technical_details'), command=self.details).grid(row=1, column=0, columnspan=2, sticky="w")
    def recover(self):
        """Lleva al dato expl?cito, sin cambiar el modelo ni seleccionar par?metros."""
        action = getattr(self.panel, 'recovery_action', None)
        if action:
            action()
        else:
            for widget in self.descendants(self.panel.options):
                if isinstance(widget, AdvancedOptions):
                    widget.open.set(True)
                    widget.toggle()
            controls = [w for w in self.descendants(self.panel.options)
                        if isinstance(w, (ttk.Entry, ttk.Combobox))]
            target = next((w for w in controls if not w.get().strip()), controls[0] if controls else None)
            if target is not None:
                self.panel.form.reveal(target)
                target.focus_set()
            else:
                app = self.panel.winfo_toplevel()
                if hasattr(app, 'open_case'):
                    app.open_case()
        self.schedule()

    def details(self):
        import json
        window = tk.Toplevel(self)
        window.title(tr('integration.original_engine_technical_data'))
        text = tk.Text(window, wrap="word", width=100, height=25)
        text.pack(fill="both", expand=True)
        text.insert("end", json.dumps(getattr(self, "result", {}), ensure_ascii=False, indent=2))
        text.configure(state="disabled")
    def set_result(self, result):
        self.result = result
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.insert("end", "\n".join(diagnostic_lines(result)))
        self.text.configure(state="disabled")



def action_button(parent, role, **kwargs):
    """Register a source action by a stable role before common layout is built."""
    button = ttk.Button(parent, **kwargs)
    button.action_id = role
    owner = parent
    while owner is not None and not isinstance(owner, StudyPanel):
        if hasattr(owner,'_study_panel_owner'):
            owner=owner._study_panel_owner
            break
        owner = getattr(owner, 'master', None)
    if owner is not None:
        if not hasattr(owner, '_action_sources'): owner._action_sources = {}
        owner._action_sources[role] = button
    return button


def finish_panel(panel, context, controller, scope):
    """Integra disposición y presentación sin cambiar peticiones ni ecuaciones."""
    if not isinstance(panel, StudyPanel):
        return panel
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from descendants(child)
    sources = getattr(panel, '_action_sources', {})
    execute, cancel, export = (sources.get(role) for role in ('run','cancel','export'))
    for button in (execute, cancel, export):
        if button:
            button.grid_remove() if button.winfo_manager() == "grid" else button.pack_forget()
    tables = []
    for widget in descendants(panel.results):
        if isinstance(widget, ttk.Treeview):
            tables.append(instrument_tree(widget))
    def export_table():
        if not tables:
            return
        path = filedialog.asksaveasfilename(parent=panel, initialdir=context.root, defaultextension=".csv",
                                           title=tr('int2.export_table'))
        if path:
            destination = Path(path).resolve()
            if not destination.is_relative_to(Path(context.root).resolve()):
                panel.show_error(tr('integration.csv_must_remain_within_the_project'))
                return
            table = tables[0]
            with destination.open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow([table.heading(k, "text") for k in table["columns"]])
                writer.writerows(table.raw_rows.get(i, ()) for i in table.get_children())
            context.log('INFO', tr('int2.exported', name=destination.name))
    def export_default():
        result = getattr(controller, "last_result", None) or getattr(controller, "result", None)
        if not result and getattr(context, 'results', None):
            result = next(reversed(context.results.values()))
        if not result:
            panel.show_error(tr('integration.run_the_calculation_before_exporting'))
            return
        path = filedialog.asksaveasfilename(parent=panel, initialdir=context.root, defaultextension=".json",
                                           title=tr('int2.export_result'))
        if path:
            destination = Path(path).resolve()
            if not destination.is_relative_to(Path(context.root).resolve()):
                panel.show_error(tr('integration.exports_must_remain_inside_the_project'))
                return
            import json
            destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            context.log('INFO', tr('int2.exported', name=destination.name))
    def cancel_default():
        operation = getattr(controller, "cancel", None)
        if operation:
            operation()
        else:
            worker = getattr(context, "worker", None)
            if worker and hasattr(worker, "cancel"):
                worker.cancel()
    panel.action_buttons = {}
    for column, (label, command) in enumerate(((tr("panel.common.run"), execute.invoke if execute else lambda: None),
                                               (tr("panel.common.cancel"), cancel.invoke if cancel else cancel_default),
                                               (tr("int2.export_result"), export_default))):
        role = ('run','cancel','export')[column]
        button = ttk.Button(panel.actions, text=label, command=command)
        button.action_id = role
        button.grid(row=1 if role == 'export' else 0, column=0 if role == 'export' else column,
                    columnspan=2 if role == 'export' else 1, sticky="ew", pady=(2,0))
        panel.action_buttons[role] = button
        if column < 2:
            panel.actions.columnconfigure(column, weight=1)
    if tables:
        panel.export_table_button = ttk.Button(panel.actions, text=tr('int2.export_table'), command=export_table)
        panel.export_table_button.grid(row=2, column=0, columnspan=2, sticky='ew', pady=(4,0))
    panel.empty_hint = ttk.Label(panel.results, text=tr('int2.empty_study'), wraplength=450,
                                 justify='center', padding=12)
    panel.empty_hint.place(relx=.5, rely=.5, anchor='center')
    panel.plot_empty_hint = ttk.Label(panel.plot_area, text=tr('int2.empty_study'), wraplength=500,
                                      justify='center', padding=20)
    panel.plot_empty_hint.place(relx=0, rely=0, relwidth=1, relheight=1)
    if not any(isinstance(w, AdvancedOptions) for w in descendants(panel.options)):
        advanced = AdvancedOptions(panel.options)
        advanced.grid(row=999, column=0, sticky="ew")
        terms = ("paso mínimo", "factor de potencia", "límite q fuente", "paso rms", "ángulo máximo",
                 "recuperación", "rf explícita", "xf explícita", "manifiesto", "serie csv", "modelo csv",
                 "perfil json", "paso δp", "paso δq", "incluir aportes")
        for child in list(panel.options.grid_slaves()):
            if isinstance(child, (ttk.Label, ttk.Checkbutton)) and any(term in str(child.cget("text")).casefold() for term in terms):
                row = int(child.grid_info()["row"])
                for field in list(panel.options.grid_slaves(row=row)):
                    field.grid_configure(in_=advanced.content)
    # En 340 px las etiquetas quedan encima de los campos, nunca los comprimen.
    def compact(owner):
        children = owner.grid_slaves()
        rows = {}
        for child in children:
            info = child.grid_info()
            rows.setdefault(int(info["row"]), []).append((int(info["column"]), child))
        index = 0
        for original_row in sorted(rows):
            for _, child in sorted(rows[original_row], key=lambda pair: pair[0]):
                child.grid_configure(row=index, column=0, columnspan=1, sticky="ew")
                if isinstance(child, ttk.Label):
                    child.configure(wraplength=302)
                if isinstance(child, (ttk.Frame,)):
                    compact(child)
                    if isinstance(child, AdvancedOptions):
                        compact(child.content)
                index += 1
        owner.columnconfigure(0, weight=1)
        for col in range(1, 5):
            owner.columnconfigure(col, weight=0, minsize=0)
        for child in owner.pack_slaves():
            if isinstance(child, ttk.Frame):
                compact(child)
                if isinstance(child, AdvancedOptions):
                    compact(child.content)
    compact(panel.options)
    for child in descendants(panel.options):
        if isinstance(child, ttk.Label):
            child.configure(wraplength=302)
    for child in descendants(panel.plot_area):
        if isinstance(child, ttk.Frame):
            child.columnconfigure(0, weight=1)
            child.rowconfigure(0, weight=1)
    if not panel.plot_area.winfo_children():
        panel.diagnostics = DiagnosticText(panel.plot_area)
        panel.diagnostics.pack(fill="both", expand=True)
        panel.set_diagnostics([{"message": scope}])
    for attribute in ("on_update", "display", "on_display"):
        original = getattr(controller, attribute, None)
        if not callable(original):
            continue
        def receive(message, callback=original):
            callback(message)
            if isinstance(message, dict) and "status" in message:
                panel.empty_hint.place_forget()
                panel.plot_empty_hint.place_forget()
                freshness = ("obsoleto" if message.get("stale") else "vigente") if message.get("schema") else panel.freshness
                panel.set_status(message["status"], freshness=freshness,
                                 validation_label=panel.validation_label, reason=message.get("stale_reason", ""))
                panel.diagnostics.set_result(message) if hasattr(panel, "diagnostics") else panel.set_diagnostics(message.get("diagnostics", []))
                for table in tables:
                    table.xview_moveto(0)
            elif isinstance(message, dict) and "error" in message:
                panel.show_error(message["error"])
            elif isinstance(message, (list, tuple)) and message and message[0] == "error":
                panel.show_error(message[1])
        setattr(controller, attribute, receive)
    def translate_actions():
        StudyPanel.retranslate(panel)
        for role, button in panel.action_buttons.items():
            button.configure(text=tr('int2.export_result' if role == 'export' else 'panel.common.'+role))
        panel.empty_hint.configure(text=tr('int2.empty_study'))
        panel.plot_empty_hint.configure(text=tr('int2.empty_study'))
        if hasattr(panel, 'export_table_button'):
            panel.export_table_button.configure(text=tr('int2.export_table'))
        panel.form.hint.configure(text=tr('int2.more_options'))
    panel_retranslate(panel, translate_actions)
    panel.form.wire()
    panel_id = type(controller).__module__.rsplit(".", 1)[-1]
    if panel_id != "protection":
        panel.capability_gate = CapabilityGate(panel, context, controller, panel_id)
    return panel


def instrument_tree(tree):
    """Conserva valores previos al formato para ordenar y exportar con precisión."""
    if hasattr(tree, "raw_rows"):
        return tree
    if isinstance(tree.master, ResultTable) and not getattr(tree, "_direct_inserts", False):
        return tree
    tree.raw_rows = {}
    insert, delete = tree.insert, tree.delete
    def add(parent, index, iid=None, **kwargs):
        values = tuple(kwargs.get("values", ()))
        kwargs["values"] = [significant(v) for v in values]
        for position, key in enumerate(tree["columns"]):
            if position < len(values) and isinstance(values[position], str):
                text = values[position]
                if key in ("state", "status", "indicator"):
                    width = max(int(tree.column(key, "minwidth")), min(300, len(significant(text))*7+20))
                    tree.column(key, minwidth=width, width=width)
        ident = insert(parent, index, iid=iid, **kwargs)
        tree.raw_rows[ident] = values
        return ident
    def remove(*items):
        for item in items:
            tree.raw_rows.pop(item, None)
        return delete(*items)
    tree.insert, tree.delete = add, remove
    def details(event):
        ident = tree.identify_row(event.y)
        if ident in tree.raw_rows:
            window = tk.Toplevel(tree)
            window.title(tr('integration.row_details_original_values'))
            text = tk.Text(window, width=90, height=18, wrap="word")
            text.pack(fill="both", expand=True)
            for key, value in zip(tree["columns"], tree.raw_rows[ident]):
                text.insert("end", f"{tree.heading(key, 'text')}: {value if value is not None else tr('integration.no_data')}\n")
            text.configure(state="disabled")
    tree.bind("<Double-1>", details, add="+")
    for position, key in enumerate(tree["columns"]):
        descending = [False]
        def sort(pos=position, direction=descending):
            def value(item):
                row = tree.raw_rows.get(item, ())
                v = row[pos] if pos < len(row) else None
                return (isinstance(v, str), v if v is not None else "")
            present, absent = [], []
            for item in tree.get_children():
                row = tree.raw_rows.get(item, ())
                (present if pos < len(row) and row[pos] is not None else absent).append(item)
            for index, item in enumerate(sorted(present, key=value, reverse=direction[0]) + absent):
                tree.move(item, "", index)
            direction[0] = not direction[0]
        tree.heading(key, command=sort)
        width = max(110, len(str(tree.heading(key, "text")))*7+20)
        tree.column(key, width=width, minwidth=width, stretch=True)
    # Las barras no quitan altura al gráfico ni se desplazan con el formulario.
    parent = tree.master
    info = tree.grid_info()
    if info and not isinstance(parent, ResultTable) and not getattr(parent, "_table_gaps", False):
        old_rows = {int(widget.grid_info()["row"]) for widget in parent.grid_slaves()}
        for row in range(max(old_rows, default=0) + 1):
            parent.rowconfigure(row, weight=0, minsize=0)
        for widget in parent.grid_slaves():
            widget.grid_configure(row=int(widget.grid_info()["row"]) * 2)
        parent._table_gaps = True
        info = tree.grid_info()
    if info and not isinstance(parent, ResultTable):
        row = int(info["row"])
        parent.rowconfigure(row, weight=1)
        xbar = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        xbar.grid(row=row+1, column=20, sticky="ew")
        ybar = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        ybar.grid(row=row, column=21, sticky="ns")
        # La barra horizontal se coloca dentro de la celda de tabla con pack si
        # su contenedor ya ofrece una barra propia. El contenido conserva xview.
        xbar.grid_configure(column=int(info["column"]), columnspan=int(info.get("columnspan", 1)))
        tree.configure(xscrollcommand=xbar.set, yscrollcommand=ybar.set)
    return tree


def format_axes(axes):
    """Ejes lineales con valores absolutos, sin desplazamiento ni notación científica."""
    from matplotlib.ticker import ScalarFormatter
    for axis, scale in ((axes.xaxis, axes.get_xscale()), (axes.yaxis, axes.get_yscale())):
        if scale == "linear" and isinstance(axis.get_major_formatter(), ScalarFormatter):
            formatter = ScalarFormatter(useOffset=False)
            formatter.set_scientific(False)
            axis.set_major_formatter(formatter)


def result_pages(parent):
    """Navegación por lista, conserva el ancho de tablas con ventanas pequeñas."""
    shell = ttk.Frame(parent)
    shell.grid(row=0, column=0, sticky="nsew")
    shell.columnconfigure(0, weight=1)
    shell.rowconfigure(1, weight=1)
    selected = tk.StringVar(shell)
    selector = ttk.Combobox(shell, textvariable=selected, state="readonly", width=24)
    selector.grid(row=0, column=0, sticky="ew")
    style = ttk.Style(parent)
    style.layout("Results.TNotebook.Tab", [])
    notebook = ttk.Notebook(shell, style="Results.TNotebook")
    notebook.grid(row=1, column=0, sticky="nsew")
    def refresh(event=None):
        titles = [notebook.tab(tab, "text") for tab in notebook.tabs()]
        selector.configure(values=titles)
        if notebook.select():
            selected.set(notebook.tab(notebook.select(), "text"))
    selector.bind("<<ComboboxSelected>>", lambda e: notebook.select(notebook.tabs()[selector.current()]))
    notebook.bind("<<NotebookTabChanged>>", refresh)
    notebook.after_idle(refresh)
    return notebook


class CapabilityGate:
    """Puerta común basada en la calificación oficial, tanto para UI como para despacho."""
    MODULES = {"calc_lfctl": "lf_controls", "shortcircuit": "sc_sweep", "contingency": "contingency", "modal": "modal",
               "dsl_editor": "dsl_editor", "calc_pvqv": "voltage_stability", "calc_sens": "sensitivities",
               "calc_ccbar": "sc_sweep", "calc_cct": "cct", "calc_met": "dyn_metrics", "calc_qds": "qds_panel"}

    def __init__(self, panel, context, controller, panel_id):
        self.panel, self.context, self.controller = panel, context, controller
        self.module = self.MODULES[panel_id]
        self.panel_id = panel_id
        self.pending = None
        self.last = {}
        self.summary = tk.StringVar(panel, tr("panel.common.preflight_pending"))
        self.restrictions_text = tk.StringVar(panel, tr("panel.common.qualification_pending"))
        scope_box = AdvancedOptions(panel.form.master)
        scope_box.winfo_children()[1].configure(text=tr("panel.common.current_scope_restrictions"))
        ttk.Label(scope_box.content, textvariable=self.restrictions_text, wraplength=302).pack(fill="x")
        scope_box.pack(fill="x", before=panel.form, padx=6)
        ttk.Label(scope_box.content, text=tr('int2.scope_hint'), wraplength=302).pack(fill='x')
        def widen():
            panel.form_host.configure(width=440 if int(panel.form_host.cget('width')) < 400 else 340)
        self.width_button = ttk.Button(scope_box.content, text=tr('int2.widen_options'), command=widen)
        self.width_button.pack(fill='x')
        ttk.Label(panel.form.master, textvariable=self.summary, wraplength=310).pack(fill="x", before=panel.form, padx=6)
        self.details_button = ttk.Button(panel.form.master, text=tr("panel.common.coverage_missing"), command=self.details)
        self.details_button.pack(
            fill="x", before=panel.form, padx=6, pady=(0, 4))
        self.recovery_button = ttk.Button(panel.form.master, text=tr('int2.review_inputs'), command=self.recover)
        self.recovery_button.pack(fill='x', before=panel.form, padx=6, pady=(0,4))
        self.execute_button = panel.action_buttons['run']
        original_submit = context.submit_job
        def submit(module, request, callback):
            if not self.refresh(request):
                context.log('AVISO', tr('integration.calculation_blocked_by_preflight') + self.summary.get())
                return None
            return original_submit(module, request, callback)
        context.submit_job = submit
        for widget in self.descendants(panel.options):
            if isinstance(widget, tk.Listbox):
                widget.bind("<<ListboxSelect>>", self.schedule, add="+")
            if isinstance(widget, (ttk.Entry, ttk.Combobox, ttk.Checkbutton)):
                key = "variable" if isinstance(widget, ttk.Checkbutton) else "textvariable"
                name = str(widget.cget(key))
                if name:
                    panel.tk.call("trace", "add", "variable", name, "write", panel.register(self.schedule))
            widget.bind("<FocusOut>", self.schedule, add="+")
        self.refresh()
        panel.refresh_capabilities = self.refresh

    @staticmethod
    def descendants(widget):
        for child in widget.winfo_children():
            yield child
            yield from CapabilityGate.descendants(child)

    def schedule(self, *args):
        if self.pending is not None:
            self.panel.after_cancel(self.pending)
        self.pending = self.panel.after(150, self.refresh)

    def refresh(self, request=None):
        from copy import deepcopy
        from power_machine.study_capabilities import capabilities
        self.pending = None
        try:
            snapshot = deepcopy(request.get("snapshot") if request else self.context.get_snapshot())
            local_error = None
            try:
                if request:
                    supplied = {"input": deepcopy(request.get("input", {})), "options": deepcopy(request.get("options", {}))}
                    if self.module == "dyn_metrics":
                        supplied.update(verified_metric_inputs(self.context.root, supplied["input"].get("run_directory", "")))
                    if self.module == "qds_panel":
                        supplied.update(verified_qds_inputs(self.context.root, snapshot["model"], supplied["input"].get("profile", {})))
                else:
                    supplied = getattr(self.panel, "preflight_inputs", lambda: {})()
            except Exception as exc:
                supplied, local_error = ({"input": deepcopy(request.get("input", {})), "options": deepcopy(request.get("options", {}))} if request else {}), str(exc)
            if request:
                supplied.update(input=deepcopy(request.get("input", {})), options=deepcopy(request.get("options", {})))
            if self.module == "lf_controls":
                from power_machine.lf_controls import preflight
                preflight(snapshot["model"], supplied.get("input", {}), supplied.get("options", {}))
            if self.module == "sc_sweep":
                try:
                    supplied = sc_preflight_inputs(self.context.root, snapshot["model"], supplied)
                except Exception as exc:
                    local_error = str(exc)
            capability_model = supplied.pop("model", snapshot["model"])
            study = snapshot.get("study", {})
            study.setdefault("capability_inputs", {})[self.module] = supplied
            self.report = capabilities(capability_model, study)
            row = self.report["calculations"].get(self.module)
            if row is None:
                # El servicio no califica estos tres motores. Se declara ese límite.
                blockers = []
                from .modal import modal_base_supported
                if self.module == "modal" and not modal_base_supported(snapshot["model"].get("meta", {})):
                    blockers.append({"code": "electrical_base", "message": tr('integration.modal_profile_requires_100_mva_and_50')})
                if self.module == "modal":
                    profile = supplied.get('input', {}).get('profile', 'full_rms')
                    if profile == 'full_rms':
                        required = ('manifest.json', 'objects.csv', 'attributes.csv', 'references.csv')
                        missing = [name for name in required if not (dynamic_catalog_root(self.context.root) / name).is_file()]
                        if missing:
                            blockers.append(dict(code='modal_inputs', message=tr('int2.modal_missing', files=', '.join(missing))))
                    elif profile == 'machine_admittance' and not supplied.get('input', {}).get('object_id'):
                        blockers.append(dict(code='machine_selection', message=tr('panel.modal.select_machine')))
                    elif profile == 'ieee39_classical':
                        from power_machine.resources import readonly_path
                        from power_machine.modal.results import fingerprint
                        import json
                        from power_machine.resources import ref_data_available
                        if not ref_data_available():
                            blockers.append(dict(code='reference_data_not_included', message=tr('reference_data_not_included')))
                        else:
                            reference = json.loads(readonly_path('cases', 'ieee39_ref', 'model.json').read_text(encoding='utf-8-sig'))
                            if fingerprint(snapshot['model']) != fingerprint(reference) or any(study.get('scenario', {}).values()):
                                blockers.append(dict(code='modal_case', message=tr('int2.modal_reference_only')))
                if self.module == "contingency":
                    blockers += [b for b in self.report["calculations"]["voltage_stability"]["blockers"]
                                 if b["code"] in ("invalid_scenario", "invalid_topology", "no_reference")]
                row = {"evaluable": not blockers, "coverage": [tr('integration.profile_not_qualified_by_study_capabilities_engine')],
                       "blockers": blockers, "missing_data": [], "restrictions": [tr('integration.no_extension_of_electrical_coverage')]}
            if local_error:
                row = deepcopy(row)
                row["evaluable"] = False
                row.setdefault("blockers", []).append({"code": "preflight_input", "message": local_error})
            self.last = row
            self.restrictions_text.set(self.report.get("qualification_revision", "") + "\n" +
                "\n".join(translate_text(str(v)) for v in row.get("coverage", []) + row.get("restrictions", [])) + "\n" + translate_text(CONCLUSION_SCOPE.get(self.panel_id, "")))
            reasons = [d.get("message", str(d)) if isinstance(d, dict) else str(d) for d in row.get("blockers", [])]
            reasons += list(row.get("missing_data", []))
            self.summary.set((tr("panel.common.preflight_conditional") if row.get("evaluable") else tr("panel.common.preflight_blocked_title")) +
                             (". " + translate_text(str(reasons[0]))[:155] if reasons else ". " + tr("panel.common.see_declared_coverage")))
        except Exception as exc:
            self.last = {"evaluable": False, "blockers": [{"code": "preflight_input", "message": str(exc)}],
                         "missing_data": [], "coverage": [], "restrictions": []}
            self.summary.set(tr("panel.common.preflight_blocked_title") + ". " + translate_text(str(exc))[:155])
        self.execute_button.configure(state="normal" if self.last.get("evaluable") else "disabled")
        self.recovery_button.configure(text=tr(getattr(self.panel, 'recovery_key', 'int2.review_inputs')))
        self.recovery_button.configure(state="disabled" if self.last.get("evaluable") else "normal")
        self.details_button.configure(text=tr('panel.common.coverage_missing'))
        self.width_button.configure(text=tr('int2.widen_options'))
        return bool(self.last.get("evaluable"))

    def recover(self):
        """Lleva al dato expl?cito, sin cambiar el modelo ni seleccionar par?metros."""
        action = getattr(self.panel, 'recovery_action', None)
        if action:
            action()
        else:
            for widget in self.descendants(self.panel.options):
                if isinstance(widget, AdvancedOptions):
                    widget.open.set(True)
                    widget.toggle()
            controls = [w for w in self.descendants(self.panel.options)
                        if isinstance(w, (ttk.Entry, ttk.Combobox))]
            target = next((w for w in controls if not w.get().strip()), controls[0] if controls else None)
            if target is not None:
                self.panel.form.reveal(target)
                target.focus_set()
            else:
                app = self.panel.winfo_toplevel()
                if hasattr(app, 'open_case'):
                    app.open_case()
        self.schedule()

    def details(self):
        import json
        window = tk.Toplevel(self.panel)
        window.title(tr("panel.common.coverage_missing"))
        text = tk.Text(window, width=90, height=24, wrap="word")
        text.pack(fill="both", expand=True)
        text.insert("end", json.dumps({"module": self.module, **self.last}, ensure_ascii=False, indent=2))
        text.configure(state="disabled")


def verified_metric_inputs(root, run_directory):
    """Verifica lectura y confinamiento de la corrida, sin calcular indicadores."""
    from power_machine.dyn_metrics.analysis import read_signals
    import hashlib
    path = (Path(root) / run_directory).resolve()
    if not path.is_relative_to(Path(root).resolve()):
        raise ValueError(tr('integration.run_must_remain_within_the_project'))
    times, signals, source, events, metadata = read_signals(path)
    return {"signals_verified": bool(len(times) and signals),
            "signals_sha256": hashlib.sha256(source.read_bytes()).hexdigest()}


def verified_qds_inputs(root, model, profile):
    """El lector público QSS comprueba hashes, la GUI no resuelve flujo."""
    from power_machine.qss import read_series
    from power_machine.qds_panel.adapter import fingerprint
    base = Path(root).resolve()
    builtin = profile.get("kind") == "builtin_ieee39"
    from power_machine.resources import ref_data_root
    model_path = (ref_data_root() / "model.json") if builtin else (base / profile.get("model_path", "power_machine/cases/ieee39/model.json")).resolve()
    manifest = (base / ("power_machine/cases/ieee39/manifest.json" if builtin else profile.get("manifest_path", ""))).resolve()
    if (not builtin and not model_path.is_relative_to(base)) or not manifest.is_relative_to(base):
        raise ValueError(tr('integration.profile_must_remain_within_the_project'))
    loaded, series = read_series(model_path, manifest, profile.get("name", ""))
    return {"profile_verified": bool(series.steps) and fingerprint(loaded) == fingerprint(model)}


def sc_preflight_inputs(root, model, supplied):
    """Prepara mediante el motor la misma red y suplemento que se pretende resolver."""
    import json
    from copy import deepcopy
    result = deepcopy(supplied)
    inputs, options = result.get("input", {}), result.get("options", {})
    fault = inputs.get("fault", {})
    selected = inputs.get("selected_bus_ids") or ([fault["bus_id"]] if fault.get("bus_id") else None)
    result["selected_bus_ids"] = selected
    relative = inputs.get("supplemental_path") or ("power_machine/cases/ieee39/shortcircuit_data.json"
        if model.get("meta", {}).get("benchmark") == "ieee39" else "")
    if "supplemental" in inputs:
        result["supplemental"] = inputs["supplemental"]
    elif relative:
        path = (Path(root) / relative).resolve()
        if not path.is_relative_to(Path(root).resolve()) or not path.is_file():
            raise ValueError(tr('integration.select_an_sc_supplement_file_within_the'))
        result["supplemental"] = json.loads(path.read_text(encoding="utf-8-sig"))
    result["options"] = {**options, "fault_kind": fault.get("kind", options.get("fault_kind", "3ph"))}
    return result
