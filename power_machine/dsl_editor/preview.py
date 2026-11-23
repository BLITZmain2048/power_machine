"""Ensayos aislados cancelables sobre CompiledDSL y DAESolver existentes."""
from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
import multiprocessing as mp
import queue
import time

import numpy as np

from power_machine.dynamics.solver import DAEState, SolverConvergenceError
from power_machine.dynamics.dsl_compile import CompiledDSL
from power_machine.dynamics.dsl_expr import evaluate, UnsupportedFunction
from .schema import EditorError, PROFILE, finite, fingerprint, source_hash, diagnostic, encode, compiler_fingerprint
from .compiler_adapter import compile_artifact, scheduled_time_guards
from .events import PreviewDAESolver, same_time, time_tolerance
from .results import envelope

MAX_SAMPLES = 20000
MAX_STEPS = 100000


class _InterpretedDSL(CompiledDSL):
    """Misma propagación/limitación, expresiones por intérprete AST de referencia."""
    def _eval(self, expr, scope, t_s=None):
        return evaluate(expr, scope, time_s=t_s, runtime=self.memory)


def _use_interpreter(block):
    block.__class__ = _InterpretedDSL
    block.fast_graph = False
    for node in block.nodes:
        if node.subgraph is not None:
            _use_interpreter(node.subgraph)


class _IsolatedNetwork:
    """Nodo numérico pasivo Y=1, sin red eléctrica ni conversión de señales."""
    node_ids = ("isolated_preview",)
    ybus = np.eye(1, dtype=complex)
    model = {}
    def index_of_bus(self, bus_id):
        if bus_id != self.node_ids[0]:
            raise ValueError("Nodo aislado desconocido")
        return 0


class _PreviewDevice:
    n_alg = 0
    bus_id = "isolated_preview"
    def __init__(self, block, inputs):
        self.object_id, self.relative_path = block.object_id, block.relative_path
        self.n_states = block.n_states
        self.block, self.inputs = block, dict(inputs)
        self.blocks = {block.relative_path: block}
        self.slices = {block.relative_path: slice(0, block.n_states)}
    def f(self, t, x, z, v):
        return self.block.evaluate(t, x, self.inputs)[1]
    def current_injection(self, x, v):
        return 0j
    def g(self, t, x, z, v):
        return np.empty(0)


def _validate_case(case, block, options):
    if not case.time_s or len(case.time_s) > MAX_SAMPLES:
        raise EditorError("invalid_calendar", "Calendario vacío o supera 20000 muestras")
    times = [finite(t, "time_s [s]") for t in case.time_s]
    if times[0] < 0 or any(b <= a for a, b in zip(times, times[1:])):
        raise EditorError("invalid_calendar", "Tiempos no negativos y estrictamente crecientes requeridos")
    if set(case.inputs) != set(block.ir.inputs):
        raise EditorError("missing_input", "Entradas del caso deben coincidir con los puertos declarados")
    for name, values in case.inputs.items():
        if len(values) != len(times):
            raise EditorError("invalid_input_series", f"{name}: longitud diferente del calendario")
        for value in values:
            finite(value, name)
    def functions(graph):
        result = set(graph.ir.functions)
        for node in graph.nodes:
            if node.ir:
                result.update(node.ir.functions)
            if node.subgraph:
                result.update(functions(node.subgraph))
        return result
    if functions(block) & {"delay", "movingavg"} and any(any(b != a for a, b in zip(values, values[1:])) for values in case.inputs.values()):
        raise EditorError("discontinuous_memory_history",
            "delay/movingavg conservan interpolación histórica del runtime, falta contrato de ambos lados de discontinuidades", status="unsupported")
    for name, value in case.initial.items():
        finite(value, name)
    step_s = finite(options.get("step_s", .001), "step_s [s]")
    if step_s <= 0:
        raise EditorError("invalid_step", "Paso debe ser positivo")
    if (times[-1] - times[0]) / step_s > MAX_STEPS:
        raise EditorError("step_budget", "Preview supera presupuesto de 100000 pasos")
    return step_s


def _base_payload(draft):
    return {"object_id": draft.object_id, "relative_path": draft.relative_path,
        "source_sha256": source_hash(draft), "artifact_sha256": fingerprint(asdict(draft)),
        "compiler_profile": PROFILE, "time_s": [], "outputs": {}, "states": {},
        "compiler_sha256": compiler_fingerprint(),
        "input_units": {p["name"]: p["unit"] for p in draft.ports if p["direction"] == "input"},
        "output_units": {p["name"]: p["unit"] for p in draft.ports if p["direction"] == "output"},
        "state_units": draft.metadata.get("state_units", {}), "input_semantics": "ZOH derecha, sin interpolación de discontinuidades",
        "initialization_completed": False, "internal_events": [], "input_events": [], "time_events": [],
        "discontinuity_semantics": "muestra ordinaria derecha, eventos con before/after y estado continuo común",
        "analytical_checks": [], "pf_comparison": "sin validar contra PF"}


def _simulate_local(draft, case, options, *, checkpoint=None, interpreted=False):
    payload = _base_payload(draft)
    try:
        block = compile_artifact(draft)
        if interpreted:
            _use_interpreter(block)
        step_s = _validate_case(case, block, options)
        first_inputs = {k: v[0] for k, v in case.inputs.items()}
        # Condiciones conocidas nunca se rellenan con ceros implícitos.
        known = {**first_inputs, **case.initial}
        x, _ = block.initialize(known)
        block.accept_step(case.time_s[0], x, first_inputs)
        payload["initialization_completed"] = True
        payload["inputs"] = {k: list(v) for k, v in case.inputs.items()}
        payload["outputs"] = {k: [] for k in block.ir.outputs}
        payload["states"] = {k: [] for k in block.state_names}
        payload["state_units"] = {k: payload["state_units"].get(k) for k in block.state_names}
        device = _PreviewDevice(block, first_inputs)
        solver = PreviewDAESolver(_IsolatedNetwork(), [device], step_s=step_s, atol=1e-12, rtol=1e-10,
                                 restart_euler=True, time_guards=scheduled_time_guards(block))
        state = DAEState(float(case.time_s[0]), x, np.empty(0), np.zeros(1, dtype=complex))
        steps = 0
        methods = set()

        def time_event(t_s):
            before = solver.outputs(t_s, state.x, device.inputs, side=-1)
            after = solver.outputs(t_s, state.x, device.inputs, side=1)
            states = dict(zip(block.state_names, map(float, state.x)))
            payload["time_events"].append({"time_s": t_s, "guards": solver.guards_at(t_s),
                "before_outputs": before, "after_outputs": after,
                "before_states": states.copy(), "after_states": states.copy()})
            solver.restart_discrete()

        if solver.guards_at(state.t):
            time_event(state.t)
            solver.accept(state, device.inputs)
        for i, target in enumerate(case.time_s):
            while state.t < target - time_tolerance(target):
                memory_event = block.next_event_time(state.t)
                scheduled_event = solver.next_time_event(state.t)
                stop = min(t for t in (target, memory_event, scheduled_event) if t is not None)
                h = min(step_s, stop-state.t)
                if h <= 0:
                    raise EditorError("invalid_memory_event", "Evento de memoria no posterior al paso")
                state, metrics = solver.step(state, h)
                steps += 1
                methods.add(metrics.method)
                if scheduled_event is not None and same_time(state.t, scheduled_event):
                    state.t = scheduled_event
                    time_event(scheduled_event)
                solver.accept(state, device.inputs)
                if memory_event is not None and same_time(state.t, memory_event):
                    solver.restart_discrete()
                if steps > MAX_STEPS:
                    raise EditorError("step_budget", "Presupuesto de pasos agotado", status="partial")
            # Se evalúa el lado izquierdo antes de cambiar la entrada exactamente en target.
            right = {k: v[i] for k, v in case.inputs.items()}
            if device.inputs != right:
                before = solver.outputs(target, state.x, device.inputs)
                previous = device.inputs
                device.inputs = right
                solver.accept(state, right)
                after = solver.outputs(target, state.x, right)
                payload["input_events"].append({"time_s": target, "before_inputs": previous, "after_inputs": right,
                    "before_outputs": before, "after_outputs": after,
                    "before_states": dict(zip(block.state_names, map(float, state.x))),
                    "after_states": dict(zip(block.state_names, map(float, state.x)))})
                solver.restart_discrete()
            outputs = solver.outputs(target, state.x, device.inputs)
            for value in outputs.values():
                finite(float(value), "salida DSL")
            payload["time_s"].append(float(target))
            for name, value in outputs.items():
                payload["outputs"][name].append(float(value))
            for name, value in zip(block.state_names, state.x):
                payload["states"][name].append(float(value))
            payload["steps_completed"] = steps
            if checkpoint:
                checkpoint({"kind": "sample", "index": i, "time_s": float(target), "outputs": outputs,
                            "states": dict(zip(block.state_names, map(float, state.x))), "initialization_completed": True})
        payload.update(internal_events=solver.internal_events, integration_methods=sorted(methods), nominal_step_s=step_s,
                       evaluator="interpreter_AST" if interpreted else "compiled_AST")
        if case.expected is not None:
            checks = []
            for name, spec in case.expected.items():
                if name not in payload["outputs"] or not isinstance(spec, dict) or len(spec["values"]) != len(case.time_s):
                    raise EditorError("invalid_expected", "Referencia analítica requiere salida y valores en el calendario")
                values = [finite(v, "expected") for v in spec["values"]]
                tolerance = finite(spec["atol"], "expected.atol")
                if tolerance < 0:
                    raise EditorError("invalid_expected", "Tolerancia negativa")
                error = max(abs(a-b) for a, b in zip(payload["outputs"][name], values))
                checks.append({"signal": name, "reference": "analítica aportada, sin validar contra PF", "max_abs_error": error,
                               "atol": tolerance, "passed": error <= tolerance})
            payload["analytical_checks"] = checks
        return envelope("completed", payload, options=options, inputs=draft.metadata.get("source_inputs", []))
    except (ValueError, KeyError, TypeError, ArithmeticError, RecursionError, SolverConvergenceError) as exc:
        status = getattr(exc, "status", "not_converged" if isinstance(exc, SolverConvergenceError) else "unsupported" if isinstance(exc, UnsupportedFunction) else "numerical_failure" if isinstance(exc, ArithmeticError) else "invalid_input")
        return envelope(status, payload, options=options, diagnostics=[diagnostic(exc, draft)])


def _process_entry(mailbox, draft, case, options, interpreted):
    try:
        result = _simulate_local(draft, case, options, checkpoint=lambda p: mailbox.put(("progress", p)), interpreted=interpreted)
        mailbox.put(("result", result))
    except Exception as exc:
        mailbox.put(("error", str(exc)))


def _run_process(draft, case, options, *, cancel=None, progress=None, interpreted=False):
    timeout_s = finite(options.get("timeout_s", 30.), "timeout_s [s]")
    if timeout_s <= 0:
        raise EditorError("invalid_timeout", "Timeout debe ser positivo")
    # Limita el tamaño antes de enviar el draft al proceso spawn.
    from .schema import validate_draft
    validate_draft(draft)
    if len(case.time_s) > MAX_SAMPLES or len(encode(asdict(case)).encode("utf-8")) > 8*1024*1024:
        raise EditorError("case_too_large", "Caso máximo 20000 muestras y 8 MiB")
    payload = _base_payload(draft)
    if cancel is not None and cancel.is_set():
        return envelope("cancelled", payload, options=options, diagnostics=[diagnostic(EditorError("preview_cancelled", "Preview cancelada antes de iniciar"), draft)])
    ctx = mp.get_context("spawn")
    mailbox = ctx.Queue()
    process = ctx.Process(target=_process_entry, args=(mailbox, draft, case, options, interpreted))
    start = time.monotonic()
    status = "partial"
    code = "preview_timeout"
    try:
        process.start()
        while True:
            if cancel is not None and cancel.is_set():
                status, code = "cancelled", "preview_cancelled"
                break
            if time.monotonic() - start >= timeout_s:
                break
            try:
                kind, data = mailbox.get(timeout=min(.05, max(.001, timeout_s-(time.monotonic()-start))))
            except queue.Empty:
                if not process.is_alive():
                    code, status = "preview_process_failed", "numerical_failure"
                    break
                continue
            if kind == "result":
                return data
            if kind == "error":
                return envelope("numerical_failure", payload, options=options, diagnostics=[diagnostic(EditorError("preview_process_failed", data), draft)])
            if kind == "progress":
                payload["time_s"].append(data["time_s"])
                payload["initialization_completed"] = data["initialization_completed"]
                for group in ("outputs", "states"):
                    for name, value in data[group].items():
                        payload[group].setdefault(name, []).append(value)
                if progress:
                    progress(deepcopy(data))
        payload["last_completed_time_s"] = payload["time_s"][-1] if payload["time_s"] else None
        return envelope(status, payload, options=options,
            diagnostics=[diagnostic(EditorError(code, "Proceso de preview detenido, muestras aceptadas conservadas"), draft)])
    finally:
        if process.is_alive():
            process.terminate()
        if process.pid is not None:
            process.join(timeout=2.)
            process.close()
        mailbox.close()


def simulate_preview(draft, case, *, options=None, cancel=None, progress=None):
    """Proceso propio cancelable. Paso [s] y timeout real [s], 30 s por defecto."""
    options = deepcopy(options or {})
    try:
        if not isinstance(options, dict):
            raise ValueError("Opciones deben ser un objeto JSON")
        encode(options)
    except (ValueError, TypeError, RecursionError) as exc:
        return envelope("invalid_input", _base_payload(draft), options={}, diagnostics=[diagnostic(EditorError("invalid_options", str(exc)), draft)])
    try:
        return _run_process(draft, case, options, cancel=cancel, progress=progress)
    except (ValueError, TypeError, KeyError, OSError, AssertionError) as exc:
        return envelope(getattr(exc, "status", "invalid_input"), _base_payload(draft), options=options, diagnostics=[diagnostic(exc, draft)])


def compare_interpreter(draft, case, *, options=None, cancel=None, progress=None):
    """Compara trayectorias compiladas e interpretadas del mismo runtime local."""
    options = deepcopy(options or {})
    compiled = simulate_preview(draft, case, options=options, cancel=cancel, progress=progress)
    if compiled["status"] != "completed":
        return compiled
    try:
        reference = _run_process(draft, case, options, cancel=cancel, interpreted=True)
    except (ValueError, TypeError, OSError, AssertionError) as exc:
        return envelope("invalid_input", _base_payload(draft), options=options, diagnostics=[diagnostic(exc, draft)])
    if reference["status"] != "completed":
        return reference
    errors = []
    same_calendar = compiled["payload"]["time_s"] == reference["payload"]["time_s"]
    for group in ("outputs", "states"):
        for name, values in compiled["payload"][group].items():
            other = reference["payload"][group][name]
            error = max(abs(a-b) / max(1., abs(b)) for a, b in zip(values, other))
            errors.append({"kind": group, "signal": name, "max_relative_error": error, "passed": len(values) == len(other) and error <= 1e-12})
    compiled["payload"]["interpreter_comparison"] = {"reference": "intérprete AST local actual, sin validar contra PF",
        "errors": errors, "passed": same_calendar and all(e["passed"] for e in errors), "same_calendar": same_calendar,
        "normalization": "max(1,abs(reference))", "tolerance": 1e-12,
        "reference_run_id": reference["run_id"]}
    return compiled
