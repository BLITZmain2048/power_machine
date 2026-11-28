"""Panel autónomo de contingencias, sin registro global ni cálculo en tkinter."""
from __future__ import annotations
from .panel_widgets import action_button

import copy
from dataclasses import asdict
import tkinter as tk
from tkinter import ttk

from power_machine.contingency import ContingencyOptions

from ..bus import bus
from ..results_tables import ContingencyRanking
from ..i18n import tr
from .panel_widgets import finish_panel, element_label, study_panel, ElementSelector, ResultTable, AdvancedOptions, significant, error_result

PANEL_SPEC = {"title_key": 'integration.n_1_n_2_contingencies', "id": "contingency", "title": tr('integration.n_1_n_2_contingencies'),
              "api_version": 1, "factory": "create_panel", "order": 20}
STATUS_TEXT = {"completed": tr('integration.calculated'), "partial": tr('integration.partial'),
               "limit_violation": tr('integration.limit_exceeded'), "islanded": tr('integration.islanded_operation'),
               "missing_reference": tr('integration.no_reference'), "not_converged": tr('integration.not_converged'),
               "invalid_input": tr('integration.invalid_input'), "unsupported": tr('integration.unsupported'),
               "numerical_failure": tr('integration.numerical_failure'), "cancelled": tr('integration.cancelled'),
               "generation_disconnected": tr('integration.generation_disconnected')}


def build_request(context, options: ContingencyOptions, *, selected_ids=None, filter_reason=None):
    snapshot = copy.deepcopy(context.get_snapshot())
    inputs = {}
    if selected_ids is not None:
        inputs.update(selected_ids=list(selected_ids), filter_reason=filter_reason)
    return {"schema": "power_machine-parity-request-v1", "module": "contingency",
            "root": str(context.root), "snapshot": snapshot,
            "options": asdict(options), "input": inputs}


class PanelController:
    """Contrato de interacción ensayable sin pantalla, callbacks en hilo principal."""
    def __init__(self, context, on_update=None):
        self.context = context
        self.on_update = on_update or (lambda result: None)
        self.result = None
        self.job = None

    def submit(self, options, **selection):
        request = build_request(self.context, options, **selection)
        self.job = self.context.submit_job("contingency", request, self.on_message)
        self.context.log("info", tr('integration.static_contingencies_no_ref_validation'))
        return request

    def on_message(self, message):
        if isinstance(message, dict) and message.get("schema") == "power_machine-parity-result-v1":
            self.result = message
        elif isinstance(message, (list, tuple)) and message and message[0] == "result":
            self.result = message[1]
        elif isinstance(message, (list, tuple)) and message and message[0] == "log":
            self.context.log(message[1], message[2])
            return
        elif isinstance(message, (list, tuple)) and message and message[0] == "error":
            self.context.log("error", str(message[1]))
            self.on_update(error_result(message[1]))
            return
        else:
            self.on_update(message)
            return
        self.context.publish_result(self.result)
        self.on_update(self.result)

    def cancel(self):
        # El contexto mínimo no promete un método de cancelación.
        if self.job is not None and callable(getattr(self.job, "cancel", None)):
            self.job.cancel()
        else:
            self.context.log("warning", tr('integration.cancellation_is_requested_through_the_application_job'))

    def select(self, object_id):
        self.context.select_element(object_id)
        bus.publish("selection.changed", ids=[object_id], source="contingency")



def engineering_label(ident, model):
    """Nombre y contexto, con identidad técnica conservada como dato secundario."""
    item = next((item for group in ("branches", "branches3", "gens", "buses") for item in model.get(group, []) if item["id"] == ident), None)
    if not item:
        return ident
    item = dict(item)
    if item.get("scope") == "equivalente":
        item["name"] = item.get("name", ident).replace("__", " ").replace("_", " ")
        if item.get("class") == "ElmZpu":
            item["name"] = tr('integration.equivalent_impedance') + item["name"]
    return element_label(item, model)


def engineering_row(case, model):
    """Consume LF publicado y loading.py, no estima cargabilidad desde iterados fallidos."""
    from power_machine.loading import branch_loading
    metric = case.get("metrics", {})
    voltage_rows = metric.get("voltage_changes", [])
    minimum = min((r for r in voltage_rows if r.get("u_pu") is not None), key=lambda r: r["u_pu"], default={})
    loading = branch_loading(model, case["loadflow"]) if case.get("loadflow") else {}
    loading_rows=metric.get("branches") or list(loading.values())
    maximum = max((r for r in loading_rows if r.get("loading_pct") is not None), key=lambda r: r["loading_pct"], default={})
    failed = metric.get("energized_converged") is False or case.get("status") == "not_converged"
    first_id = ((case.get("outages") or [{}])[0].get("member_ids") or [(case.get("outages") or [{}])[0].get("object_id")])[0]
    first = next((item for group in ("branches", "branches3", "gens") for item in model.get(group, []) if item["id"] == first_id), {})
    bus_id = first.get("bus", first.get("bus_from", first.get("bus_hv")))
    nominal = next((bus.get("un_kv") for bus in model.get("buses", []) if bus["id"] == bus_id), None)
    return {"case": " + ".join(engineering_label((o.get("member_ids") or [o["object_id"]])[0], model) for o in case.get("outages", [])),
            "kv": nominal, "state": STATUS_TEXT.get(case.get("status"), tr('integration.not_evaluable')),
            "voltage": minimum.get("u_pu", metric.get("min_u_pu")) if not failed else None,
            "bus": engineering_label(minimum.get("model_id", minimum.get("id")), model) if minimum and not failed else None,
            "loading": metric.get("max_loading_pct",maximum.get("loading_pct")) if not failed else None,
            "generation": metric.get("newly_disconnected_generation_p_mw"),
            "severity": metric.get("severity"),
            "priority": metric.get("alert_priority"),
            "branch": engineering_label(maximum.get("model_id",maximum.get("id")), model) if maximum and not failed else None,
            "quantity": {"current_ratio": "I/Inom", "apparent_power_ratio": "S/Sn"}.get(maximum.get("loading_metric", {}).get("quantity")) if not failed else None,
            "new": metric.get("newly_unsupplied_p_mw",metric.get("newly_deenergized_p_mw")) if not failed and not metric.get("unknown_supply_load_ids") and not metric.get("unknown_deenergized_load_ids") else None,
            "demand": metric.get("unsupplied_p_mw"),
            "failed": tr('integration.yes') if failed else tr('integration.no') if metric.get("energized_converged") is True else tr('integration.no_data')}


def severity_order(case):
    """Prioridad del motor y magnitudes publicadas, sin reclasificar islas."""
    metric=case.get('metrics',{})
    # Same descending tuple used by power_machine.api.ContingencyResult.rank().
    return (-float(metric.get('unsupplied_p_mw') or 0),
            -float(metric.get('max_loading_pct') or 0), str(case.get('case_id','')))


def execute_diagnosis(request, *, cancel=None, progress=None):
    """Adaptador del panel para el trabajador, sin registro ni cambios de motor."""
    from power_machine.contingency.diagnostics import diagnose_outage
    from power_machine.contingency.runner import materialize_scenario
    snapshot = request["snapshot"]
    model = materialize_scenario(snapshot["model"], snapshot["study"].get("scenario", {}))
    if cancel is not None and cancel.is_set():
        return {"cancelled": True}
    result = diagnose_outage(model, request["branch_id"], options=request.get("options"))
    result["snapshot_fingerprints"] = snapshot.get("fingerprints", {})
    return result


def diagnosis_rows(result, model):
    rows = []
    for phase, steps in (("Admitancia", result.get("homotopy", [])), (tr('integration.internal_load_relief'), result.get("load_reductions", []))):
        for step in steps:
            minimum = step.get("minimum_voltage") or {}
            solved = step.get("energized_status", {}).get("ok")
            rows.append({"phase": phase, "fraction": step.get("admittance_fraction"), "relief": step.get("load_reduction_fraction"),
                         "solved": tr('integration.yes') if solved else tr('integration.no'), "residual": step.get("max_mismatch_mva"),
                         "voltage": minimum.get("u_pu") if solved else None,
                         "bus": engineering_label(minimum.get("model_id"), model) if solved and minimum else None})
    return rows


def start_diagnosis(panel, context, ident, options):
    """Reserva el documento y ejecuta fuera del hilo Tk. Descarta resultados obsoletos."""
    from power_machine.gui.parity_worker import ParityWorker
    from power_machine.gui.app_jobs import document_gate
    from tkinter import filedialog
    from pathlib import Path
    import json
    if getattr(panel, "diagnostic_worker", None) and panel.diagnostic_worker.running:
        return
    if getattr(context, "external_busy", lambda: False)() or getattr(getattr(context, "worker", None), "running", False):
        panel.show_error(tr('integration.wait_for_the_active_calculation_to_finish'))
        return
    gate = document_gate(context._get_document()) if hasattr(context, "_get_document") else None
    if gate:
        try:
            gate.reserve()
        except ValueError as exc:
            panel.show_error(str(exc))
            return
    snapshot = context.get_snapshot()
    window = tk.Toplevel(panel)
    panel.diagnostic_window = window
    window.title(tr('integration.contingency_investigation_homotopy'))
    window.geometry("1000x600")
    label = tk.StringVar(window, tr('integration.computing_a_numerical_trajectory_on_a_copy'))
    ttk.Label(window, textvariable=label, wraplength=950).pack(fill="x", padx=8, pady=8)
    ttk.Label(window, wraplength=950, text=tr('integration.conditional_hypothesis_not_operational_authorization_load_relief')).pack(fill="x", padx=8)
    table = ResultTable(window, [("phase", "Trayectoria"), ("fraction", tr('integration.admittance_fraction_1')), ("relief", tr('integration.internal_relief_1')), ("solved", "Converge"), ("residual", tr('integration.mismatch_mva')), ("voltage", tr('integration.minimum_u_p_u')), ("bus", tr('integration.busbar_with_minimum_u'))], context.root)
    table.pack(fill="both", expand=True, padx=8, pady=8)
    worker = ParityWorker(panel, resolver=lambda module: execute_diagnosis, on_error=panel.show_error)
    panel.diagnostic_worker = worker
    def receive(message):
        if not panel.winfo_exists() or not window.winfo_exists():
            return
        if context.get_snapshot() != snapshot:
            label.set(tr('integration.result_discarded_case_changed_during_investigation'))
            return
        if message[0] == "error":
            label.set(tr('integration.unable_to_complete_the_investigation_check_engine'))
        elif message[0] == "result":
            result = message[1]
            panel.diagnosis_result = result
            table.set_rows(diagnosis_rows(result, snapshot["model"]))
            label.set(tr('integration.conditional_hypothesis') + result.get("conclusion", {}).get("classification", "indeterminado") + ". " + result.get("conclusion", {}).get("evidence", tr('integration.no_conclusive_evidence')))
    try:
        job = worker.submit("n1_diagnostic", {"snapshot": snapshot, "branch_id": ident, "options": options}, receive)
        if gate:
            gate.attach(job)
    except Exception:
        if gate:
            gate.reserved = False
        worker.shutdown()
        window.destroy()
        raise
    panel.bind("<Destroy>", lambda event: worker.shutdown() if event.widget is panel else None, add="+")
    def save():
        result = getattr(panel, "diagnosis_result", None)
        if not result:
            return
        path = filedialog.asksaveasfilename(parent=window, initialdir=context.root, defaultextension=".json")
        if path:
            destination = Path(path).resolve()
            if not destination.is_relative_to(Path(context.root).resolve()):
                label.set(tr('integration.diagnostic_must_remain_within_the_project'))
                return
            with destination.open("x", encoding="utf-8") as stream:
                json.dump(result, stream, ensure_ascii=False, indent=2)
    ttk.Button(window, text=tr('integration.save_diagnostic_and_provenance_json'), command=save).pack(anchor="w", padx=8, pady=4)


def create_panel(parent, context) -> ttk.Frame:
    shell, frame, results = study_panel(parent)
    if parent is None:
        # Contexto estructural sin una superficie Tk conectada.
        shell.controller = PanelController(context)
        return shell
    include_n2 = tk.BooleanVar(master=frame, value=False)
    q_limits = tk.BooleanVar(master=frame, value=True)
    cap = tk.StringVar(master=frame, value="10000")
    policies=('largest_generator','none')
    policy=tk.StringVar(frame,value=tr('integration.largest_generator_per_island'))
    ttk.Label(frame,text=tr('integration.angular_reference_per_island')).grid(row=0,column=0,sticky='w')
    policy_box=ttk.Combobox(frame,textvariable=policy,state='readonly')
    policy_box.grid(row=0,column=1,sticky='ew')
    policy_box.configure(values=(tr('integration.largest_generator_per_island'),tr('integration.only_declared_references')))
    policy_box.current(0)
    selected_ids = tk.StringVar(master=frame, value="")
    reason = tk.StringVar(master=frame, value="")
    summary = tk.StringVar(master=frame, value=tr('integration.n_1_by_default_screening_thresholds_no'))
    advanced = AdvancedOptions(frame)
    advanced.grid(row=9, column=0, columnspan=2, sticky="ew")
    ttk.Checkbutton(advanced.content, text=tr('integration.include_simultaneous_n_2'), variable=include_n2).grid(row=0, column=0, sticky="w")
    ttk.Checkbutton(advanced.content, text=tr('integration.consider_q_limits'), variable=q_limits).grid(row=0, column=1, sticky="w")
    ttk.Label(advanced.content, text=tr('integration.maximum_case_budget')).grid(row=1, column=0, sticky="w")
    ttk.Entry(advanced.content, textvariable=cap).grid(row=1, column=1, sticky="ew")
    ttk.Label(frame, text=tr('integration.optional_element_selection')).grid(row=2, column=0, sticky="w")
    ttk.Entry(frame, textvariable=selected_ids).grid(row=2, column=1, sticky="ew")
    ttk.Label(frame, text=tr('integration.filter_reason')).grid(row=3, column=0, sticky="w")
    ttk.Entry(frame, textvariable=reason).grid(row=3, column=1, sticky="ew")
    model = context.get_snapshot()["model"]
    pick = tk.StringVar(frame, "")
    selector = ElementSelector(frame, model.get("branches", []) + model.get("branches3", []) + model.get("gens", []), model, pick)
    selector.grid(row=7, column=0, columnspan=2, sticky="ew")
    def add_selection():
        ids = [v.strip() for v in selected_ids.get().split(",") if v.strip()]
        if pick.get() and pick.get() not in ids:
            selected_ids.set(",".join(ids + [pick.get()]))
    ttk.Button(frame, text=tr('integration.add_element_to_filter'), command=add_selection).grid(row=8, column=0, sticky="w")
    violations = tk.BooleanVar(frame, False)
    cached = [None]
    ttk.Checkbutton(frame, text=tr('integration.violations_only'), variable=violations, command=lambda: update(cached[0]) if cached[0] else None).grid(row=8, column=1, sticky="w")
    ranking = ContingencyRanking(results, [], on_select=lambda case: selected_case.__setitem__(0, case), detail_parent=shell.plot_area)
    ranking.grid(row=0, column=0, sticky='nsew')
    results.rowconfigure(0, weight=1)
    results.columnconfigure(0, weight=1)
    selected_case = [None]
    def update(message):
        if isinstance(message, dict) and message.get("status") == "error":
            shell.show_error(message["diagnostics"][0]["message"])
            summary.set(message["diagnostics"][0]["message"])
            return
        if isinstance(message, (tuple, list)) and message and message[0] == "progress":
            progress = message[1]
            summary.set(tr('integration.cases', value0=progress['completed'], value1=progress['total']))
        elif isinstance(message, dict) and message.get("schema") == "power_machine-parity-result-v1":
            if message is not cached[0]:
                shell.show_state(message)
            cached[0] = message
            payload = message['payload']
            cases = payload.get('cases', [])
            if violations.get():
                cases = [case for case in cases if case.get('status') != 'completed']
            ranking.set_cases(cases)
            status_key = {'partial': 'results.state_partial', 'completed': 'results.state_solved'}.get(message['status'])
            summary.set(tr('results.panel_summary', status=tr(status_key) if status_key else message['status'],
                           completed=payload.get('completed_count', 0), total=payload.get('total_expected', 0)))
    controller = PanelController(context, update)
    def submit():
        try:
            options = ContingencyOptions(orders=(1, 2) if include_n2.get() else (1,),
                                         max_cases=int(cap.get()), q_limits=q_limits.get(),island_reference=policies[max(0,policy_box.current())])
            ids = [value.strip() for value in selected_ids.get().split(",") if value.strip()]
            if ids and not reason.get().strip():
                raise ValueError(tr('integration.specify_the_filter_reason'))
            controller.submit(options, selected_ids=ids or None, filter_reason=reason.get().strip() or None)
        except (ValueError, TypeError) as exc:
            summary.set(str(exc))
            context.log("error", str(exc))
    def diagnose_selected():
        if shell.freshness == "obsoleto":
            summary.set(tr('integration.run_n_1_again_before_investigating_a'))
            return
        case = selected_case[0]
        if not case or case.get("status") != "not_converged" or len(case.get("outages", [])) != 1:
            summary.set(tr('integration.select_a_single_nonconvergent_contingency_to_investigate'))
            return
        if len(case["outages"][0].get("member_ids") or []) > 1:
            summary.set(tr('integration.the_diagnostic_accepts_an_individual_line_not'))
            return
        ident = case['outages'][0]['object_id']
        element = next((b for b in model.get("branches", []) if b["id"] == ident), {})
        if element.get("class") != "ElmLne":
            summary.set(tr('integration.homotopy_accepts_one_line_not_groups_or'))
            return
        if not shell.refresh_capabilities():
            summary.set(tr('integration.diagnostic_blocked_by_current_preflight'))
            return
        start_diagnosis(shell, context, ident, {"q_limits": q_limits.get()})
    ttk.Button(shell.actions, text=tr('integration.investigate_non_convergence_homotopy'), command=diagnose_selected).grid(row=1, column=0, columnspan=3, sticky="ew")
    shell.diagnose_selected = diagnose_selected
    shell.ranking = ranking
    shell.result_table = ranking.ranked_table
    action_button(frame, 'export', text=tr('results.export_csv'), command=ranking.ranked_table.export).grid(row=10, column=0)
    action_button(frame, 'run', text=tr('integration.run_contingencies'), command=submit).grid(row=4, column=0, sticky="w")
    action_button(frame, 'cancel', text=tr('integration.cancel'), command=controller.cancel).grid(row=4, column=1, sticky="w")
    ttk.Label(frame, textvariable=summary, wraplength=650).grid(row=5, column=0, columnspan=2, sticky="w")
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(6, weight=1)
    shell.controller = controller
    def configure_case(config):
        if 'q_limits' in config:q_limits.set(config['q_limits'])
        if 'island_reference' in config:
            policy_box.current(policies.index(config['island_reference']))
    shell.configure_case=configure_case
    shell.reference_selector=policy_box
    def translate_policy():
        selected=policy_box.current()
        policy_box.configure(values=(tr('integration.largest_generator_per_island'),tr('integration.only_declared_references')))
        policy_box.current(max(0,selected))
        if cached[0]:
            update(cached[0])
    from .panel_widgets import panel_retranslate
    panel_retranslate(shell,translate_policy)
    shell.preflight_inputs = lambda: {"options": {"orders": [1, 2] if include_n2.get() else [1], "q_limits": q_limits.get(),"island_reference":policies[max(0,policy_box.current())]}, "input": {"selected_ids": [v.strip() for v in selected_ids.get().split(",") if v.strip()]}}
    return finish_panel(shell, context, controller, tr('results.rank_criterion'))
