'Plant support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import cmath
import csv
import json
import math
from pathlib import Path

import numpy as np

from .converters import StaticGeneratorRMS
from .dsl_compile import DSLCompileError, _path, _names
from .measurements import PhaseLockedLoop, VoltageMeasurement, PeriodicClock, SampleHold


class MultiportPlant:
    def __init__(self, root, model, catalog, generators, *, configuration=None):
        configuration = configuration or model.get("dynamic_plant")
        if not configuration:
            raise _coded_error(ValueError, 'missing_plant_data', 'Missing case dynamic_plant configuration')
        self.configuration = configuration
        self.catalog = catalog
        self.model = model
        self.s_base_mva = float(model['meta']['s_base_mva'])
        self.generators = [StaticGeneratorRMS.from_model(g, controlled=True,
                           s_base_mva=self.s_base_mva) for g in generators]
        self.gen_index = {g.relative_path: i for i, g in enumerate(self.generators)}
        self.buses = {b['path']: b for b in model['buses']}
        self.items = {e['path']: e for group in ('gens', 'branches', 'branches3')
                      for e in model[group]}
        self.rows = {p: r for p, r in catalog.objects.items()
                     if p in configuration['object_paths']
                     and not int(catalog.attrs(r).get('outserv', 0))}
        comp = next(r for r in self.rows.values() if r['relative_path'] == configuration['composite_path'])
        self.object_id, self.relative_path = comp['object_id'], comp['relative_path']
        self.bus_id = self.generators[0].bus_id
        self.discrete_state = {}
        self.blocks = {p: catalog.compile_dsl(p) for p, r in self.rows.items()
                       if r['class'] == 'ElmDsl'}
        self.connections = {}
        self.ignored_wires = []
        for p, r in self.rows.items():
            if r['class'] != 'ElmComp':
                continue
            a = catalog.attrs(r)
            typ = catalog.object_at(_path(a['typ_id']))
            slots = {_path(s): _path(e) for s, e in zip(a['pblk'], a['pelm'])}
            slots[typ['relative_path']] = p
            for row in catalog.children.get(typ['object_id'], ()):
                if row['class'] != 'BlkSig':
                    continue
                w = catalog.attrs(row)
                src, dst = slots.get(_path(w.get('pnodfrom'))), slots.get(_path(w.get('pnodto')))
                if src is None or dst is None:
                    continue
                for sn, dn in zip(_names(w['GetFromSigName()']), _names(w['GetToSigName()'])):
                    if dst in self.blocks and dn not in self.blocks[dst].ir.inputs:
                        self.ignored_wires.append(row['relative_path'])
                        continue
                    self.connections[dst, dn] = src, sn
        # Alias de marcos sin salida real: REF deja inc0 en el receptor.
        def resolve(key, seen=()):
            if key in seen:
                raise _coded_error(DSLCompileError, 'invalid_plant_data', f'ElmComp alias cycle: {key}')
            if key in self.connections:
                return resolve(self.connections[key], (*seen, key))
            return key
        self.connections = {k: resolve(v) for k, v in self.connections.items()}
        for key, source in tuple(self.connections.items()):
            if source[0] in self.rows and self.rows[source[0]]['class'] == 'ElmComp':
                self.ignored_wires.append(': missing output from the actual frame')
                del self.connections[key]

        self.plls, self.vmeas, self.vt, self.pq = {}, {}, {}, {}
        self.slices, cursor = {}, 0
        for p, b in self.blocks.items():
            self.slices[p] = slice(cursor, cursor+b.n_states)
            cursor += b.n_states
        read_buses, read_branches = set(), set()
        cubic = {}
        for e in self.items.values():
            for name, value in e['params'].items():
                if not isinstance(value, dict):
                    continue
                ref = value.get('reference', {})
                if ref.get('class') == 'StaCubic':
                    path = ref['path'][ref['path'].index('Network Model.'):]
                    cubic[path] = e, name
        portable = Path(configuration['portable_csv_dir'])
        vt_types = {}
        with (portable / 'objects.csv').open(encoding='utf-8-sig', newline='') as f:
            type_ids = {r['object_id']: r['relative_path'] for r in csv.DictReader(f)
                        if r['class'] == 'TypVt'}
        with (portable / 'parameters.csv').open(encoding='utf-8-sig', newline='') as f:
            for r in csv.DictReader(f):
                if r['object_id'] in type_ids and r['status'] == 'PRESENT':
                    vt_types.setdefault(type_ids[r['object_id']], {})[r['attribute']] = json.loads(r['value_json'])
        self.native_provenance = str(portable)
        for p, row in self.rows.items():
            a, cls = catalog.attrs(row), row['class']
            if cls in ('ElmPhi__pll', 'StaVmea', 'StaVt'):
                buspath = _path(a['pbusbar'])
                if buspath.endswith('.StaCubic'):
                    buspath = buspath.rsplit('\\', 1)[0]
                bus = self.buses[buspath]
                read_buses.add(bus['id'])
                if cls == 'ElmPhi__pll':
                    obj = PhaseLockedLoop.from_params(row['object_id'], p, a)
                    self.plls[p] = obj, bus['id']
                    count = obj.n_states
                elif cls == 'StaVmea':
                    obj = VoltageMeasurement(row['object_id'], p)
                    mode = int(a['i_mode'])
                    scale = {0: bus['un_kv']/math.sqrt(3), 1: 1.,
                             2: bus['un_kv']/float(a['Unom'])}[mode]
                    self.vmeas[p] = obj, bus['id'], scale, int(a['freqOutput'])
                    count = 2
                else:
                    typ = vt_types[_path(a['typ_id'])]
                    if typ['iopt_mod'] != 0 or a['stapcon'] != 'Y' or a['secvts']:
                        raise _coded_error(DSLCompileError, 'invalid_plant_data', f'StaVt must be ideal/simple: {p}')
                    scale = bus['un_kv']*1000/math.sqrt(3)*a['stapset']/a['ptapset']
                    self.vt[p] = bus['id'], scale
                    count = 0
                self.slices[p] = slice(cursor, cursor+count)
                cursor += count
            elif cls == 'StaPqmea':
                cub = _path(a['pcubic'])
                e, port = cubic[cub]
                mode = int(a['i_mode'])
                base = (1. if mode == 0 else float(a['Snom']) if mode == 2
                        else float(e['s_rated_mva']))
                orientation = int(a['i_orient'])
                if e['class'] == 'ElmGenstat':
                    source = ('gen', e['path'], 0)
                else:
                    end = {'bus1': 0, 'bus2': 1, 'bushv': 0, 'busmv': 1, 'buslv': 2}[port]
                    source = ('branch', e['id'], end)
                    read_branches.add(e['id'])
                self.pq[p] = source, base, orientation
        self.n_states, self.n_alg = cursor, 2*len(self.generators)
        self.equilibrium_fixed_state_indexes = tuple(
            self.slices[p].start+i for p, b in self.blocks.items()
            for i, name in enumerate(b.state_names)
            if name.startswith(('flag', 'x1t', 'x2t', 'x3t', 'xft')))
        self.read_bus_ids = tuple(sorted(read_buses | {g.bus_id for g in self.generators}))
        self.read_branch_ids = tuple(sorted(read_branches))
        self.injection_bus_ids = tuple(dict.fromkeys(g.bus_id for g in self.generators))
        clock_path = configuration['clock_path']
        self.clock = PeriodicClock.from_ppc_dsl(catalog, clock_path)
        self.holds = {p: None for p, r in self.rows.items() if r['class'] == 'ElmSamp'}
        self.samplers = {p: SampleHold(self.rows[p]['object_id'], p, self.clock)
                         for p in self.holds}
        self.defaults = {}
        self._cache = None
        self._t = 0.
        self._sample_version = 0
        self._execution_order = None
        self._last_sample_time = None
        self._input_sources = {p: {n: self.connections.get((p, n), (p, n))
                                  for n in b.ir.inputs} for p, b in self.blocks.items()}

    def _propagate(self, values, reverse=False):
        for _ in range(len(self.connections)+1):
            changed = False
            for target, source in self.connections.items():
                if source in values and target not in values:
                    values[target] = values[source]
                    changed = True
                elif reverse and target in values and source not in values:
                    values[source] = values[target]
                    changed = True
            if not changed:
                break

    def _currents(self, x, z, ports):
        result = {}
        for g in self.generators:
            i = self.gen_index[g.relative_path]
            cp = self.connections[g.relative_path, 'cosref'][0]
            pll, bid = self.plls[cp]
            angle = pll.outputs(x[self.slices[cp]], ports.bus_voltage_pu[bid])['phi_rad']
            result[g.relative_path] = complex(z[2*i], z[2*i+1])*cmath.exp(1j*angle)*g.equipment_base_mva/self.s_base_mva
        return result

    def _native(self, x, z, ports):
        values, derivatives = {}, {}
        currents = self._currents(x, z, ports)
        for p, (pll, bid) in self.plls.items():
            v = ports.bus_voltage_pu[bid]
            out = pll.outputs(x[self.slices[p]], v)
            derivatives[p] = pll.f(x[self.slices[p]], v)
            for name, value in {'fmeas': out['f_meas_pu'], 'Fmeas': out['f_meas_hz'],
                    'cosphi': out['cosphi'], 'sinphi': out['sinphi'],
                    'cosref': out['cosphi'], 'sinref': out['sinphi']}.items():
                values[p, name] = value
        for p, (obj, bid, scale, _always_calculate) in self.vmeas.items():
            v = ports.bus_voltage_pu[bid]
            out = obj.outputs(x[self.slices[p]], v)
            derivatives[p] = obj.f(x[self.slices[p]], v)
            for name, value in {'u': out['u_pu']*scale, 'ur': v.real*scale,
                               'ui': v.imag*scale,
                               # freqOutput selecciona cálculo conectado/siempre,
                               # no unidades. TechRef StaVmea §3.1: fe en p.u.
                               'fe': out['fe_pu']}.items():
                values[p, name] = value
        for p, (bid, scale) in self.vt.items():
            v = ports.bus_voltage_pu[bid]*scale
            for phase, angle in zip('ABC', (0., 2*math.pi/3, 4*math.pi/3)):
                phasor = v*cmath.exp(1j*angle)
                values[p, 'U2r_'+phase], values[p, 'U2i_'+phase] = phasor.real, phasor.imag
        for p, (source, base, orientation) in self.pq.items():
            kind, identifier, end = source
            if kind == 'gen':
                g = self.generators[self.gen_index[identifier]]
                power = ports.bus_voltage_pu[g.bus_id]*currents[identifier].conjugate()*self.s_base_mva
                if orientation == 0:
                    power = -power
            else:
                power = ports.branch_power_pu[identifier, end]*self.s_base_mva
                if orientation == 1:
                    power = -power
            values[p, 'p'], values[p, 'q'] = power.real/base, power.imag/base
        return values, derivatives, currents

    def initialize_ports(self, lf_v, powers, ports):
        x = np.full(self.n_states, np.nan)
        z = np.empty(self.n_alg)
        for i, g in enumerate(self.generators):
            g.init(lf_v[g.bus_id], powers[g.object_id])
            z[2*i:2*i+2] = g._id_ref, g._iq_ref
        for p, (pll, bid) in self.plls.items():
            x[self.slices[p]] = pll.init(lf_v[bid])
        for p, (vm, bid, _, _) in self.vmeas.items():
            x[self.slices[p]] = vm.init(lf_v[bid])
        values, _, _ = self._native(x, z, ports)
        for i, g in enumerate(self.generators):
            values[g.relative_path, 'id_ref'] = z[2*i]
            values[g.relative_path, 'iq_ref'] = z[2*i+1]
        self._propagate(values, True)
        # Se inicia desde las salidas conocidas de los convertidores, luego
        # los esclavos y finalmente el maestro, con las mediciones LF fijas.
        order = sorted(self.blocks, key=lambda p: (p == self.configuration['master_path'],
                       p in self.configuration.get('slave_paths', ()), p))
        master_ready = False
        for _ in range(30):
            before = len(values)
            for p in self.holds:
                if (p, 'input') in values:
                    values.setdefault((p, 'output'), values[p, 'input'])
                elif (p, 'output') in values:
                    values[p, 'input'] = values[p, 'output']
            self._propagate(values, True)
            for p in order:
                b = self.blocks[p]
                if p == self.configuration['master_path'] and not master_ready:
                    continue
                known = {name: value for (path, name), value in values.items() if path == p}
                if (any((p, name) in self.connections and name not in known for name in b.ir.inputs)
                        and not any(name in known for name in b.ir.outputs)):
                    continue
                try:
                    bx, inputs = b.initialize(known, _partial=True)
                except (ValueError, KeyError) as exc:
                    raise _coded_error(DSLCompileError, 'invalid_plant_data', f'{p}: {exc}. Known={known}') from exc
                x[self.slices[p]] = bx
                for name in (*b.ir.inputs, *b.ir.outputs):
                    if name in b.initial_scope:
                        values.setdefault((p, name), b.initial_scope[name])
                if np.isfinite(bx).all() and set(b.ir.inputs) <= set(inputs):
                    output, _ = b.evaluate(0., bx, inputs)
                    for name, value in output.items():
                        values.setdefault((p, name), value)
                self._propagate(values, True)
            if len(values) == before:
                if master_ready:
                    break
                master_ready = True
        self.initial_values = values
        self.defaults = {p: {name: values[p, name] for name in b.ir.inputs
                              if (p, name) in values and (p, name) not in self.connections}
                         for p, b in self.blocks.items()}
        for p in self.holds:
            if (p, 'input') not in values:
                raise _coded_error(DSLCompileError, 'invalid_plant_data', f'SampleHold has no load flow: {p}')
            self.holds[p] = values[p, 'input']
            source, signal = self.connections[p, 'cl']
            # El reloj conserva su inc incluso si h(tstart) tiene otro valor.
            clock_initial = (self.blocks[source].initial_outputs()[signal]
                             if source in self.blocks else values[source, signal])
            self.samplers[p].init(self.holds[p], clock_value=clock_initial)
        missing = [(p, b.state_names[i]) for p, b in self.blocks.items()
                   for i, value in enumerate(x[self.slices[p]]) if not np.isfinite(value)]
        if missing:
            raise _coded_error(DSLCompileError, 'missing_plant_data', f'Unresolved PPC inc: {missing}')
        self.initial_z = z
        self._evaluate(0., x, z, ports)
        return x

    def _evaluate(self, t, x, z, ports, *, accept=False):
        key = (t, x.tobytes(), z.tobytes(), tuple(ports.bus_voltage_pu.values()),
               tuple(ports.branch_power_pu.values()), self._sample_version)
        if not accept and self._cache is not None and key == self._cache[0]:
            if len(self._cache) > 2:
                for p, values in self._cache[2].items():
                    self.blocks[p].memory.switch_values.update(values)
            return self._cache[1]
        values, derivatives, currents = self._native(x, z, ports)
        for p, value in self.holds.items():
            values[p, 'output'] = value
        remaining = set(self.blocks)
        execution_order = []
        for _ in range(len(remaining)+1):
            done = []
            for p in (self._execution_order or sorted(remaining)):
                if p not in remaining:
                    continue
                b = self.blocks[p]
                inputs = {**self.defaults[p], **{name: values[source]
                         for name, source in self._input_sources[p].items() if source in values}}
                if set(b.ir.inputs) - set(inputs):
                    continue
                out, dx = (b.accept_step(t, x[self.slices[p]], inputs) if accept
                           else b.evaluate(t, x[self.slices[p]], inputs))
                derivatives[p] = dx
                for name, value in out.items():
                    values[p, name] = value
                done.append(p)
                execution_order.append(p)
            remaining.difference_update(done)
            if not remaining or not done:
                break
        self._propagate(values)
        if remaining:
            raise _coded_error(DSLCompileError, 'invalid_plant_data', f'Blocked PPC inputs: {[(p, [n for n in self.blocks[p].ir.inputs if (p, n) not in values and n not in self.defaults[p]]) for p in remaining]}')
        if self._execution_order is None:
            self._execution_order = tuple(execution_order)
        dx = np.zeros(self.n_states)
        for p, derivative in derivatives.items():
            dx[self.slices[p]] = derivative
        g = np.empty(self.n_alg)
        for i, gen in enumerate(self.generators):
            g[2*i:2*i+2] = z[2*i:2*i+2] - [values[gen.relative_path, 'id_ref'],
                                                     values[gen.relative_path, 'iq_ref']]
        result = dx, g, currents, values
        if not accept:
            self._cache = key, result, {
                p: b.memory.switch_values.copy() for p, b in self.blocks.items()
                if b.memory.switch_watch}
        return result

    def f_multi(self, t, x, z, ports):
        self._t = t
        return self._evaluate(t, x, z, ports)[0]

    def g_multi(self, t, x, z, ports):
        return self._evaluate(t, x, z, ports)[1]

    def current_injections_multi(self, x, z, ports):
        currents = self._currents(x, z, ports)
        result = dict.fromkeys(self.injection_bus_ids, 0j)
        for g in self.generators:
            result[g.bus_id] += currents[g.relative_path]
        return result

    def accept_step_multi(self, t, x, ports, z=None, *, initializing=False):
        if z is None:
            z = self.initial_z
        result = self._evaluate(t, x, z, ports, accept=True)
        captured = []
        if not initializing:
            # Se evalúan todas las entradas con las muestras previas. Solo
            # después se publican las capturas, también en tstart.
            for p, sampler in self.samplers.items():
                source = self.connections[p, 'cl']
                if sampler.accept_clock(result[3][source], result[3][p, 'input']):
                    captured.append(p)
        if captured:
            self.holds.update({p: self.samplers[p].output() for p in captured})
            self._sample_version += 1
            self._last_sample_time = t
        self._cache = None
        return bool(captured)

    def next_event_time(self, after_s):
        edge = self.clock.phase_s + (math.floor((after_s-self.clock.phase_s+1e-10)/self.clock.period_s)+1)*self.clock.period_s
        times = [b.next_event_time(after_s) for b in self.blocks.values()]
        return min([edge, *(t for t in times if t is not None)])

    def events(self):
        return ()

    def jacobian_dependencies(self, *, dynamic, bus_columns, branch_buses):
        'Conservative block dependencies for grouped finite differences. Perturb columns together only when they share no affected output row.'
        offset = self.n_states if dynamic else 0
        rows = [set() for _ in range(offset+self.n_alg+2*len(self.injection_bus_ids))]
        values = {}

        def states(path):
            sl = self.slices[path]
            return set(range(sl.start, sl.stop)) if dynamic else set()

        def voltage(bid):
            return set(bus_columns[bid])

        def publish(path, names, columns):
            for name in names:
                values[path, name] = set(columns)

        def derivative(path, columns):
            if dynamic:
                sl = self.slices[path]
                for i in range(sl.start, sl.stop):
                    rows[i] = set(columns) | {i}

        current = {}
        for gen in self.generators:
            i = self.gen_index[gen.relative_path]
            pll_path = self.connections[gen.relative_path, 'cosref'][0]
            current[gen.relative_path] = (states(pll_path) | voltage(gen.bus_id)
                                         | {offset+2*i, offset+2*i+1})
        for path, (_, bid) in self.plls.items():
            columns = states(path) | voltage(bid)
            derivative(path, columns)
            publish(path, ('fmeas', 'Fmeas', 'cosphi', 'sinphi', 'cosref', 'sinref'), columns)
        for path, (_, bid, _, _) in self.vmeas.items():
            columns = states(path) | voltage(bid)
            derivative(path, columns)
            publish(path, ('u', 'ur', 'ui', 'fe'), columns)
        for path, (bid, _) in self.vt.items():
            publish(path, tuple('U2'+part+'_'+phase for part in ('r', 'i') for phase in 'ABC'),
                    voltage(bid))
        for path, (source, _, _) in self.pq.items():
            kind, identifier, _ = source
            columns = (current[identifier] if kind == 'gen' else
                       set().union(*(voltage(bid) for bid in branch_buses[identifier])))
            publish(path, ('p', 'q'), columns)
        for path in self.holds:
            publish(path, ('output',), set())
        for path in self._execution_order:
            block = self.blocks[path]
            columns = states(path)
            for name, source in self._input_sources[path].items():
                if name in self.defaults[path]:
                    continue
                if source not in values:
                    raise _coded_error(DSLCompileError, 'missing_plant_data', f'Missing multiport dependency: {source}')
                columns |= values[source]
            derivative(path, columns)
            publish(path, block.ir.outputs, columns)
        for i, gen in enumerate(self.generators):
            for j, name in enumerate(('id_ref', 'iq_ref')):
                source = self.connections[gen.relative_path, name]
                rows[offset+2*i+j] = values[source] | {offset+2*i+j}
            bus_index = self.injection_bus_ids.index(gen.bus_id)
            rows[offset+self.n_alg+bus_index] |= current[gen.relative_path]
            rows[offset+self.n_alg+len(self.injection_bus_ids)+bus_index] |= current[gen.relative_path]
        return rows
