"""Adaptador del compilador existente, sin lenguaje nuevo ni código persistido."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
import re

from power_machine.dynamics.dsl_compile import DynamicCatalog, DSLCompileError
from power_machine.dynamics.dsl_expr import (ParseError, UnsupportedFunction, Call, Binary,
    Unary, Number, Name, evaluate, parse_equations)
from power_machine.dynamics.dsl_ir import BlockIR, PURE_FUNCTIONS, STATEFUL_FUNCTIONS
from power_machine.dynamics.dsl_codegen import compile_expression
from power_machine.dynamics.dsl_switching import names, walk, expression_text
from .schema import (CompilePreview, EditorError, PROFILE, validate_draft,
                     source_hash, diagnostic, fingerprint, compiler_fingerprint, MAX_STATES, MAX_PORTS, MAX_NODES)

FIELDS = frozenset(("sAddEquat", "sInput", "sOutput", "sStates", "sParams", "sLowLimPar", "sUpLimPar"))


def _validate_expression(expr):
    """Verifica firmas que el generador difiere hasta ejecución, sin añadir funciones."""
    unary = {"abs", "sqrt", "exp", "sin", "cos", "tan", "atan", "acos", "sqr", "lastvalue"}
    arities = {"time": {0}, "pow": {2, 3}, "modulo": {2}, "flipflop": {2}, "delay": {2},
        "lapprox": {2}, "invlapprox": {2}, "picontrol_const": {4}}
    arities.update({name: {1} for name in unary})
    arities.update({name: {3} for name in ("lim", "lim_const", "limstate", "limstate_const", "select", "select_const",
        "selfix", "selfix_const", "picdro", "picdro_const", "aflipflop", "gradlim_const", "movingavg")})
    for node in walk(expr):
        if isinstance(node, Call):
            name = node.name.lower()
            if name in arities and len(node.args) not in arities[name] or name in ("min", "max") and len(node.args) < 2:
                raise EditorError("invalid_argument_count", f"{name}: número de argumentos incompatible con runtime")
    compile_expression(expr)


def parse_fields(draft):
    validate_draft(draft)
    try:
        fields = json.loads(draft.source_text)
    except json.JSONDecodeError as exc:
        raise EditorError("invalid_json", exc.msg, line=exc.lineno, column=exc.colno) from exc
    if not isinstance(fields, dict) or set(fields) - FIELDS:
        raise EditorError("invalid_fields", "Objeto JSON con campos BlkDef admitidos requerido")
    for key, value in fields.items():
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise EditorError("invalid_fields", f"{key}: se requiere lista de cadenas")
    # La misma función de normalización del compilador conserva las declaraciones REF.
    from power_machine.dynamics.dsl_compile import _names
    for key in FIELDS - {"sAddEquat"}:
        fields[key] = list(_names(fields.get(key)))
    if len(fields["sStates"]) > MAX_STATES or len(fields["sInput"]) > MAX_PORTS or len(fields["sOutput"]) > MAX_PORTS:
        raise EditorError("block_budget", "Preview admite máximo 256 estados y 256 puertos por dirección", status="unsupported")
    return fields


def _check_parse_location(equations):
    """Identifica fila de sAddEquat por grupos reales. El parser no da columna."""
    from power_machine.dynamics.dsl_expr import _strip_comment
    groups = []
    for line, raw in enumerate(equations, 1):
        clean = _strip_comment(raw).strip()
        if not clean:
            continue
        if clean.startswith("&") and groups:
            groups[-1][1].append(raw)
        else:
            groups.append((line, [raw]))
    for line, group in groups:
        try:
            parse_equations(group)
        except ParseError as exc:
            raise EditorError("dsl_syntax", f"sAddEquat[{line}]: {exc}", line=line) from exc


def _validate_ir(ir, fields, parameters, *, has_nodes=False):
    declared = (*ir.inputs, *ir.outputs, *ir.states, *ir.parameters)
    for group in (ir.inputs, ir.outputs, ir.states, ir.parameters):
        if len(group) != len(set(group)) or any(not re.fullmatch(r"[A-Za-z_]\w*", n) for n in group):
            raise EditorError("invalid_declaration", "Declaración duplicada o identificador inválido")
    if set(ir.inputs) & (set(ir.states) | set(ir.parameters)) or set(ir.parameters) & set(ir.states):
        raise EditorError("invalid_declaration", "Entrada, estado y parámetro no pueden compartir identidad")
    missing = set(ir.parameters) | set(fields["sLowLimPar"]) | set(fields["sUpLimPar"])
    missing -= parameters.keys()
    if missing:
        raise EditorError("missing_parameter", f"Parámetros ausentes: {sorted(missing)}")
    if ir.unsupported_functions or ir.directives:
        raise EditorError("unsupported_function", f"Funciones/directivas no soportadas: {sorted(ir.unsupported_functions)}", status="unsupported")
    algebraic = [s.target for s in ir.algebraic]
    derivative = [s.target for s in ir.derivatives]
    if len(algebraic) != len(set(algebraic)) or len(derivative) != len(set(derivative)):
        raise EditorError("duplicate_equation", "Dos ecuaciones definen la misma señal")
    if set(derivative) - set(ir.states) or set(algebraic) & (set(ir.states) | set(ir.inputs) | set(ir.parameters)):
        raise EditorError("invalid_target", "Derivada sin estado declarado o asignación a dato de entrada")
    # En macros el cableado resuelve señales que no están en sAddEquat raíz.
    if not has_nodes:
        if set(ir.states) != set(derivative):
            raise EditorError("missing_derivative", "Cada estado requiere su derivada")
        known = set(declared) | set(algebraic) | {s.target for s in ir.initializers} | parameters.keys()
        for stmt in (*ir.algebraic, *ir.derivatives, *ir.initializers):
            unknown = names(stmt.value) - known
            if unknown:
                raise EditorError("unknown_symbol", f"{stmt.target}: símbolos desconocidos {sorted(unknown)}")
        pending = {s.target: names(s.value) & set(algebraic) for s in ir.algebraic}
        while pending:
            ready = {k for k, deps in pending.items() if not deps}
            if not ready:
                raise EditorError("algebraic_cycle", f"Lazo algebraico no soportado: {sorted(pending)}", status="unsupported")
            pending = {k: deps - ready for k, deps in pending.items() if k not in ready}
        if set(ir.outputs) - (set(algebraic) | set(ir.states) | set(ir.inputs)):
            raise EditorError("undefined_output", "Puerto de salida sin ecuación")
    for stmt in (*ir.algebraic, *ir.derivatives, *ir.initializers):
        _validate_expression(stmt.value)  # Whitelist AST del motor, sin evaluar entradas.
    for limit in ir.limits:
        for expr in (limit.lower, limit.upper):
            if expr is not None:
                _validate_expression(expr)


def _catalog(snapshot):
    catalog = object.__new__(DynamicCatalog)
    catalog.objects = {r["relative_path"]: deepcopy(r) for r in snapshot["objects"]}
    catalog.by_id = {r["object_id"]: r for r in catalog.objects.values()}
    catalog.attributes = deepcopy(snapshot["attributes"])
    catalog.children = {}
    for row in catalog.objects.values():
        catalog.children.setdefault(row["parent_id"], []).append(row)
    return catalog


def compile_artifact(draft):
    """Devuelve instancia nueva, nunca cachea memoria ni funciones de otro preview."""
    fields = parse_fields(draft)
    _check_parse_location(fields.get("sAddEquat", []))
    parameters = {k: deepcopy(v["value"]) for k, v in draft.parameters.items()}
    imported = draft.metadata.get("catalog_snapshot")
    if imported:
        if fingerprint(imported) != draft.metadata.get("catalog_sha256"):
            raise EditorError("catalog_hash_mismatch", "Catálogo derivado distinto de su huella")
        catalog = _catalog(imported)
        path = draft.metadata["type_path"]
        root = catalog.object_at(path)
        catalog.attributes[root["object_id"]].update(fields)
    else:
        path = draft.relative_path + "/type.BlkDef"
        row = {"object_id": draft.object_id + "_type", "relative_path": path, "class": "BlkDef", "parent_id": "", "name": draft.draft_id}
        catalog = _catalog({"objects": [row], "attributes": {row["object_id"]: fields}})
    ir = BlockIR.from_fields(draft.object_id, draft.relative_path, fields)
    node_classes = {"BlkRef", "BlkSum", "BlkMul", "BlkDiv", "BlkSwt", "BlkFrom", "BlkGoto"}
    _validate_ir(ir, fields, parameters, has_nodes=any(c["class"] in node_classes for c in catalog.children.get(root["object_id"], ())) if imported else False)
    from .ports import validate_ports
    validate_ports(draft, ir)
    try:
        artifact = catalog.compile_type(path, parameters, object_id=draft.object_id, relative_path=draft.relative_path)
    except DSLCompileError as exc:
        if " bajo limfix:" in str(exc) or " sobre limfix:" in str(exc):
            raise EditorError("parameter_limit", str(exc)) from exc
        raise
    _validate_graph(artifact)
    return artifact


def _validate_graph(block, *, depth=0, budget=None):
    """El compilador construye grafos. El editor comprueba extremos antes del preview."""
    from .schema import finite
    budget = [MAX_NODES] if budget is None else budget
    budget[0] -= len(block.nodes)
    if budget[0] < 0 or depth > 64:
        raise EditorError("graph_budget", "Grafo supera 512 nodos o 64 niveles", status="unsupported")
    nodes = {n.relative_path: n for n in block.nodes}
    for node in block.nodes:
        if node.ir:
            if node.ir.unsupported_functions or node.ir.directives:
                raise EditorError("unsupported_function", f"Función/directiva no soportada: {node.relative_path}", status="unsupported")
            for stmt in (*node.ir.algebraic, *node.ir.derivatives, *node.ir.initializers):
                _validate_expression(stmt.value)
        if node.subgraph:
            _validate_graph(node.subgraph, depth=depth+1, budget=budget)
    incoming = set()
    for wire in block.wires:
        finite(wire.sign, "signo de cable")
        source = nodes.get(wire.source)
        # BlkSum/BlkDiv y afines usan índices gráficos como alias de su salida.
        # _plan y evaluate del motor publican la misma señal en esos índices.
        source_count = len(block.ir.inputs) if wire.source == block.type_path else len(source.outputs) if source.ir else None
        target = nodes.get(wire.target)
        target_count = len(block.ir.outputs) if wire.target == block.type_path else len(target.inputs) if target.ir else None
        if wire.source_port < 0 or source_count is not None and wire.source_port >= source_count or wire.target_port < 0 or target_count is not None and wire.target_port >= target_count:
            raise EditorError("unknown_port", f"Índice de puerto fuera de rango: {wire.relative_path}")
        key = wire.target, wire.target_port
        if key in incoming:
            raise EditorError("port_cardinality", f"Dos cables hacia el mismo puerto: {wire.relative_path}")
        incoming.add(key)


def compile_draft(draft):
    digest = source_hash(draft)
    try:
        artifact = compile_artifact(draft)
        algebraic = tuple(s.target for s in artifact.ir.algebraic)
        return CompilePreview("completed", digest, PROFILE, artifact.state_names, algebraic, ())
    except (EditorError, DSLCompileError, ParseError, UnsupportedFunction, ValueError, TypeError, KeyError, RecursionError, AttributeError) as exc:
        status = getattr(exc, "status", "unsupported" if isinstance(exc, (DSLCompileError, UnsupportedFunction)) else "invalid_input")
        return CompilePreview(status, digest, PROFILE, (), (), (diagnostic(exc, draft),))


def scheduled_time_guards(block):
    """Calendario de comparaciones afines de time(), con parámetros en segundos.

    Conserva identidad AST para congelar cada rama durante Newton. Resuelve
    alias algebraicos y parámetros de macros, sin modificar el IR compartido.
    Condiciones temporales no afines se rechazan en vez de perder su evento.
    """
    import math
    from power_machine.dynamics.dsl_switching import COMPARISONS
    guards = []

    def inspect(ir, parameters, memory, scope_path):
        aliases = {s.target: s.value for s in ir.algebraic}

        def has_time(expr, seen=frozenset()):
            if isinstance(expr, Name) and expr.value in aliases and expr.value not in seen:
                return has_time(aliases[expr.value], seen | {expr.value})
            return any(isinstance(e, Call) and e.name.lower() == "time" or
                       isinstance(e, Name) and e.value in aliases and e.value not in seen
                       and has_time(aliases[e.value], seen | {e.value}) for e in walk(expr))

        def affine(expr, seen=frozenset()):
            if isinstance(expr, Number):
                return 0., float(expr.value)
            if isinstance(expr, Name):
                if expr.value in parameters:
                    return 0., float(parameters[expr.value])
                if expr.value in aliases and expr.value not in seen:
                    return affine(aliases[expr.value], seen | {expr.value})
                raise ValueError("Dependencia temporal no programable")
            if isinstance(expr, Call) and expr.name.lower() == "time":
                return 1., 0.
            if isinstance(expr, Unary) and expr.op in ("+", "-"):
                a, b = affine(expr.operand, seen)
                sign = -1. if expr.op == "-" else 1.
                return sign*a, sign*b
            if isinstance(expr, Binary) and expr.op in ("+", "-", "*", "/"):
                a, b = affine(expr.left, seen)
                c, d = affine(expr.right, seen)
                if expr.op == "+":
                    return a+c, b+d
                if expr.op == "-":
                    return a-c, b-d
                if expr.op == "*" and (a == 0. or c == 0.):
                    return a*d+b*c, b*d
                if expr.op == "/" and c == 0. and d != 0.:
                    return a/d, b/d
            if not has_time(expr):
                return 0., float(evaluate(expr, parameters))
            raise ValueError("Condición temporal no afín")

        seen_keys = set()
        for stmt in (*ir.algebraic, *ir.derivatives):
            for expr in walk(stmt.value):
                if not isinstance(expr, Binary) or expr.op not in COMPARISONS or not has_time(expr):
                    continue
                if id(expr) in seen_keys:
                    continue
                seen_keys.add(id(expr))
                try:
                    a, b = affine(expr.left)
                    c, d = affine(expr.right)
                    slope, intercept = a-c, b-d
                    if not math.isfinite(slope) or not math.isfinite(intercept):
                        raise ValueError("Tiempo no finito")
                    if slope == 0.:
                        continue
                    time_s = -intercept/slope
                    if not math.isfinite(time_s):
                        raise ValueError("Tiempo no finito")
                except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
                    raise EditorError("unsupported_time_guard", f"{scope_path}: {expression_text(expr)}",
                                      status="unsupported") from exc
                guards.append({"memory": memory, "key": id(expr), "op": expr.op,
                    "slope": slope, "time_s": time_s, "metadata": {
                        "object_id": block.object_id, "relative_path": block.relative_path,
                        "scope_path": scope_path, "expression": expression_text(expr),
                        "target": stmt.target, "kind": "scheduled_time"}})

    def recurse(b, parameters):
        inspect(b.ir, {**parameters, **b.initial_constants}, b.memory, b.relative_path)
        for node in b.nodes:
            if node.ir is None:
                continue
            scope = b._node_scope(node, {n: 0. for n in b.state_names}, {})
            for name in node.ir.states:
                scope.pop(name, None)
            if node.subgraph is None:
                inspect(node.ir, scope, b.memory, node.relative_path)
            else:
                recurse(b._child(node), scope)
    recurse(block, block.parameters)
    return guards


def block_view(draft):
    """IR serializable, algebraicas distintas de solver.z, grafo por identidad."""
    block = compile_artifact(draft)
    def ir_view(ir):
        return {"object_id": ir.object_id, "relative_path": ir.relative_path,
            "inputs": list(ir.inputs), "outputs": list(ir.outputs), "states": list(ir.states),
            "parameters": list(ir.parameters), "algebraic": [{"target": s.target, "expression": expression_text(s.value)} for s in ir.algebraic],
            "derivatives": [{"target": s.target, "expression": expression_text(s.value)} for s in ir.derivatives],
            "initializers": [{"target": s.target, "expression": expression_text(s.value), "initial_only": s.initial_only} for s in ir.initializers],
            "functions": sorted(ir.functions)}
    return {"object_id": draft.object_id, "relative_path": draft.relative_path, "source_sha256": source_hash(draft),
        "artifact_sha256": fingerprint(asdict(draft)), "compiler_profile": PROFILE,
        "compiler_sha256": compiler_fingerprint(),
        "ports": list(draft.ports), "parameters": draft.parameters,
        "input_units": {p["name"]: p["unit"] for p in draft.ports if p["direction"] == "input"},
        "output_units": {p["name"]: p["unit"] for p in draft.ports if p["direction"] == "output"},
        "state_units": {n: draft.metadata.get("state_units", {}).get(n) for n in block.state_names},
        "root": ir_view(block.ir), "solver_z_names": [],
        "nodes": [{"object_id": n.object_id, "relative_path": n.relative_path, "kind": n.kind,
                   "state_bindings": list(n.state_bindings), "ir": ir_view(n.ir) if n.ir else None} for n in block.nodes],
        "wires": [asdict(w) for w in block.wires], "coverage": coverage_catalog(),
        "modal_continuity": "requiere análisis de ramas y memoria por modal",
        "ref_comparison": "sin validar contra la referencia externa"}


def coverage_catalog():
    rejected = {"sapprox", "event", "output"}
    nonsmooth = {"abs", "min", "max", "lim", "lim_const", "limstate", "limstate_const", "select", "select_const", "selfix", "selfix_const", "time", "picontrol_const", "lapprox", "invlapprox"}
    return [{"name": name, "supported": name not in rejected,
             "modal_status": "unsupported" if name in rejected else "requires_branch_analysis" if name in nonsmooth or name in STATEFUL_FUNCTIONS else "requires_operating_point",
             "ref_comparison": "sin validar contra la referencia externa"} for name in sorted(PURE_FUNCTIONS | STATEFUL_FUNCTIONS)]
