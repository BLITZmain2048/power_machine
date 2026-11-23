'Dsl compile support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Mapping
import math
import re

import numpy as np

from .dsl_expr import Binary, Call, DSLMemory, Name, Unary, evaluate, parse_expression
from .dsl_codegen import compile_expression
from .dsl_ir import BlockIR
from .interfaces import finite_difference_jacobian
from .schema import read_dynamic_export


class DSLCompileError(ValueError):
    'The export cannot execute the requested block with the supported semantics.'


def _limstate_calls(expr):
    """Encuentra limstate aunque estÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â© dentro de lim o de una suma algebraica."""
    if isinstance(expr, Call):
        if expr.name.lower() in ("limstate", "limstate_const"):
            yield expr
        for arg in expr.args:
            yield from _limstate_calls(arg)
    elif isinstance(expr, Binary):
        yield from _limstate_calls(expr.left)
        yield from _limstate_calls(expr.right)
    elif isinstance(expr, Unary):
        yield from _limstate_calls(expr.operand)


def _expression_names(expr):
    if isinstance(expr, Name):
        yield expr.value
    elif isinstance(expr, Call):
        for arg in expr.args:
            yield from _expression_names(arg)
    elif isinstance(expr, Binary):
        yield from _expression_names(expr.left)
        yield from _expression_names(expr.right)
    elif isinstance(expr, Unary):
        yield from _expression_names(expr.operand)


def _names(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        value = [value]
    return tuple(part.strip() for item in value for part in
                 str(item).replace(";", ",").split(",") if part.strip())


def _path(value: object) -> str | None:
    return value.get("relative_path") if isinstance(value, dict) else None


def _block_ir(row: Mapping[str, str], fields: Mapping[str, object]) -> BlockIR:
    normalized = dict(fields)
    for key in ("sInput", "sOutput", "sStates", "sParams"):
        normalized[key] = _names(fields.get(key))
    return BlockIR.from_fields(row["object_id"], row["relative_path"], normalized)


@dataclass(frozen=True)
class Wire:
    object_id: str
    relative_path: str
    source: str
    source_port: int
    target: str
    target_port: int
    sign: float


@dataclass(frozen=True)
class Node:
    object_id: str
    relative_path: str
    kind: str
    ir: BlockIR | None
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    state_bindings: tuple[str, ...]
    parameter_bindings: tuple[str, ...]
    subgraph: CompiledDSL | None = None
    signal: str | None = None
    limit_bindings: tuple[tuple[str, str], ...] = ()
    switch_invert: bool = False


class DynamicCatalog:
    'Read-only index of a pf-dynamic-v1 export.'

    def __init__(self, directory: Path):
        data = read_dynamic_export(Path(directory))
        self.objects = {row["relative_path"]: row for row in data["objects"]}
        self.by_id = {row["object_id"]: row for row in data["objects"]}
        self.attributes: dict[str, dict[str, object]] = {}
        import json
        for row in data["attributes"]:
            if row["status"] == "PRESENT":
                self.attributes.setdefault(row["object_id"], {})[row["attribute"]] = json.loads(row["value_json"])
        self.children: dict[str, list[dict[str, str]]] = {}
        for row in data["objects"]:
            self.children.setdefault(row["parent_id"], []).append(row)

    def attrs(self, row: Mapping[str, str]) -> dict[str, object]:
        return self.attributes.get(row["object_id"], {})

    def object_at(self, path: str) -> dict[str, str]:
        try:
            return self.objects[path]
        except KeyError as exc:
            raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Missing PF reference: {path}') from exc

    def parameter_map(self, dsl: Mapping[str, str]) -> dict[str, float]:
        'Map positional parnam/params values. Never convert null into zero.'
        a = self.attrs(dsl)
        names, values = _names(a.get("parnam")), a.get("params")
        if not isinstance(values, list) or len(names) != len(values):
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Inconsistent parnam/params: {dsl['relative_path']}")
        result = {}
        for name, value in zip(names, values):
            if value is not None:
                result[name] = float(value)
        return result

    def curve_map(self, dsl: Mapping[str, str], typ: Mapping[str, str]) -> dict[str, tuple[tuple[float, float], ...]]:
        'Read paired PF curve columns in alphabetical curve-name order. This is distinct from sParams order. Require complete, finite, strictly ordered curve data.'
        declared = _names(self.attrs(typ).get("sParams"))
        unsupported = [n for n in declared if n.startswith(('matrix_', 'omatrix_'))]
        if unsupported:
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"No decoder for characteristics {unsupported}: {dsl['relative_path']}")
        names = sorted(name for name in declared if name.startswith(("array_", "oarray_")))
        if len(names) != len(set(names)):
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Duplicate curves: {dsl['relative_path']}")
        if not names:
            return {}
        matrix = self.attrs(dsl).get("matrix")
        if matrix is None or matrix == []:
            if all(n.startswith('oarray_') for n in names):
                return {}
        if not isinstance(matrix, list) or not matrix or not isinstance(matrix[0], list):
            raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f"Missing matrix curves: {dsl['relative_path']}")
        header = matrix[0]
        if len(header) < 2*len(names):
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Incomplete curve columns: {dsl['relative_path']}")
        curves = {}
        for index, name in enumerate(names):
            try:
                size = float(header[2*index])
                if name.startswith('oarray_') and size == 0:
                    continue
                if not math.isfinite(size) or not size.is_integer() or size < 2 or size >= len(matrix):
                    raise _coded_error(ValueError, 'invalid_dsl_compile_data', 'Invalid point count')
                count = int(size)
                points = tuple((float(row[2*index]), float(row[2*index+1]))
                               for row in matrix[1:count+1])
                if not all(math.isfinite(x) and math.isfinite(y) for x, y in points):
                    raise _coded_error(ValueError, 'invalid_dsl_compile_data', 'Point must be finite')
                if any(b[0] <= a[0] and b != a for a, b in zip(points, points[1:])):
                    raise _coded_error(ValueError, 'invalid_dsl_compile_data', 'Abscissae must be strictly increasing')
            except (TypeError, ValueError, IndexError) as exc:
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Invalid curve {name}: {dsl['relative_path']}: {exc}") from exc
            curves[name] = points
        return curves

    def compile_frame(self, composite_path: str, *, adapters=None):
        """Execute an ElmComp frame using explicit native/network adapters."""
        from .frames import compile_frame
        return compile_frame(self, composite_path, adapters=adapters)

    def compile_dsl(self, dsl_path: str) -> "CompiledDSL":
        dsl = self.object_at(dsl_path)
        if dsl["class"] != "ElmDsl":
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Not an ElmDsl: {dsl_path}')
        typ_path = _path(self.attrs(dsl).get("typ_id"))
        if not typ_path:
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'ElmDsl has no typ_id: {dsl_path}')
        typ = self.object_at(typ_path)
        parameters = {**self.parameter_map(dsl), **self.curve_map(dsl, typ)}
        fields = self.attrs(typ)
        formal_params = _names(fields.get("sParams"))
        actual_names = _names(self.attrs(dsl).get("parnam"))
        formal_limits = (*_names(fields.get("sLowLimPar")),
                         *_names(fields.get("sUpLimPar")))
        if (formal_limits and tuple(actual_names[:len(formal_params)]) == formal_params
                and len(actual_names) == len(formal_params) + len(formal_limits)):
            for formal, actual in zip(formal_limits, actual_names[len(formal_params):]):
                parameters[formal] = parameters[actual]
        return self.compile_type(typ_path, parameters,
                                 object_id=dsl["object_id"], relative_path=dsl_path)

    def compile_type(self, type_path: str, parameters: Mapping[str, object], *,
                     object_id: str | None = None,
                     relative_path: str | None = None,
                     _stack: tuple[str, ...] = ()) -> "CompiledDSL":
        if type_path in _stack:
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Recursive BlkDef: {' -> '.join((*_stack, type_path))}")
        root = self.object_at(type_path)
        if root["class"] != "BlkDef":
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Not a BlkDef: {type_path}')
        a = self.attrs(root)
        ir = _block_ir(root, a)
        if ir.unsupported_functions:
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Function cannot be executed {sorted(ir.unsupported_functions)}: {type_path}')
        self._check_limits(ir, parameters)
        children = self.children.get(root["object_id"], ())
        nodes: list[Node] = []
        for child in children:
            kind = child["class"]
            if kind not in ("BlkRef", "BlkSum", "BlkSig", "ElmDsl",
                            "BlkMul", "BlkDiv", "BlkSwt", "BlkFrom", "BlkGoto"):
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Node {kind} has no defined semantics: {child['relative_path']}")
            if kind in ("BlkSig", "ElmDsl"):
                continue
            ca = self.attrs(child)
            if kind in ("BlkSum", "BlkMul", "BlkDiv", "BlkSwt", "BlkFrom", "BlkGoto"):
                signal = _names(ca.get("sSig"))
                if kind in ("BlkFrom", "BlkGoto") and len(signal) > 1:
                    raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Ambiguous labels: {child['relative_path']}")
                nodes.append(Node(child["object_id"], child["relative_path"], kind,
                                  None, (), ("out",), (), (), None,
                                  signal[0] if signal else ca.get("loc_name") if kind == "BlkGoto" else None,
                                  (), bool(ca.get("iNeg", 0))))
                continue
            ref_path = _path(ca.get("typ_id"))
            if not ref_path:
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"BlkRef has no type: {child['relative_path']}")
            ref = self.object_at(ref_path)
            ra = self.attrs(ref)
            descendants = self.children.get(ref["object_id"], ())
            ref_ir = _block_ir(ref, ra)
            if ref_ir.unsupported_functions:
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Function cannot be executed {sorted(ref_ir.unsupported_functions)}: {ref_path}')
            subgraph = (self.compile_type(ref_path, {}, _stack=(*_stack, type_path))
                        if any(c["class"] in ("BlkRef", "BlkSum", "BlkMul", "BlkDiv", "BlkSwt",
                                               "BlkFrom", "BlkGoto") for c in descendants)
                        else None)
            state_bindings = _names(ca.get("sStates"))
            if len(state_bindings) != len(_names(ra.get("sStates"))):
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"States are not aligned: {child['relative_path']}")
            param_bindings = _names(ca.get("sParams"))
            formal_params = _names(ra.get("sParams"))
            if len(param_bindings) < len(formal_params):
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Parameters are not aligned: {child['relative_path']}")
            extra_params = param_bindings[len(formal_params):]
            param_bindings = param_bindings[:len(formal_params)]
            limit_bindings = []
            for key in ("sLowLimPar", "sUpLimPar"):
                formal, actual = _names(ra.get(key)), _names(ca.get(key))
                # PF puede ubicar un lÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­mite posicional en BlkRef.sParams.
                if not actual and formal and len(extra_params) >= len(formal):
                    actual = extra_params[:len(formal)]
                    extra_params = extra_params[len(formal):]
                if len(formal) != len(actual):
                    raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Limits {key} are not aligned: {child['relative_path']}")
                limit_bindings.extend(zip(formal, actual))
            if extra_params:
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Additional parameters have no port: {child['relative_path']}")
            nodes.append(Node(child["object_id"], child["relative_path"], kind,
                              ref_ir, (*_names(ra.get("sInput")),
                                       *_names(ra.get("sLowLimInp")),
                                       *_names(ra.get("sUpLimInp"))),
                              _names(ra.get("sOutput")),
                              state_bindings, param_bindings, subgraph, None,
                              tuple(limit_bindings)))
        wires: list[Wire] = []
        nodes_by_path = {node.relative_path: node for node in nodes}
        for child in children:
            if child["class"] != "BlkSig":
                continue
            ca = self.attrs(child)
            source, target = _path(ca.get("pnodfrom")), _path(ca.get("pnodto"))
            if source is None or target is None:
                # En PF los segmentos grÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡ficos independientes tienen ambos ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­ndices -1.
                if (source is None and target is None and
                        ca.get("inodfrom") == -1 and ca.get("inodto") == -1 and
                        ca.get("indep") == 1):
                    continue
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"BlkSig has no endpoint: {child['relative_path']}")
            port = int(ca["inodto"])
            if target in nodes_by_path and nodes_by_path[target].ir is not None:
                to_name = ca.get("GetToSigName()")
                if to_name in nodes_by_path[target].inputs:
                    port = nodes_by_path[target].inputs.index(to_name)
            if target == type_path and port >= len(ir.outputs):
                signal_name = ca.get("GetToSigName()") or ca.get("loc_name")
                if signal_name in ir.outputs:
                    port = ir.outputs.index(signal_name)
                else:
                    raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f"Unnamed output port: {child['relative_path']}")
            source_port = int(ca["inodfrom"])
            if source in nodes_by_path and nodes_by_path[source].ir is not None:
                from_name = ca.get("GetFromSigName()")
                if from_name in nodes_by_path[source].outputs:
                    source_port = nodes_by_path[source].outputs.index(from_name)
            if source == type_path:
                wire_name = re.sub(r"\(\d+\)$", "", str(ca.get("loc_name", "")))
                if wire_name in ir.inputs:
                    source_port = ir.inputs.index(wire_name)
                elif ca.get("GetFromSigName()") in ir.inputs:
                    source_port = ir.inputs.index(ca["GetFromSigName()"])
            target_row = self.object_at(target)
            sign = -1.0 if target_row["class"] == "BlkSum" and self.attrs(target_row).get(f"sInput{port}") == "-" else 1.0
            wires.append(Wire(child["object_id"], child["relative_path"], source,
                              source_port, target, port, sign))
        if not nodes and not ir.algebraic and not ir.derivatives:
            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Block has no equations or nodes: {type_path}')
        known = {type_path} | {n.relative_path for n in nodes}
        for wire in wires:
            if wire.source not in known or wire.target not in known:
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Wire targets a node that cannot be executed: {wire.relative_path}')
        states = _names(a.get("sStates"))
        for node in nodes:
            if not set(node.state_bindings) <= set(states):
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Unknown external state: {node.relative_path}')
        return CompiledDSL(object_id or root["object_id"], relative_path or type_path,
                           root["object_id"], type_path, ir, dict(parameters),
                           tuple(nodes), tuple(wires), states)

    @staticmethod
    def _check_limits(ir: BlockIR, values: Mapping[str, float]) -> None:
        for limit in ir.limits:
            if limit.kind != "limfix" or limit.target not in values:
                continue
            value = values[limit.target]
            try:
                low = evaluate(limit.lower, values) if limit.lower is not None else None
                high = evaluate(limit.upper, values) if limit.upper is not None else None
            except KeyError:
                continue
            if low is not None and (value < low if limit.lower_closed else value <= low):
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'{limit.target} is below limfix: {ir.relative_path}')
            if high is not None and (value > high if limit.upper_closed else value >= high):
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'{limit.target} is above limfix: {ir.relative_path}')


@dataclass
class CompiledDSL:
    "Compiled block system x'=f(x,u), y=h(x,u), with no local algebraic z variables."

    object_id: str
    relative_path: str
    type_id: str
    type_path: str
    ir: BlockIR
    parameters: dict[str, object]
    nodes: tuple[Node, ...]
    wires: tuple[Wire, ...]
    state_names: tuple[str, ...]
    memory: DSLMemory = field(default_factory=DSLMemory, repr=False)
    initial_constants: dict[str, float] = field(default_factory=dict, repr=False)
    _functions: dict[int, object] = field(default_factory=dict, repr=False)
    _bindings: dict[str, object] = field(default_factory=dict, repr=False)
    _graph_plan: tuple | None = field(default=None, repr=False)
    _evaluation_cache: tuple | None = field(default=None, repr=False)
    _scope_bases: dict[str, dict[str, object]] = field(default_factory=dict, repr=False)
    _uses_memory: bool | None = field(default=None, repr=False)
    _node_initial: dict = field(default_factory=dict, repr=False)
    _children: dict = field(default_factory=dict, repr=False)
    initial_scope: dict = field(default_factory=dict, repr=False)
    _limiter_cache: dict = field(default_factory=dict, repr=False)
    _ordered_once: bool = field(default=False, repr=False)
    _flat_plan: object = field(default=None, repr=False)
    fast_graph: bool = field(default=True, repr=False)
    _runtime_targets: dict = field(default_factory=dict, repr=False)
    cached_accept: bool = field(default=True, repr=False)
    _evaluation_trace: dict = field(default_factory=dict, repr=False)

    def _clear_initial_algebraic(self, ir, scope):
        """Un inc no sustituye una algebraica cuyo cable todavÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­a no llegÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³.

        Los ciclos explÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â­citos con memoria retienen su semilla inc para que
        el evaluador general pueda resolver sus condiciones por partes.
        """
        targets = self._runtime_targets.get(ir.object_id)
        if targets is None:
            names = {s.target for s in ir.algebraic}
            dependencies = {s.target: set(_expression_names(s.value)) & names
                            for s in ir.algebraic}
            def cyclic(name):
                pending, seen = list(dependencies[name]), set()
                while pending:
                    other = pending.pop()
                    if other == name:
                        return True
                    if other not in seen:
                        seen.add(other)
                        pending.extend(dependencies[other])
                return False
            targets = tuple(n for n in names if not cyclic(n))
            self._runtime_targets[ir.object_id] = targets
        for name in targets:
            scope.pop(name, None)

    def _limit_derivative(self, ir, target, scope, value, t_s):
        key = ir.object_id, target
        rules = self._limiter_cache.get(key)
        if rules is None:
            rules = []
            for stmt in ir.algebraic:
                call = stmt.value
                if (isinstance(call, Call) and call.name.lower() == 'picontrol_const'
                        and len(call.args) == 4 and isinstance(call.args[0], Name)
                        and call.args[0].value == target):
                    rules.append(call)
                rules.extend(c for c in _limstate_calls(call)
                             if len(c.args) == 3 and isinstance(c.args[0], Name)
                             and c.args[0].value == target)
            self._limiter_cache[key] = rules
        for call in rules:
            low = float(self._eval(call.args[1], scope, t_s))
            high = float(self._eval(call.args[2], scope, t_s))
            position = float(scope[target])
            if call.name.lower() == 'picontrol_const':
                total = position + float(self._eval(call.args[3], scope, t_s))
                if total < low or total > high:
                    value = 0.
            elif (self.memory.switch_modes.get(-2*id(call), position <= low) and value < 0) or (
                    self.memory.switch_modes.get(-2*id(call)-1, position >= high) and value > 0):
                value = 0.
        return value

    def _child(self, node):
        if node.relative_path not in self._children:
            self._children[node.relative_path] = replace(node.subgraph,
                memory=self.memory, _evaluation_cache=None, _scope_bases={},
                initial_constants={}, _node_initial={}, _children={}, initial_scope={})
        return self._children[node.relative_path]

    @property
    def n_states(self) -> int:
        return len(self.state_names)

    def _eval(self, expr, scope: Mapping[str, object], t_s: float | None = None):
        key = id(expr)
        function = self._functions.get(key)
        if function is None:
            function = compile_expression(expr)
            self._functions[key] = function
        try:
            return function(scope, t_s, self.memory)
        except KeyError as exc:
            if str(exc.args[0]).startswith(('array_', 'oarray_')):
                raise DSLCompileError(f'Required curve missing in active mode: {exc.args[0]} in {self.relative_path}') from exc
            raise

    def _binding(self, text: str):
        expr = self._bindings.get(text)
        if expr is None:
            expr = parse_expression(text)
            self._bindings[text] = expr
        return expr

    def _plan(self):
        if self._graph_plan is None:
            by_target: dict[str, list[Wire]] = {}
            source_ports: dict[str, set[int]] = {}
            for wire in self.wires:
                by_target.setdefault(wire.target, []).append(wire)
                source_ports.setdefault(wire.source, set()).add(wire.source_port)
            from_tags = {node.signal for node in self.nodes if node.kind == "BlkFrom"}
            goto_tags = {node.signal for node in self.nodes if node.kind == "BlkGoto"}
            aliases = []
            for wire in self.wires:
                tag = re.sub(r"\(\d+\)$", "", wire.relative_path.rsplit("\\", 1)[-1]
                             .removesuffix(".BlkSig"))
                if tag in from_tags and tag not in goto_tags:
                    aliases.append((tag, (wire.source, wire.source_port)))
            ordered_nodes = self.nodes
            simple = (not self.ir.algebraic and
                      all(node.kind in ("BlkRef", "BlkSum", "BlkMul", "BlkDiv", "BlkSwt")
                          and node.subgraph is None for node in self.nodes))
            if simple:
                paths = {node.relative_path for node in self.nodes}
                remaining = {node.relative_path: {wire.source for wire in
                             by_target.get(node.relative_path, ()) if wire.source in paths}
                             for node in self.nodes}
                sorted_nodes = []
                while remaining:
                    ready = [node for node in self.nodes
                             if node.relative_path in remaining and not remaining[node.relative_path]]
                    if not ready:
                        simple = False
                        break
                    for node in ready:
                        sorted_nodes.append(node)
                        del remaining[node.relative_path]
                    for dependencies in remaining.values():
                        dependencies.difference_update(node.relative_path for node in ready)
                if simple:
                    ordered_nodes = tuple(sorted_nodes)
            self._graph_plan = by_target, aliases, ordered_nodes, simple, source_ports
        return self._graph_plan

    def _has_memory(self) -> bool:
        if self._uses_memory is None:
            memory = {"picdro", "picdro_const", "flipflop", "aflipflop",
                      "delay", "gradlim_const", "lastvalue", "movingavg"}
            self._uses_memory = bool(self.ir.functions & memory) or any(
                (node.ir is not None and bool(node.ir.functions & memory)) or
                (node.subgraph is not None and node.subgraph._has_memory())
                for node in self.nodes)
        return self._uses_memory

    def _node_scope(self, node: Node, x: Mapping[str, float],
                    inputs: Mapping[str, float]) -> dict[str, object]:
        assert node.ir is not None
        base = self._scope_bases.get(node.relative_path)
        if base is None:
            base = {**self.parameters, **self.initial_constants}
            # Los nombres formales de un bloque tienen ÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â¡mbito local. Un inc
            # homÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³nimo del padre no puede suplir un cable aÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Âºn no propagado
            # (p. ej. yi0 del selector UEL de la referencia angular).
            for name in (*node.inputs, *node.outputs):
                base.pop(name, None)
            base.update(self._node_initial.get(node.relative_path, {}))
            for formal, binding in zip(node.ir.parameters, node.parameter_bindings):
                try:
                    base[formal] = self._eval(self._binding(binding), self.parameters)
                except KeyError as exc:
                    raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Missing parameter {binding}: {node.relative_path}') from exc
            for formal, binding in node.limit_bindings:
                try:
                    base[formal] = self._eval(self._binding(binding), self.parameters)
                except KeyError as exc:
                    raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Missing limit {binding}: {node.relative_path}') from exc
            self._clear_initial_algebraic(node.ir, base)
            self._scope_bases[node.relative_path] = base
        scope = base.copy()
        for formal, binding in zip(node.ir.states, node.state_bindings):
            scope[formal] = x[binding]
        scope.update(inputs)
        return scope

    def evaluate(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[dict[str, float], np.ndarray]:
        'Evaluate without committing memory and retain the evaluation trace for acceptance.'
        if self.memory.observations is not None:
            return self._evaluate_impl(t_s, x, inputs)
        parent_trace = self.memory.trace
        self.memory.trace = {}
        try:
            result = self._evaluate_impl(t_s, x, inputs)
            self._evaluation_trace = self.memory.trace.copy()
            return result
        except KeyError as exc:
            if str(exc.args[0]).startswith(('array_', 'oarray_')):
                raise DSLCompileError(f'Required curve missing in active mode: {exc.args[0]} in {self.relative_path}') from exc
            raise
        finally:
            trace = self.memory.trace
            self.memory.trace = parent_trace
            if parent_trace is not None:
                parent_trace.update(trace)

    def _evaluate_impl(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[dict[str, float], np.ndarray]:
        """Propaga puertos, signos y estados; rechaza ciclos algebraicos irresolubles."""
        x = np.asarray(x, dtype=float).reshape(-1)
        if x.size != self.n_states or not np.isfinite(x).all():
            raise _coded_error(ValueError, 'invalid_dsl_compile_data', 'Invalid DSL state vector')
        cache_key = None
        if self.memory.observations is None:
            cache_key = (float(t_s), x.tobytes(),
                         tuple(sorted((name, float(value)) for name, value in inputs.items())),
                         self.memory.version)
            if self._evaluation_cache is not None and self._evaluation_cache[0] == cache_key:
                if len(self._evaluation_cache) > 3:
                    self.memory.switch_values.update(self._evaluation_cache[3])
                if self.memory.trace is not None:
                    self.memory.trace.update(self._evaluation_trace)
                return self._evaluation_cache[1].copy(), self._evaluation_cache[2].copy()
        state = dict(zip(self.state_names, map(float, x)))
        if self.fast_graph and self.nodes and self.initial_scope:
            from .dsl_flat import compile_graph, GraphNotExplicit
            if self._flat_plan is None:
                try:
                    self._flat_plan = compile_graph(self)
                except GraphNotExplicit:
                    self._flat_plan = False
            if self._flat_plan:
                function, base, states, pins = self._flat_plan
                values = base.copy()
                values.update(zip(states, map(float, x)))
                values.update((p, float(inputs[n])) for p, n in zip(pins, self.ir.inputs))
                outputs, derivative = function(values, t_s, self.memory)
                result = np.asarray(derivative, dtype=float)
                if cache_key is not None:
                    self._evaluation_cache = cache_key, outputs.copy(), result.copy(), self.memory.switch_values.copy()
                return outputs, result
        if not self.nodes:
            scope = {**self.parameters, **self.initial_constants, **state, **inputs}
            self._clear_initial_algebraic(self.ir, scope)
            pending = list(self.ir.algebraic)
            while pending:
                later = []
                for stmt in pending:
                    try:
                        scope[stmt.target] = float(self._eval(stmt.value, scope, t_s))
                    except KeyError:
                        later.append(stmt)
                if len(later) == len(pending):
                    raise _coded_error(ValueError, 'invalid_dsl_compile_data', f'Unresolved algebraic variables in {self.ir.object_id}: {[s.target for s in later]}')
                pending = later
            derivatives = {stmt.target: self._limit_derivative(self.ir, stmt.target, scope,
                           float(self._eval(stmt.value, scope, t_s)), t_s)
                           for stmt in self.ir.derivatives}
            outputs = {name: float(scope[name]) for name in self.ir.outputs}
            result = np.array([derivatives.get(name, 0.0) for name in self.state_names])
            if cache_key is not None:
                self._evaluation_cache = (cache_key, outputs.copy(), result.copy(), self.memory.switch_values.copy())
            return outputs, result
        ports: dict[tuple[str, int], float] = {}
        for i, name in enumerate(self.ir.inputs):
            if name not in inputs:
                raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Missing input {name}: {self.relative_path}')
            ports[(self.type_path, i)] = float(inputs[name])
        by_target, aliases, ordered_nodes, simple, source_ports = self._plan()
        leaf_scopes: dict[str, dict[str, object]] = {}
        sub_derivatives: dict[str, np.ndarray] = {}
        outputs: dict[str, float] = {}
        root_scope = {**self.parameters, **self.initial_constants, **state, **inputs}
        self._clear_initial_algebraic(self.ir, root_scope)
        signals: dict[str, float] = {}
        processed: set[str] = set()
        execution_order = {}
        for _ in range(2 * len(self.nodes) + len(self.wires) + 3):
            changed = False
            for tag, key in aliases:
                if tag not in signals and key in ports:
                    signals[tag] = ports[key]
                    changed = True
            for node in ordered_nodes:
                if node.relative_path in processed:
                    continue
                incoming = by_target.get(node.relative_path, ())
                available = [w for w in incoming if (w.source, w.source_port) in ports]
                if node.kind in ("BlkSum", "BlkMul", "BlkDiv", "BlkSwt"):
                    if len(available) != len(incoming):
                        continue
                    terms = [(w.target_port, w.sign * ports[(w.source, w.source_port)])
                             for w in available]
                    if node.kind == "BlkSum":
                        value = sum(term for _, term in terms)
                    elif node.kind == "BlkMul":
                        if not terms:
                            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'BlkMul has no inputs: {node.relative_path}')
                        value = math.prod(term for _, term in terms)
                    elif node.kind == "BlkDiv":
                        if len(terms) < 2 or not any(port == 0 for port, _ in terms):
                            raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'BlkDiv requires a numerator and a denominator: {node.relative_path}')
                        numerator = next(term for port, term in terms if port == 0)
                        denominator = math.prod(term for port, term in terms if port != 0)
                        if denominator == 0:
                            raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Zero denominator: {node.relative_path}')
                        value = numerator / denominator
                    else:
                        by_port = dict(terms)
                        if set(by_port) != {0, 1, 2}:
                            raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'BlkSwt requires control and two signals: {node.relative_path}')
                        active = by_port[0] > 0.5
                        if node.switch_invert:
                            active = not active
                        value = by_port[1] if active else by_port[2]
                    key = (node.relative_path, 0)
                    if key not in ports:
                        for output_port in source_ports.get(node.relative_path, {0}):
                            ports[(node.relative_path, output_port)] = value
                        execution_order.setdefault(node.relative_path, len(execution_order))
                        changed = True
                    processed.add(node.relative_path)
                    continue
                if node.kind == "BlkGoto":
                    if len(incoming) != 1:
                        raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'BlkGoto requires a source: {node.relative_path}')
                    if len(available) == 1 and node.signal:
                        value = available[0].sign * ports[(available[0].source,
                                                           available[0].source_port)]
                        if node.signal not in signals:
                            signals[node.signal] = value
                            changed = True
                        processed.add(node.relative_path)
                        execution_order.setdefault(node.relative_path, len(execution_order))
                    continue
                if node.kind == "BlkFrom":
                    if incoming:
                        raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'BlkFrom has an input: {node.relative_path}')
                    if node.signal in signals and (node.relative_path, 0) not in ports:
                        ports[(node.relative_path, 0)] = signals[node.signal]
                        changed = True
                        processed.add(node.relative_path)
                        execution_order.setdefault(node.relative_path, len(execution_order))
                    continue
                if node.subgraph is not None and len(available) != len(incoming):
                    continue
                pin: dict[str, float] = {}
                for w in available:
                    if w.target_port >= len(node.inputs):
                        raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Invalid input port: {w.relative_path}')
                    name = node.inputs[w.target_port]
                    if name in pin:
                        raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Input has two wires: {node.relative_path}:{name}')
                    pin[name] = w.sign * ports[(w.source, w.source_port)]
                scope = self._node_scope(node, state, pin)
                if node.subgraph is not None:
                    if set(node.inputs) - set(pin):
                        continue
                    child = self._child(node)
                    if not child.parameters:
                        child.parameters = {name: scope[name] for name in
                            (*node.ir.parameters, *(formal for formal, _ in node.limit_bindings))}
                    child_x = np.array([scope[name] for name in node.ir.states], dtype=float)
                    child_y, child_dx = child.evaluate(t_s, child_x, pin)
                    scope.update(child_y)
                    sub_derivatives[node.relative_path] = child_dx
                    leaf_scopes[node.relative_path] = scope
                    for port, name in enumerate(node.outputs):
                        if name in child_y and (node.relative_path, port) not in ports:
                            ports[(node.relative_path, port)] = child_y[name]
                            execution_order.setdefault(node.relative_path, len(execution_order))
                            changed = True
                    processed.add(node.relative_path)
                    continue
                pending = list(node.ir.algebraic)
                while pending:
                    later = []
                    for stmt in pending:
                        try:
                            scope[stmt.target] = float(self._eval(stmt.value, scope, t_s))
                        except KeyError:
                            later.append(stmt)
                    if len(later) == len(pending):
                        break
                    pending = later
                leaf_scopes[node.relative_path] = scope
                if not pending and len(available) == len(incoming):
                    processed.add(node.relative_path)
                for port, name in enumerate(node.outputs):
                    if name in scope and (node.relative_path, port) not in ports:
                        ports[(node.relative_path, port)] = scope[name]
                        execution_order.setdefault(node.relative_path, len(execution_order))
                        changed = True
            root_scope.update(signals)
            root_scope.update(outputs)
            for stmt in self.ir.algebraic:
                try:
                    value = float(self._eval(stmt.value, root_scope, t_s))
                except KeyError:
                    continue
                if stmt.target not in root_scope:
                    root_scope[stmt.target] = value
                    signals[stmt.target] = value
                    changed = True
                if stmt.target in self.ir.outputs:
                    outputs[stmt.target] = value
            for wire in by_target.get(self.type_path, ()):
                key = (wire.source, wire.source_port)
                if key in ports:
                    if wire.target_port >= len(self.ir.outputs):
                        raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Invalid output port: {wire.relative_path}')
                    name = self.ir.outputs[wire.target_port]
                    outputs[name] = wire.sign * ports[key]
            if (len(processed) == len(self.nodes)
                    and len(outputs) == len(self.ir.outputs)) or not changed:
                break
        missing = set(self.ir.outputs) - set(outputs)
        if missing:
            blocked = []
            for node in self.nodes:
                if node.kind in ("BlkGoto", "BlkFrom") or node.ir is None:
                    continue
                absent = [w.relative_path for w in by_target.get(node.relative_path, ())
                          if (w.source, w.source_port) not in ports]
                if absent and any((node.relative_path, port) not in ports
                                  for port in range(len(node.outputs))):
                    blocked.append((node.relative_path, absent[:2]))
            raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Unresolved outputs {sorted(missing)}: {self.relative_path}. Blocked inputs {blocked[:5]}. Signals {sorted(signals)}')
        derivative_map: dict[str, float] = {}
        for node in self.nodes:
            if node.ir is None:
                continue
            scope = leaf_scopes[node.relative_path]
            if node.subgraph is not None:
                for binding, value in zip(node.state_bindings, sub_derivatives[node.relative_path]):
                    if binding in derivative_map:
                        raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Two equations for {binding}: {node.relative_path}')
                    derivative_map[binding] = float(value)
                continue
            for stmt in node.ir.derivatives:
                try:
                    value = float(self._eval(stmt.value, scope, t_s))
                except KeyError as exc:
                    raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Derivative is missing input {exc}: {node.relative_path}') from exc
                if stmt.target not in node.ir.states:
                    continue
                value = self._limit_derivative(node.ir, stmt.target, scope, value, t_s)
                binding = node.state_bindings[node.ir.states.index(stmt.target)]
                if binding in derivative_map:
                    raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Two equations for {binding}: {node.relative_path}')
                derivative_map[binding] = value
        for stmt in self.ir.derivatives:
            if stmt.target not in self.state_names:
                continue
            if stmt.target in derivative_map:
                raise _coded_error(DSLCompileError, 'invalid_dsl_compile_data', f'Two equations for {stmt.target}: {self.relative_path}')
            derivative_map[stmt.target] = float(self._eval(stmt.value, root_scope, t_s))
        missing = set(self.state_names) - set(derivative_map)
        if missing:
            raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Missing derivatives {sorted(missing)}: {self.relative_path}')
        result = np.array([derivative_map[name] for name in self.state_names])
        if not simple and not self._ordered_once:
            ordered = tuple(sorted(self.nodes, key=lambda n: execution_order.get(n.relative_path, len(self.nodes))))
            self._graph_plan = by_target, aliases, ordered, simple, source_ports
            self._ordered_once = True
        if cache_key is not None:
            self._evaluation_cache = (cache_key, outputs.copy(), result.copy(), self.memory.switch_values.copy())
        return outputs, result

    def initialize(self, known: Mapping[str, float], *, _partial=False) -> tuple[np.ndarray, dict[str, float]]:
        from .dsl_initialization import initialize_graph
        return initialize_graph(self, known, partial=_partial)

    def initial_outputs(self) -> dict[str, float]:
        'Return inc outputs before substituting run-time equations. Discrete consumers must preserve this pre-start side until the first accepted update.'
        missing = set(self.ir.outputs) - set(self.initial_scope)
        if missing:
            raise _coded_error(DSLCompileError, 'missing_dsl_compile_data', f'Unresolved inc outputs {sorted(missing)}: {self.relative_path}')
        return {name: float(self.initial_scope[name]) for name in self.ir.outputs}

    def jacobian(self, t_s: float, x: np.ndarray,
                 inputs: Mapping[str, float]) -> tuple[np.ndarray, np.ndarray]:
        'Return local central-difference Jacobians of state derivatives and outputs.'
        names = self.ir.inputs
        point = np.r_[np.asarray(x, dtype=float), [inputs[name] for name in names]]
        def both(p):
            y, f = self.evaluate(t_s, p[:self.n_states],
                                 dict(zip(names, p[self.n_states:])))
            return np.r_[f, [y[name] for name in self.ir.outputs]]
        jac = finite_difference_jacobian(both, point)
        return jac[:self.n_states], jac[self.n_states:]

    def accept_step(self, t_s: float, x: np.ndarray,
                    inputs: Mapping[str, float]) -> tuple[dict[str, float], np.ndarray]:
        'Commit DSL memory exactly once after an accepted step.'
        if not self._has_memory():
            return self.evaluate(t_s, x, inputs)
        key = (float(t_s), np.asarray(x, dtype=float).reshape(-1).tobytes(),
               tuple(sorted((name, float(value)) for name, value in inputs.items())),
               self.memory.version)
        if self.cached_accept and self._evaluation_cache is not None and self._evaluation_cache[0] == key:
            # La traza pertenece exactamente al estado aceptado y a la misma
            # versiÃƒÆ’Ã†â€™Ãƒâ€ Ã¢â‚¬â„¢ÃƒÆ’Ã¢â‚¬Â ÃƒÂ¢Ã¢â€šÂ¬Ã¢â€žÂ¢ÃƒÆ’Ã†â€™ÃƒÂ¢Ã¢â€šÂ¬Ã…Â¡ÃƒÆ’Ã¢â‚¬Å¡Ãƒâ€šÃ‚Â³n de memoria. Newton nunca confirma las trazas tentativas.
            self.memory.observations = self._evaluation_trace.copy()
            self.memory.finish_accept()
            return self._evaluation_cache[1].copy(), self._evaluation_cache[2].copy()
        self.memory.begin_accept()
        try:
            result = self.evaluate(t_s, x, inputs)
            self.memory.finish_accept()
            return result
        except Exception:
            self.memory.observations = None
            raise

    def next_event_time(self, after_s: float) -> float | None:
        return self.memory.next_event_time(after_s)
