'Dsl flat support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import ast

from .dsl_codegen import _generate
from .dsl_expr import Call, Name, _PURE, linear_lookup


class GraphNotExplicit(ValueError):
    pass


def compile_graph(block):
    from .dsl_compile import _limstate_calls
    base, equations, derivatives = {}, {}, {}
    serial = 0

    def key(scope, name):
        return f'{scope}:{name}'

    def value(name):
        return ast.Subscript(ast.Name('values', ast.Load()), ast.Constant(name), ast.Load())

    def scaled(expr, sign):
        return expr if sign == 1 else ast.BinOp(ast.Constant(sign), ast.Mult(), expr)

    class Rename(ast.NodeTransformer):
        def __init__(self, prefix):
            self.prefix = prefix

        def visit_Subscript(self, node):
            if isinstance(node.value, ast.Name) and node.value.id == 'values':
                return value(key(self.prefix, node.slice.value))
            return self.generic_visit(node)

    def expression(expr, scope):
        return Rename(scope).visit(_generate(expr))

    def put(target, expr):
        if target in equations:
            raise _coded_error(GraphNotExplicit, 'invalid_dsl_flat_data', f'Two equations for {target}')
        equations[target] = expr
        base.pop(target, None)

    def derivative(ir, stmt, scope):
        result = expression(stmt.value, scope)
        for algebraic in ir.algebraic:
            call = algebraic.value
            rules = list(_limstate_calls(call))
            if isinstance(call, Call) and call.name.lower() == 'picontrol_const':
                rules.append(call)
            for call in rules:
                if not isinstance(call.args[0], Name) or call.args[0].value != stmt.target:
                    continue
                low, high = expression(call.args[1], scope), expression(call.args[2], scope)
                position = value(key(scope, stmt.target))
                if call.name.lower() == 'picontrol_const':
                    total = ast.BinOp(position, ast.Add(), expression(call.args[3], scope))
                    condition = ast.BoolOp(ast.Or(), [ast.Compare(total, [ast.Lt()], [low]),
                        ast.Compare(total, [ast.Gt()], [high])])
                else:
                    def bound_mode(side, condition):
                        return ast.Call(ast.Attribute(ast.Attribute(ast.Name('runtime', ast.Load()),
                            'switch_modes', ast.Load()), 'get', ast.Load()),
                            [ast.Constant(-2*id(call)-side), condition], [])
                    condition = ast.BoolOp(ast.Or(), [
                        ast.BoolOp(ast.And(), [bound_mode(0, ast.Compare(position, [ast.LtE()], [low])),
                            ast.Compare(result, [ast.Lt()], [ast.Constant(0.)])]),
                        ast.BoolOp(ast.And(), [bound_mode(1, ast.Compare(position, [ast.GtE()], [high])),
                            ast.Compare(result, [ast.Gt()], [ast.Constant(0.)])])])
                result = ast.IfExp(condition, ast.Constant(0.), result)
        return result

    def walk(b, scope, parameters, constants, state_links, input_links):
        nonlocal serial
        for n, v in {**parameters, **constants}.items():
            if n not in (*b.ir.inputs, *b.ir.outputs, *b.state_names):
                base[key(scope, n)] = v
        for n, target in state_links.items():
            if key(scope, n) != target:
                put(key(scope, n), value(target))
        for n, expr in input_links.items():
            put(key(scope, n), expr)
        for stmt in b.ir.algebraic:
            put(key(scope, stmt.target), expression(stmt.value, scope))
        for stmt in b.ir.derivatives:
            derivatives[state_links[stmt.target]] = derivative(b.ir, stmt, scope)
        paths = {}
        for node in b.nodes:
            serial += 1
            paths[node.relative_path] = f'n{serial}'
        def port(path, index):
            if path == b.type_path:
                return key(scope, b.ir.inputs[index])
            node = next(n for n in b.nodes if n.relative_path == path)
            return key(paths[path], node.outputs[index] if node.ir else f'out{index}')
        incoming = {}
        for wire in b.wires:
            incoming.setdefault(wire.target, []).append(wire)
        _, aliases, _, _, source_ports = b._plan()
        for name, (path, index) in aliases:
            target = key(scope, name)
            if target not in equations and target != port(path, index):
                put(target, value(port(path, index)))
        for wire in incoming.get(b.type_path, ()):
            put(key(scope, b.ir.outputs[wire.target_port]),
                scaled(value(port(wire.source, wire.source_port)), wire.sign))
        for node in b.nodes:
            ns = paths[node.relative_path]
            wires = incoming.get(node.relative_path, ())
            terms = {w.target_port: scaled(value(port(w.source, w.source_port)), w.sign)
                     for w in wires}
            if node.kind == 'BlkGoto':
                put(key(scope, node.signal), terms[0])
                continue
            if node.kind == 'BlkFrom':
                put(key(ns, 'out0'), value(key(scope, node.signal)))
                continue
            if node.ir is None:
                if node.kind in ('BlkSum', 'BlkMul'):
                    expr = ast.Constant(0. if node.kind == 'BlkSum' else 1.)
                    for term in terms.values():
                        expr = ast.BinOp(expr, ast.Add() if node.kind == 'BlkSum' else ast.Mult(), term)
                elif node.kind == 'BlkDiv':
                    expr = terms[0]
                    denominator = ast.Constant(1.)
                    for i, term in terms.items():
                        if i:
                            denominator = ast.BinOp(denominator, ast.Mult(), term)
                    expr = ast.BinOp(expr, ast.Div(), denominator)
                elif node.kind == 'BlkSwt':
                    condition = ast.Compare(terms[0], [ast.Gt()], [ast.Constant(.5)])
                    expr = ast.IfExp(condition, terms[2 if node.switch_invert else 1],
                                     terms[1 if node.switch_invert else 2])
                else:
                    raise _coded_error(GraphNotExplicit, 'invalid_dsl_flat_data', node.kind)
                for i in source_ports.get(node.relative_path, {0}):
                    put(key(ns, f'out{i}'), expr)
                continue
            # La base de ámbito resuelve parámetros formales con el mismo contrato
            # que el evaluador general; los estados no forman parte de esta base.
            seed = {n: 0. for n in b.state_names}
            nodebase = b._node_scope(node, seed, {})
            params = {n: v for n, v in nodebase.items()
                      if n not in (*node.ir.states, *node.inputs, *node.outputs)}
            links = {n: state_links[actual] for n, actual in zip(node.ir.states, node.state_bindings)}
            pins = {node.inputs[i]: expr for i, expr in terms.items()}
            if node.subgraph is not None:
                child = b._child(node)
                walk(child, ns, params, child.initial_constants, links, pins)
            else:
                for n, v in params.items():
                    base[key(ns, n)] = v
                for n, target in links.items():
                    put(key(ns, n), value(target))
                for n, expr in pins.items():
                    put(key(ns, n), expr)
                for stmt in node.ir.algebraic:
                    put(key(ns, stmt.target), expression(stmt.value, ns))
                for stmt in node.ir.derivatives:
                    if stmt.target in links:
                        derivatives[links[stmt.target]] = derivative(node.ir, stmt, ns)

    states = {n: key('root', n) for n in block.state_names}
    walk(block, 'root', block.parameters, block.initial_constants, states, {})
    initial = set(base) | set(states.values()) | {key('root', n) for n in block.ir.inputs}
    body = []
    remaining = dict(equations)
    while remaining:
        progress = False
        for target, expr in tuple(remaining.items()):
            required = {n.slice.value for n in ast.walk(expr) if isinstance(n, ast.Subscript)
                        and isinstance(n.value, ast.Name) and n.value.id == 'values'}
            if not required <= initial:
                continue
            lhs = value(target)
            lhs.ctx = ast.Store()
            body.append(ast.Assign([lhs], expr))
            initial.add(target)
            del remaining[target]
            progress = True
        if not progress:
            missing = {target: sorted({n.slice.value for n in ast.walk(expr)
                if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                and n.value.id == 'values'}-initial) for target, expr in remaining.items()}
            raise _coded_error(GraphNotExplicit, 'missing_dsl_flat_data', f'Algebraic graph is not explicit: {missing}')
    output_keys = [key('root', n) for n in block.ir.outputs]
    if not set(output_keys) <= initial or set(states.values()) != set(derivatives):
        raise _coded_error(GraphNotExplicit, 'invalid_dsl_flat_data', 'Incomplete ports or derivatives')
    body.append(ast.Return(ast.Tuple([
        ast.Dict([ast.Constant(n) for n in block.ir.outputs], [value(n) for n in output_keys]),
        ast.Tuple([derivatives[n] for n in states.values()], ast.Load())], ast.Load())))
    function = ast.FunctionDef('_graph', ast.arguments(posonlyargs=[],
        args=[ast.arg(arg=n) for n in ('values', 'time_s', 'runtime')], vararg=None,
        kwonlyargs=[], kw_defaults=[], kwarg=None, defaults=[]), body, [])
    namespace = {}
    exec(compile(ast.fix_missing_locations(ast.Module([function], [])), '<dsl-grafo-validado>', 'exec'),
         {'__builtins__': {}, '_pure': _PURE, '_lookup': linear_lookup,
          'float': float, 'bool': bool, 'min': min, 'max': max}, namespace)
    return namespace['_graph'], base, tuple(states.values()), tuple(key('root', n) for n in block.ir.inputs)
