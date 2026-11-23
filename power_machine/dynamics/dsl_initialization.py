'Dsl initialization support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import re
import numpy as np

from .dsl_expr import evaluate, parse_expression


def initialize_graph(block, known, *, partial=False):
    'Solve inc equations in local scopes. Preserve unresolved data as missing. Initialization may propagate wires backwards and must distinguish sums, gains and block port semantics.'
    from .dsl_compile import DSLCompileError
    scope = {**block.parameters, **known}
    local = {n.relative_path: {} for n in block.nodes}
    ports = {}
    tags = {}
    root = block.type_path
    changed = False

    def put(mapping, key, value):
        nonlocal changed
        if key not in mapping:
            mapping[key] = value
            changed = True

    def equations(ir, values):
        for stmt in (*ir.initializers, *ir.algebraic):
            if stmt.target in values:
                continue
            try:
                put(values, stmt.target, float(evaluate(stmt.value, values,
                    time_s=0.0, runtime=block.memory)))
            except KeyError:
                pass

    def port_key(path, index, source):
        return (path, 'in' if (path == root) == source else 'out', index)

    def publish(path, values, inputs, outputs):
        for side, names in (('in', inputs), ('out', outputs)):
            for i, name in enumerate(names):
                key = path, side, i
                if name in values:
                    put(ports, key, values[name])
                elif key in ports:
                    put(values, name, ports[key])

    for _ in range(4 * (len(block.nodes) + len(block.wires) + 2)):
        changed = False
        equations(block.ir, scope)
        publish(root, scope, block.ir.inputs, block.ir.outputs)
        for wire in block.wires:
            src = port_key(wire.source, wire.source_port, True)
            dst = port_key(wire.target, wire.target_port, False)
            name = re.sub(r'\(\d+\)$', '', wire.relative_path.rsplit('\\', 1)[-1]
                          .removesuffix('.BlkSig'))
            if name in scope:
                put(ports, src, scope[name])
            if src in ports:
                put(ports, dst, wire.sign * ports[src])
                put(scope, name, ports[src])
                put(tags, name, ports[src])
            elif dst in ports:
                put(ports, src, ports[dst] / wire.sign)
        for node in block.nodes:
            path = node.relative_path
            values = local[path]
            incoming = [w for w in block.wires if w.target == path]
            if node.ir is None:
                if node.kind == 'BlkGoto':
                    key = path, 'in', 0
                    if key in ports:
                        put(tags, node.signal, ports[key])
                elif node.kind == 'BlkFrom':
                    if node.signal in tags:
                        put(ports, (path, 'out', 0), tags[node.signal])
                else:
                    terms = {w.target_port: ports[path, 'in', w.target_port]
                             for w in incoming if (path, 'in', w.target_port) in ports}
                    outkeys = [(path, 'out', w.source_port) for w in block.wires
                               if w.source == path]
                    out = next((ports[k] for k in outkeys if k in ports), None)
                    if len(terms) == len(incoming):
                        if node.kind == 'BlkSum':
                            out = sum(terms.values())
                        elif node.kind == 'BlkMul':
                            out = float(np.prod(list(terms.values())))
                        elif node.kind == 'BlkDiv' and terms:
                            den = float(np.prod([v for p, v in terms.items() if p != 0]))
                            if den:
                                out = terms[0] / den
                        elif node.kind == 'BlkSwt' and set(terms) == {0, 1, 2}:
                            active = terms[0] > .5
                            out = terms[1 if active != node.switch_invert else 2]
                    elif out is not None and len(terms) == len(incoming)-1:
                        missing = next(w.target_port for w in incoming
                                       if w.target_port not in terms)
                        value = None
                        if node.kind == 'BlkSum':
                            value = out - sum(terms.values())
                        elif node.kind == 'BlkMul':
                            product = float(np.prod(list(terms.values())))
                            if product:
                                value = out / product
                        elif node.kind == 'BlkDiv':
                            if missing == 0:
                                value = out * float(np.prod(list(terms.values())))
                            elif out:
                                product = float(np.prod([v for p, v in terms.items() if p != 0]))
                                if product:
                                    value = terms[0] / out / product
                        if value is not None:
                            put(ports, (path, 'in', missing), value)
                    if out is not None:
                        for key in outkeys:
                            put(ports, key, out)
                continue
            for name, binding in (*zip(node.ir.parameters, node.parameter_bindings),
                                  *node.limit_bindings):
                try:
                    put(values, name, evaluate(parse_expression(binding), scope))
                except KeyError:
                    pass
            for name, binding in zip(node.ir.states, node.state_bindings):
                if binding in scope:
                    put(values, name, scope[binding])
                elif name in values:
                    put(scope, binding, values[name])
            publish(path, values, node.inputs, node.outputs)
            if node.subgraph is not None:
                child = block._child(node)
                child.parameters = {k: values[k] for k in
                                    (*node.ir.parameters, *(p for p, _ in node.limit_bindings))
                                    if k in values}
                child.initialize(values, _partial=True)
                for name, value in child.initial_scope.items():
                    put(values, name, value)
            else:
                equations(node.ir, values)
            block._node_initial[path] = {
                s.target: values[s.target] for s in node.ir.initializers
                if s.target in values and s.target not in (*node.ir.states, *node.inputs)}
        if not changed:
            break
    block.initial_scope = scope
    block.initial_constants = {s.target: scope[s.target] for s in block.ir.initializers
        if s.target in scope and s.target not in (*block.state_names, *block.ir.inputs)}
    block._scope_bases.clear()
    block._evaluation_cache = None
    block._flat_plan = None
    missing = set(block.state_names) - set(scope)
    if missing and not partial:
        raise _coded_error(DSLCompileError, 'missing_dsl_initialization_data', f'inc is missing states {sorted(missing)}: {block.relative_path}')
    return (np.array([scope.get(s, np.nan) for s in block.state_names], dtype=float),
            {name: scope[name] for name in block.ir.inputs if name in scope})
