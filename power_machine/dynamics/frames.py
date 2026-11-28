"""Executable, positional reference simulator composite frames (stdlib and numpy)."""
from dataclasses import dataclass
import numpy as np

from .dsl_compile import DSLCompileError, _path


class FrameError(DSLCompileError):
    pass


def port_groups(value):
    """Comma delimits groups, semicolon delimits members. Never flatten first."""
    if value is None:
        return ()
    rows = [value] if isinstance(value, str) else value
    rows = [row for row in rows if str(row).strip()]
    groups = tuple(tuple(p.strip() for p in g.split(';'))
                   for row in rows for g in str(row).split(','))
    if any(not p for g in groups for p in g):
        raise FrameError('Empty member in port declaration')
    return groups


def names(value):
    return tuple(p for g in port_groups(value) for p in g)


@dataclass
class NativeBlock:
    """Explicit adapter for Python cores, including IEC modules.

    run(t,x,inputs) -> (output mapping, derivative vector).
    start(known) -> (state vector, inferred signal mapping).
    partial may publish state-only outputs before all inputs are available.
    """
    inputs: tuple
    outputs: tuple
    n_states: int
    run: object
    start: object
    partial: object = None
    accept: object = None

    def evaluate(self, t, x, u):
        return self.run(t, x, u)

    def initialize(self, known):
        return self.start(known)

    @classmethod
    def pss2b(cls, core):
        def run(t, x, u):
            y, dx = core.evaluate(x, u['Vs1'], u['Vs2'])
            return {'Vst': y}, dx
        def start(k):
            if not {'Vs1', 'Vs2'} <= k.keys():
                raise KeyError('Vs1/Vs2')
            return core.init(k['Vs1'], k['Vs2']), {}
        return cls(('Vs1', 'Vs2'), ('Vst',), core.n_states, run, start)

    @classmethod
    def synchronous(cls, machine, voltage, power_mva):
        """Native machine at an imposed bus phasor, LF fixes ve and pt.

        speed is deviation from 1 pu. P/Q are on total machine MVA base.
        The governor is held at LF mechanical power unless pt is wired using
        a separate explicit adapter. Trial evaluations use a private machine.
        """
        import copy
        from .machine_init import initialize_machine
        core = copy.copy(machine)
        initial = initialize_machine(core, voltage(0.), power_mva)
        def outputs(t, x, u):
            v = voltage(t)
            y = core.outputs(x, v)
            base = core.sgn_mva*core.n_units
            return {'ut': abs(v)*core.bus_un_kv/core.ugn_kv,
                    'speed': y['xspeed']-1., 'P': y['P1_mw']/base,
                    'Q': y['Q1_mvar']/base,
                    'id': y['id_pu'], 'iq': y['iq_pu']}
        def run(t, x, u):
            core.ve = u['ve']
            return outputs(t, x, u), core.f(t, x, np.empty(0), voltage(t))
        def start(k):
            return initial.state.copy(), {'ve': initial.ve0, **outputs(0., initial.state, {})}
        return cls(('ve',), ('ut', 'speed', 'P', 'Q', 'id', 'iq'),
                   core.n_states, run, start, outputs)

    @classmethod
    def measurement(cls, core):
        ports = ('u', 'i', 'p', 'q', 'f')
        def run(t, x, u):
            y, dx = core.evaluate(x, *(u[n] for n in ports))
            return dict(zip(ports, y)), dx
        def start(k):
            return np.array([k[n] for n in ports]), {}
        # Zero time constants have feedthrough, the other outputs are states.
        def partial(t, x, u):
            return {n: x[i] for i, (n, T) in enumerate(zip(ports, vars(core).values())) if T > 0}
        return cls(ports, ports, 5, run, start, partial)


class NetworkBlock(NativeBlock):
    """Network boundary. Signals use explicit equipment bases, positive generation.

    read(t) supplies current network/rotor observations. Commands are returned
    by Frame.commands(), never committed during a trial RHS evaluation.
    """
    PORTS = {
        'ElmSym': (('ve', 'pt', 'fref'), ('ut', 'speed', 'P', 'Q', 'f', 'id', 'iq')),
        'ElmGenstat': (('ip', 'iq', 'Pext', 'Qext'), ('ut', 'P', 'Q', 'f')),
        'ElmPvsys': (('ip', 'iq', 'Pext', 'Qext'), ('ut', 'P', 'Q', 'f')),
        'StaVmea': ((), ('U', 'ur', 'ui')),
        'StaPqmea': ((), ('P', 'Q')),
        'StaFrqmea': ((), ('f',)),
    }

    def __init__(self, kind, read, load_flow):
        inputs, outputs = self.PORTS[kind]
        self.read = read
        self.load_flow = dict(load_flow)
        super().__init__(inputs, outputs, 0,
                         lambda t, x, u: (dict(read(t)), np.empty(0)),
                         lambda k: (np.empty(0), self.load_flow),
                         lambda t, x, u: dict(read(t)))


class DSLBlock:
    def __init__(self, compiled):
        self.block = compiled
        self.inputs, self.outputs = compiled.ir.inputs, compiled.ir.outputs
        self.n_states = compiled.n_states

    def partial(self, t, x, u):
        b = self.block
        scope = {**b.parameters, **b.initial_constants, **u, **dict(zip(b.state_names, x))}
        local = {n.relative_path: {} for n in b.nodes}
        by_path = {n.relative_path:n for n in b.nodes}
        tags = {}
        def equations(ir, values):
            for stmt in ir.algebraic:
                if stmt.target not in values:
                    try:
                        values[stmt.target] = b._eval(stmt.value, values, t)
                    except KeyError:
                        pass
        def port(path, index, source):
            if path == b.type_path:
                return scope, (b.ir.inputs if source else b.ir.outputs)[index]
            node = by_path[path]
            ports = node.outputs if source else node.inputs
            return local[path], ports[index] if node.ir else ('out' if source else f'in{index}')
        for _ in range(2*(len(b.nodes)+len(b.wires)+len(b.ir.algebraic)+1)):
            count = len(scope)+sum(map(len,local.values()))+len(tags)
            equations(b.ir, scope)
            for wire in b.wires:
                src, sn = port(wire.source, wire.source_port, True)
                dst, dn = port(wire.target, wire.target_port, False)
                if sn in src and dn not in dst:
                    dst[dn] = wire.sign*src[sn]
            for node in b.nodes:
                v = local[node.relative_path]
                if node.ir:
                    for name, binding in (*zip(node.ir.parameters,node.parameter_bindings), *node.limit_bindings):
                        if name not in v:
                            try:
                                from .dsl_expr import parse_expression
                                v[name] = b._eval(parse_expression(binding),scope,t)
                            except KeyError:
                                pass
                    for name, binding in zip(node.ir.states,node.state_bindings):
                        if binding in scope:
                            v[name] = scope[binding]
                    v.update({k:val for k,val in b._node_initial.get(node.relative_path,{}).items() if k not in v})
                    if node.subgraph:
                        child = b._child(node)
                        child.parameters.update({n:v[n] for n in node.ir.parameters if n in v})
                        if all(n in v for n in child.state_names):
                            v.update(DSLBlock(child).partial(t,np.array([v[n] for n in child.state_names]),
                                                            {n:v[n] for n in child.ir.inputs if n in v}))
                    else:
                        equations(node.ir,v)
                else:
                    incoming = [w for w in b.wires if w.target == node.relative_path]
                    terms = {w.target_port:v[f'in{w.target_port}'] for w in incoming if f'in{w.target_port}' in v}
                    if node.kind == 'BlkFrom' and node.signal in tags:
                        v['out'] = tags[node.signal]
                    elif node.kind == 'BlkGoto' and 'in0' in v:
                        tags[node.signal] = v['in0']
                    elif len(terms) == len(incoming) and incoming:
                        if node.kind == 'BlkSum':
                            v['out'] = sum(terms.values())
                        elif node.kind == 'BlkMul':
                            v['out'] = float(np.prod(list(terms.values())))
                        elif node.kind == 'BlkDiv':
                            v['out'] = terms[0]/float(np.prod([v for k,v in terms.items() if k != 0]))
                        elif node.kind == 'BlkSwt':
                            v['out'] = terms[1 if (terms[0]>.5) != node.switch_invert else 2]
            if count == len(scope)+sum(map(len,local.values()))+len(tags):
                break
        return {n: scope[n] for n in self.outputs if n in scope}

    def evaluate(self, t, x, u):
        return self.block.evaluate(t, x, u)

    def initialize(self, known):
        x, _ = self.block.initialize(known, _partial=True)
        if not np.isfinite(x).all():
            raise KeyError('unresolved states')
        return x, self.block.initial_scope


class Frame:
    """Flattened signal graph with one state allocation per physical object."""
    def __init__(self):
        self.blocks = {}
        self.links = []
        self.bindings = []
        self.inputs = ()
        self.outputs = ()
        self.root = ''
        self._slices = {}

    @property
    def n_states(self):
        return sum(b.n_states for b in self.blocks.values())

    def _allocate(self):
        offset = 0
        for key, b in self.blocks.items():
            self._slices[key] = slice(offset, offset+b.n_states)
            offset += b.n_states
        drivers = {}
        for src, dst in self.links:
            if dst in drivers and drivers[dst] != src:
                raise FrameError(f'Multiple signal drivers for {dst}: {drivers[dst]}, {src}')
            drivers[dst] = src

    def _propagate(self, values, backward=False):
        changed = False
        for src, dst in self.links:
            for a, b in ((src, dst), (dst, src)) if backward else ((src, dst),):
                if a in values:
                    if b in values:
                        if not np.isclose(values[a], values[b], rtol=1e-9, atol=1e-10):
                            raise FrameError(f'Inconsistent initial signals {a} and {b}')
                    else:
                        values[b] = values[a]
                        changed = True
        return changed

    def evaluate(self, t_s, x, inputs):
        x = np.asarray(x, dtype=float)
        if x.shape != (self.n_states,) or not np.isfinite(x).all():
            raise FrameError('Invalid frame state vector')
        values = {(self.root, 'in', n): float(v) for n, v in inputs.items()}
        done = set()
        dx = np.zeros(self.n_states)
        for _ in range(2*(len(self.links)+len(self.blocks)+1)):
            changed = self._propagate(values)
            for key, b in self.blocks.items():
                u = {n: values[key, 'in', n] for n in b.inputs if (key, 'in', n) in values}
                part = b.partial(t_s, x[self._slices[key]], u) if b.partial else {}
                for n, v in part.items():
                    if (key, 'out', n) not in values:
                        values[key, 'out', n] = float(v)
                        changed = True
                if key not in done and (isinstance(b, NetworkBlock) or set(b.inputs) <= u.keys()):
                    try:
                        out, d = b.evaluate(t_s, x[self._slices[key]], u)
                    except KeyError as exc:
                        raise FrameError(f'Required signal or curve missing in {key}: {exc}') from exc
                    if np.asarray(d).shape != (b.n_states,) or not np.isfinite(d).all():
                        raise FrameError(f'Invalid derivative vector in {key}')
                    dx[self._slices[key]] = d
                    for n, v in out.items():
                        values[key, 'out', n] = float(v)
                    done.add(key)
                    changed = True
            if not changed:
                break
        missing = [(k, n) for k, b in self.blocks.items() for n in b.inputs if (k, 'in', n) not in values]
        absent = [n for n in self.outputs if (self.root, 'out', n) not in values]
        required_missing = [(k,n) for k,n in missing if not isinstance(self.blocks[k], NetworkBlock)
                            or any(dst == (k,'in',n) for _,dst in self.links)]
        if len(done) != len(self.blocks) or absent or required_missing:
            raise FrameError(f'Algebraic loop or unconnected frame input: {missing}, outputs {absent}')
        self.last_signals = values
        return {n: values[self.root, 'out', n] for n in self.outputs}, dx

    def initialize(self, known, *, tolerance=1e-8):
        """Propagate LF outputs backwards, then solve declared inc equations."""
        values = dict(known)  # keys are (instance path, in/out, port)
        states = {}
        for _ in range(2*(len(self.links)+len(self.blocks)+1)):
            changed = self._propagate(values, backward=True)
            for key, b in self.blocks.items():
                local = {n: values[key, side, n] for side, ports in (('in', b.inputs), ('out', b.outputs))
                         for n in ports if (key, side, n) in values}
                try:
                    state, inferred = b.initialize(local)
                except KeyError:
                    continue
                states[key] = state
                if isinstance(b, NativeBlock) and not isinstance(b, NetworkBlock) and set(b.inputs) <= local.keys():
                    out, _ = b.evaluate(0., state, {n: local[n] for n in b.inputs})
                    inferred = {**out, **inferred}
                for side, ports in (('in', b.inputs), ('out', b.outputs)):
                    for n in ports:
                        if n in inferred and (key, side, n) not in values:
                            values[key, side, n] = inferred[n]
                            changed = True
            if not changed:
                break
        missing = set(self.blocks)-states.keys()
        if missing:
            raise FrameError(f'Backward initialization unresolved for {sorted(missing)}')
        x = np.concatenate([np.asarray(states[k]) for k in self.blocks]) if states else np.empty(0)
        u = {n: values[self.root, 'in', n] for n in self.inputs if (self.root, 'in', n) in values}
        _, dx = self.evaluate(0., x, u)
        for key, v in values.items():
            if key in self.last_signals and not np.isclose(v, self.last_signals[key], atol=tolerance, rtol=0):
                raise FrameError(f'Load-flow constraint not satisfied: {key}')
        if dx.size and np.max(abs(dx)) > tolerance:
            raise FrameError(f'Frame initialization is not at equilibrium: {np.max(abs(dx)):.6g}')
        return x, u

    def commands(self):
        return {k: {n: self.last_signals[k, 'in', n] for n in b.inputs if (k, 'in', n) in self.last_signals}
                for k, b in self.blocks.items() if isinstance(b, NetworkBlock)}

    def accept_step(self, t, x, inputs):
        self.evaluate(t, x, inputs)
        for key, b in self.blocks.items():
            u = {n: self.last_signals[key, 'in', n] for n in b.inputs if (key, 'in', n) in self.last_signals}
            if isinstance(b, DSLBlock):
                b.block.accept_step(t, x[self._slices[key]], u)
            elif b.accept:
                b.accept(t, x[self._slices[key]], u)


def compile_frame(catalog, composite_path, *, adapters=None):
    """Compile ElmComp by positional pblk/pelm, preserving holes and aliases.

    adapters maps physical instance paths to NativeBlock or NetworkBlock.
    No vendor DLL is inferred from a name or substituted with a generic core.
    """
    adapters = adapters or {}
    result = Frame()
    result.root = composite_path
    def walk(path, stack):
        if path in stack:
            raise FrameError(f'Recursive ElmComp: {stack + (path,)}')
        comp = catalog.object_at(path)
        a = catalog.attrs(comp)
        if comp['class'] != 'ElmComp':
            raise FrameError(f'Not an ElmComp: {path}')
        typ = catalog.object_at(_path(a.get('typ_id')))
        ta = catalog.attrs(typ)
        frame = typ['relative_path']
        children = catalog.children.get(typ['object_id'], ())
        slots = {r['relative_path']: r for r in children if r['class'] == 'BlkSlot'}
        pblk, pelm = a.get('pblk', []), a.get('pelm', [])
        if len(pblk) != len(pelm):
            raise FrameError(f'pblk/pelm length mismatch: {path}')
        bound = {}
        for i, (s, e) in enumerate(zip(pblk, pelm)):
            sp, ep = _path(s), _path(e)
            result.bindings.append((path, i, sp, ep))
            if sp is None:
                if ep is not None:
                    raise FrameError(f'Element without slot at {path}[{i}]')
                continue
            if sp not in slots or sp in bound:
                raise FrameError(f'Unknown or repeated pblk slot: {sp}')
            bound[sp] = ep
        endpoints = {frame: (path, ta)}
        for sp, row in slots.items():
            ep = bound.get(sp)
            endpoints[sp] = (ep, catalog.attrs(row))
            if ep is None:
                continue
            obj = catalog.object_at(ep)
            if obj['class'] == 'ElmComp':
                walk(ep, stack+(path,))
            elif ep not in result.blocks:
                if ep in adapters:
                    result.blocks[ep] = adapters[ep]
                elif obj['class'] == 'ElmDsl':
                    result.blocks[ep] = DSLBlock(catalog.compile_dsl(ep))
                else:
                    raise FrameError(f'Explicit network/native adapter required: {ep}')
            if ep in result.blocks:
                block = result.blocks[ep]
                fields = catalog.attrs(row)
                for field, actual in (('sInput', block.inputs), ('sOutput', block.outputs)):
                    missing = set(names(fields.get(field))) - set(actual)
                    if missing:
                        raise FrameError(f'Slot ports absent in {ep}: {sorted(missing)}')
        # Input macros are context aliases, not empty DSL definitions.
        for row in children:
            if row['class'] != 'BlkRef':
                continue
            ra = catalog.attrs(row)
            ref = catalog.object_at(_path(ra.get('typ_id')))
            fa = catalog.attrs(ref)
            if fa.get('loc_name') != 'Input':
                raise FrameError(f'Unsupported frame macro: {row["relative_path"]}')
            binding = names(ra.get('sParams') or ra.get('sOutput'))
            ports = names(fa.get('sOutput'))
            if len(binding) != len(ports):
                raise FrameError(f'Input macro context binding missing: {row["relative_path"]}')
            key = path+'::'+row['relative_path']
            endpoints[row['relative_path']] = (key, fa)
            for formal, actual in zip(ports, binding):
                if actual not in names(ta.get('sInput')):
                    raise FrameError(f'Unknown Input context port {actual}')
                result.links.append(((path, 'in', actual), (key, 'out', formal)))
        def endpoint(attrs, suffix):
            node = _path(attrs.get('pnod'+suffix))
            if node not in endpoints:
                raise FrameError(f'Unknown BlkSig endpoint {node}')
            key, fields = endpoints[node]
            icon, index = attrs['icon'+suffix], attrs['inod'+suffix]
            if icon not in (1, 2) or isinstance(index, bool) or int(index) != index or index < 0:
                raise FrameError(f'Invalid BlkSig icon/inod at {node}')
            if key is None:
                raise FrameError(f'Connected null pelm slot: {node}')
            groups = port_groups(fields.get('sInput' if icon == 1 else 'sOutput'))
            if index >= len(groups):
                raise FrameError(f'Port group out of range at {node}: {index}')
            return [(key, 'in' if icon == 1 else 'out', n) for n in groups[int(index)]]
        for row in children:
            if row['class'] != 'BlkSig':
                continue
            attrs = catalog.attrs(row)
            if attrs.get('indep') == 1 and attrs.get('inodfrom') == attrs.get('inodto') == -1:
                continue
            src, dst = endpoint(attrs, 'from'), endpoint(attrs, 'to')
            if len(src) != len(dst):
                raise FrameError(f'Grouped port width mismatch: {row["relative_path"]}')
            def is_source(port):
                key, side, _ = port
                return (side == 'in') if key == path else (side == 'out')
            if not is_source(src[0]) and is_source(dst[0]):
                src, dst = dst, src
            if not is_source(src[0]) or is_source(dst[0]):
                raise FrameError(f'BlkSig must join a source and sink: {row["relative_path"]}')
            result.links.extend(zip(src, dst))
        return names(ta.get('sInput')), names(ta.get('sOutput'))
    result.inputs, result.outputs = walk(composite_path, ())
    result._allocate()
    return result

