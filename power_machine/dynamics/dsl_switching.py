'Dsl switching support for power-system RMS dynamics.'
from __future__ import annotations

from dataclasses import dataclass
import math

from .dsl_expr import Binary, Call, Name, Number, Unary, evaluate


COMPARISONS = frozenset(('<', '<=', '>', '>=', '=', '==', '!=', '<>'))


def expression_text(expr):
    if isinstance(expr, Name):
        return expr.value
    if isinstance(expr, Number):
        return repr(expr.value)
    if isinstance(expr, Unary):
        return f'{expr.op}({expression_text(expr.operand)})'
    if isinstance(expr, Binary):
        return f'({expression_text(expr.left)} {expr.op} {expression_text(expr.right)})'
    if isinstance(expr, Call):
        return f'{expr.name}({", ".join(map(expression_text, expr.args))})'
    return repr(expr)


def names(expr):
    if isinstance(expr, Name):
        return {expr.value}
    if isinstance(expr, Unary):
        return names(expr.operand)
    if isinstance(expr, Binary):
        return names(expr.left) | names(expr.right)
    if isinstance(expr, Call):
        return set().union(*(names(a) for a in expr.args))
    return set()


def walk(expr):
    yield expr
    if isinstance(expr, Unary):
        yield from walk(expr.operand)
    elif isinstance(expr, Binary):
        yield from walk(expr.left)
        yield from walk(expr.right)
    elif isinstance(expr, Call):
        for arg in expr.args:
            yield from walk(arg)


def truth(op, a, b):
    if op == '<':
        return a < b
    if op == '<=':
        return a <= b
    if op == '>':
        return a > b
    if op == '>=':
        return a >= b
    if op in ('=', '=='):
        return a == b
    return a != b


@dataclass(frozen=True)
class StateSurface:
    'Constant switching plane x[index]=level in the exported state units.'
    key: int
    state_index: int
    level: float
    op: str
    state_left: bool
    metadata: dict

    def margin(self, x):
        return float(x[self.state_index])-self.level

    def mode(self, margin):
        return truth(self.op, margin, 0.) if self.state_left else truth(self.op, 0., margin)


@dataclass(frozen=True)
class ExpressionSurface:
    key: int
    op: str
    metadata: dict

    def mode(self, margin):
        return truth(self.op, margin, 0.)


def continuity_map(block, discrete_inputs=()):
    'Follow wires and macros to distinguish continuous states from clocks. Discrete selectors and memory are not continuous crossing functions.'
    equations, roots, guards = {}, {}, {}
    serial = 0
    discontinuous = {'select', 'select_const', 'selfix', 'selfix_const',
                     'picdro', 'picdro_const', 'flipflop', 'aflipflop',
                     'lastvalue', 'modulo'}

    def describe(expr, scope):
        deps = {(scope, n) for n in names(expr)}
        smooth = all(not (isinstance(e, Binary) and e.op in (*COMPARISONS, '.and.', '.or.'))
                     and not (isinstance(e, Call) and e.name.lower() in discontinuous)
                     for e in walk(expr))
        return smooth, deps

    def add_ir(ir, scope):
        for n in (*ir.states, *ir.parameters):
            roots[scope, n] = True
        for stmt in ir.algebraic:
            equations[scope, stmt.target] = describe(stmt.value, scope)
        for stmt in (*ir.algebraic, *ir.derivatives):
            for expr in walk(stmt.value):
                if isinstance(expr, Binary) and expr.op in COMPARISONS:
                    guards[id(expr)] = [describe(e, scope) for e in (expr.left, expr.right)]
                elif isinstance(expr, Call) and expr.name.lower() in (
                        'lim', 'lim_const', 'limstate', 'limstate_const'):
                    for side in (0, 1):
                        guards[-2*id(expr)-side] = [describe(e, scope)
                            for e in (expr.args[0], expr.args[side+1])]

    def visit(b, scope):
        nonlocal serial
        add_ir(b.ir, scope)
        child_scopes = {}
        for node in b.nodes:
            serial += 1
            child_scopes[node.relative_path] = serial
        by_path = {n.relative_path: n for n in b.nodes}

        def port(path, index, output):
            if path == b.type_path:
                return scope, (b.ir.outputs if output else b.ir.inputs)[index]
            node = by_path[path]
            labels = node.outputs if output else node.inputs
            return child_scopes[path], labels[index] if index < len(labels) else f'port{index}'

        for wire in b.wires:
            source = port(wire.source, wire.source_port, wire.source != b.type_path)
            target = port(wire.target, wire.target_port, wire.target == b.type_path)
            equations[target] = True, {source}
        for node in b.nodes:
            ns = child_scopes[node.relative_path]
            if node.ir is None:
                incoming = {key for key in equations if key[0] == ns}
                outgoing = {port(w.source, w.source_port, True) for w in b.wires
                            if w.source == node.relative_path}
                for target in outgoing:
                    equations[target] = node.kind != 'BlkSwt', incoming
                if node.kind == 'BlkGoto' and incoming:
                    equations[scope, node.signal] = True, incoming
                elif node.kind == 'BlkFrom':
                    for target in outgoing:
                        equations[target] = True, {(scope, node.signal)}
                continue
            if node.subgraph is not None:
                visit(b._child(node), ns)
            else:
                add_ir(node.ir, ns)
    visit(block, 0)
    for n in block.ir.inputs:
        roots[0, n] = n not in discrete_inputs
    cache = {}

    def resolve(key, seen=frozenset()):
        if key in cache:
            return cache[key]
        if key in seen:
            return False
        if key in equations:
            smooth, deps = equations[key]
            result = smooth and all(resolve(d, seen | {key}) for d in deps)
        else:
            result = roots.get(key, True)
        cache[key] = result
        return result
    surfaces = {key: all(smooth and all(resolve(d) for d in deps)
                        for smooth, deps in descriptions)
                for key, descriptions in guards.items()}
    outputs = {n: resolve((0, n)) for n in block.ir.outputs}
    return surfaces, outputs


def inspect_block(block, discrete_inputs=()):
    'Inventory the full IR and resolve constant state planes. Retain unsupported input, memory and algebraic guards in the coverage inventory.'
    inventory, surfaces, expressions = [], [], []
    continuity, _ = continuity_map(block, discrete_inputs)

    def inspect(ir, scope_path, bindings, parameters):
        dependencies = {n: {n} for n in (*ir.states, *ir.inputs)}
        equations = {s.target: s.value for s in ir.algebraic}

        def continuous(expr, seen=frozenset()):
            if isinstance(expr, Name):
                if expr.value in seen:
                    return False
                return expr.value not in equations or continuous(equations[expr.value], seen | {expr.value})
            if isinstance(expr, Binary):
                return expr.op not in COMPARISONS and expr.op not in ('.and.', '.or.') and continuous(expr.left, seen) and continuous(expr.right, seen)
            if isinstance(expr, Unary):
                return expr.op != '.not.' and continuous(expr.operand, seen)
            if isinstance(expr, Call):
                return expr.name.lower() in ('lim', 'limstate', 'lim_const', 'limstate_const',
                    'min', 'max', 'abs', 'sqrt', 'sqr', 'exp', 'sin', 'cos', 'tan', 'pow',
                    'lapprox', 'invlapprox') and all(continuous(arg, seen) for arg in expr.args)
            return True
        for _ in range(len(ir.algebraic)+1):
            for statement in ir.algebraic:
                dependencies[statement.target] = set().union(
                    *(dependencies.get(n, set()) for n in names(statement.value)))
        for statement in (*ir.algebraic, *ir.derivatives):
            for expr in walk(statement.value):
                kind = None
                if isinstance(expr, Binary) and expr.op in COMPARISONS:
                    kind = 'comparison'
                elif isinstance(expr, Call) and expr.name.lower() in (
                        'select', 'select_const', 'selfix', 'selfix_const',
                        'lim', 'lim_const', 'limstate', 'limstate_const', 'picontrol_const',
                        'min', 'max', 'abs'):
                    kind = expr.name.lower()
                if kind is None:
                    continue
                inspected = expr.args[0] if kind in ('select', 'select_const', 'selfix', 'selfix_const') else expr
                deps = set().union(*(dependencies.get(n, set()) for n in names(inspected)))
                row = dict(object_id=block.object_id, relative_path=block.relative_path,
                           type_path=ir.relative_path, scope_path=scope_path,
                           target=statement.target, derivative=statement.derivative,
                           kind=kind, expression=expression_text(expr),
                           states=sorted(deps & set(ir.states)),
                           inputs=sorted(deps & set(ir.inputs)), runtime='expression')
                if kind == 'comparison':
                    for lhs, rhs, state_left in ((expr.left, expr.right, True),
                                                (expr.right, expr.left, False)):
                        if not isinstance(lhs, Name) or lhs.value not in bindings:
                            continue
                        try:
                            level = float(evaluate(rhs, parameters))
                        except (KeyError, ValueError, TypeError):
                            continue
                        if math.isfinite(level):
                            row['runtime'] = 'state_surface'
                            row['state_index'] = bindings[lhs.value]
                            row['level'] = level
                            surfaces.append(StateSurface(id(expr), bindings[lhs.value], level,
                                                         expr.op, state_left, row))
                            break
                    if row['runtime'] == 'expression' and (row['states'] or row['inputs']):
                        if continuity.get(id(expr), False):
                            expressions.append(ExpressionSurface(id(expr), expr.op, row))
                            row['runtime'] = 'continuous_expression_surface'
                        else:
                            row['runtime'] = 'derived_discrete_condition'
                elif kind in ('lim', 'lim_const', 'limstate', 'limstate_const'):
                    lhs = expr.args[0]
                    try:
                        fixed = float(evaluate(expr.args[1], parameters)) == float(evaluate(expr.args[2], parameters))
                    except (KeyError, ValueError, TypeError):
                        fixed = False
                    if fixed and kind in ('lim', 'lim_const'):
                        row['runtime'] = 'constant_output'
                        inventory.append(row)
                        continue
                    for side, rhs, op in ((0, expr.args[1], '<='), (1, expr.args[2], '>=')):
                        detail = {**row, 'side': 'lower' if side == 0 else 'upper'}
                        if isinstance(lhs, Name) and lhs.value in bindings:
                            try:
                                level = float(evaluate(rhs, parameters))
                            except (KeyError, ValueError, TypeError):
                                level = float('nan')
                            if math.isfinite(level):
                                detail.update(state_index=bindings[lhs.value], level=level)
                                surfaces.append(StateSurface(-2*id(expr)-side, bindings[lhs.value],
                                                             level, op, True, detail))
                                row['runtime'] = 'state_surface'
                                continue
                        if (row['states'] or row['inputs']) and continuity.get(-2*id(expr)-side, False):
                            detail['runtime'] = 'continuous_expression_surface'
                            expressions.append(ExpressionSurface(-2*id(expr)-side, op, detail))
                            row['runtime'] = 'continuous_expression_surface'
                inventory.append(row)
        for limit in ir.limits:
            inventory.append(dict(object_id=block.object_id, relative_path=block.relative_path,
                type_path=ir.relative_path, scope_path=scope_path, target=limit.target,
                kind=limit.kind, states=[limit.target] if limit.target in ir.states else [],
                inputs=[], expression=str(limit), runtime='declaration'))

    def recurse(b, bindings, params, path):
        inspect(b.ir, path, bindings, {**params, **b.initial_constants})
        for node in b.nodes:
            if node.ir is None:
                if node.kind == 'BlkSwt':
                    inventory.append(dict(object_id=block.object_id, relative_path=block.relative_path,
                        type_path=b.type_path, scope_path=node.relative_path,
                        kind='BlkSwt', expression='control > 0.5', states=[], inputs=['control'],
                        runtime='expression'))
                continue
            scope = b._node_scope(node, {n: 0. for n in b.state_names}, {})
            for state_name in node.ir.states:
                scope.pop(state_name, None)
            local = {n: bindings[v] for n, v in zip(node.ir.states, node.state_bindings)}
            if node.subgraph is not None:
                recurse(b._child(node), local, scope, node.relative_path)
            else:
                inspect(node.ir, node.relative_path, local, scope)
    recurse(block, {n: i for i, n in enumerate(block.state_names)}, block.parameters, block.relative_path)
    block._expression_surfaces = expressions
    return inventory, surfaces


def device_blocks(device):
    'Return the runtime device DSL blocks and their state offsets.'
    if hasattr(device, 'blocks') and hasattr(device, 'slices'):
        return [(b, device.slices[p].start) for p, b in device.blocks.items()]
    if hasattr(device, 'machine'):
        offset = device.machine.n_states
        result = []
        for b in (device.avr, device.governor, *device.frame_blocks):
            if b is not None:
                result.append((b, offset))
                offset += b.n_states
        return result
    controller = getattr(device, 'controller', None)
    if controller is not None:
        result, offset = [], 0
        for b in (controller.reec, controller.regc, controller.protection):
            result.append((b, offset))
            offset += b.n_states
        return result
    return []


def device_discrete_inputs(device):
    'Propagate discrete-input provenance between ElmDsl and ElmSamp blocks.'
    if not hasattr(device, 'connections') or not hasattr(device, 'blocks'):
        return {}
    result = {p: set() for p in device.blocks}
    outputs = {}
    for _ in range(len(result)+1):
        changed = False
        for p, block in device.blocks.items():
            for name in block.ir.inputs:
                source = device.connections.get((p, name))
                if source is not None and (source[0] in getattr(device, 'samplers', {})
                                          or outputs.get(source) is False):
                    if name not in result[p]:
                        result[p].add(name)
                        changed = True
            _, continuous_outputs = continuity_map(block, result[p])
            for name, continuous in continuous_outputs.items():
                if outputs.get((p, name)) != continuous:
                    outputs[p, name] = continuous
                    changed = True
        if not changed:
            break
    return result
