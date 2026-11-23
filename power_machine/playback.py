"""Exact voltage-boundary playback for RMS subsystem identification."""
from __future__ import annotations

from dataclasses import dataclass
import copy
import math
from pathlib import Path
import time
import warnings

import numpy as np

from .api import Network, RmsOptions, RmsResult, StudyTable
from .dynamics.events import EventTimeline
from .dynamics.solver import DAEState


@dataclass(frozen=True)
class BoundarySeries:
    """Samples on a bus voltage base in one explicitly named angular frame.

    Repeat a time exactly twice for the left and right limits of a declared
    discontinuity. Angles are unwrapped separately within continuous segments.
    """
    times: object
    angular_reference: str
    u_pu: object = None
    angle_deg: object = None
    phasors: object = None
    discontinuities: tuple = ()

    def __post_init__(self):
        t = np.array(self.times, dtype=float, copy=True)
        if t.ndim != 1 or len(t) < 2 or not np.isfinite(t).all() or np.any(np.diff(t) < 0):
            raise ValueError('Finite, ordered sample times are required')
        if not isinstance(self.angular_reference, str) or not self.angular_reference.strip():
            raise ValueError('Declare a common angular reference')
        if self.phasors is not None:
            if self.u_pu is not None or self.angle_deg is not None:
                raise ValueError('Supply phasors or magnitude and angle, exclusively')
            v = np.array(self.phasors, dtype=complex, copy=True)
            u, a = abs(v), np.angle(v)
        else:
            u = np.array(self.u_pu, dtype=float, copy=True)
            a = np.deg2rad(np.array(self.angle_deg, dtype=float, copy=True))
        if u.shape != t.shape or a.shape != t.shape or not np.isfinite(u).all() or not np.isfinite(a).all() or np.any(u < 0):
            raise ValueError('Missing or invalid voltage samples')
        jumps = np.array(self.discontinuities, dtype=float)
        duplicates = t[:-1][np.diff(t) == 0]
        if (not np.isfinite(jumps).all() or len(set(jumps)) != len(jumps)
                or set(duplicates) != set(jumps) or any(np.count_nonzero(t == x) != 2 for x in jumps)
                or any(x <= t[0] or x >= t[-1] for x in jumps)):
            raise ValueError('Each declared jump requires exactly two interior samples')
        starts = [0] + (np.flatnonzero(np.diff(t) == 0)+1).tolist()
        ends = starts[1:] + [len(t)]
        for start, end in zip(starts, ends):
            a[start:end] = np.unwrap(a[start:end])
        for key, value in (('_t', t), ('_u', u), ('_a', a)):
            value.flags.writeable = False
            object.__setattr__(self, key, value)

    def at(self, t, side='right'):
        """Interpolate U and unwrapped angle without extrapolation."""
        if not math.isfinite(t) or t < self._t[0] or t > self._t[-1]:
            raise ValueError('Boundary extrapolation is forbidden')
        matches = np.flatnonzero(self._t == t)
        if len(matches):
            i = matches[0] if side == 'left' else matches[-1]
            return self._u[i]*np.exp(1j*self._a[i])
        j = int(np.searchsorted(self._t, t))
        f = (t-self._t[j-1])/(self._t[j]-self._t[j-1])
        return ((1-f)*self._u[j-1]+f*self._u[j])*np.exp(1j*((1-f)*self._a[j-1]+f*self._a[j]))


def fixed_voltage_loadflow(model, fixed, *, tolerance=1e-10):
    """Solve PQ/PV power balance with multiple exact fixed-phasor buses.

    Generator Q limits are not applied. Conflicting PV controls at one node
    are rejected. Return an explicit operating point for RMS initialization.
    """
    from . import elements
    from .dynamics.network import DynamicNetwork
    buses = {b['id']: b for b in model['buses']}
    seed = {bid: fixed.get(bid, 1+0j) for bid in buses}
    passive = copy.deepcopy(model)
    passive['loads'] = []
    net = DynamicNetwork(passive, seed)
    _check_topology(type('LoadflowView', (), {'network': net})(), fixed)
    n = len(net.node_ids)
    fixed_idx = {net.index_of_bus(b): v for b, v in fixed.items()}
    p = np.zeros(n, complex)
    loads = np.zeros(n, complex)
    for load in model['loads']:
        if not load.get('outserv'):
            loads[net.index_of_bus(load['bus'])] += complex(*elements.load_pq_mw(load))
    gens = {}
    pv = {}
    for gen in model['gens']:
        if gen.get('outserv'):
            continue
        i = net.index_of_bus(gen['bus'])
        s = complex(*elements.gen_pq_mw(gen))
        gens.setdefault(i, []).append((gen, s))
        p[i] += s
        if i not in fixed_idx and (gen.get('mode') == 'constv' or gen.get('ref')):
            value = float(gen.get('v_set_pu') or 1.)
            if i in pv and abs(pv[i]-value) > 1e-10:
                raise ValueError('Conflicting PV controls')
            pv[i] = value
    p = (p-loads)/net.s_base_mva
    v = np.ones(n, complex)
    for i, value in fixed_idx.items():
        v[i] = value
    def residual(w):
        v = w[:n]+1j*w[n:]
        power = v*np.conj(net.ybus@v)
        r = power-p
        for i, value in pv.items():
            r[i] = complex(r[i].real, abs(v[i])**2-value**2)
        for i, value in fixed_idx.items():
            r[i] = v[i]-value
        return np.r_[r.real, r.imag]
    w = np.r_[v.real, v.imag]
    for _ in range(50):
        r = residual(w)
        if max(abs(r), default=0) < tolerance:
            break
        jac = np.column_stack([(residual(w+np.eye(1, 2*n, j)[0]*1e-6)-residual(w-np.eye(1, 2*n, j)[0]*1e-6))/2e-6 for j in range(2*n)])
        delta = np.linalg.solve(jac, -r)
        alpha = 1.
        while alpha > 1/1024 and np.max(abs(residual(w+alpha*delta))) > np.max(abs(r)):
            alpha /= 2
        w += alpha*delta
    else:
        raise ValueError('Fixed-voltage load flow did not converge')
    v = w[:n]+1j*w[n:]
    powers = {}
    total = v*np.conj(net.ybus@v)*net.s_base_mva+loads
    for i, rows in gens.items():
        # Fixed boundaries supply the imbalance. Local generators retain P,Q.
        if i in pv:
            qextra = (total[i].imag-sum(s.imag for _, s in rows))/len(rows)
        else:
            qextra = 0.
        for gen, s in rows:
            powers[gen['id']] = s+1j*qextra
    return {'bus_voltages': net.bus_voltages(v), 'generator_powers': powers}


def _check_topology(solver, series):
    net = solver.network
    indexes = [net.index_of_bus(bid) for bid in series]
    if len(set(indexes)) != len(indexes):
        warnings.warn('Boundary buses are fused together', UserWarning)
        raise ValueError('Independent boundary currents cannot be recovered on fused boundaries')
    for bid, i in zip(series, indexes):
        members = [b for b in net.bus_to_node if net.index_of_bus(b) == i]
        if len(members) > 1:
            warnings.warn(f'Boundary {bid} is fused with {members}', UserWarning)
    if len(net.topology['islands']) > 1:
        warnings.warn('Boundary network contains islands', UserWarning)
    for island in net.topology['islands']:
        if not set(island['buses']).intersection(series):
            raise ValueError('Every playback island needs a prescribed boundary')


def run(network, boundary_series, events=None, options=None, *, step_s=.001, t_end_s=None):
    """Run playback on an API Network or an assembled solver/initial system.

    An assembled system supplies .solver and .initial (or .state). Its initial
    operating point must already be the fixed-boundary subsystem load flow.
    An assembled solver is owned by this run and must not be shared concurrently.
    """
    options = options or RmsOptions()
    if options.t_start_s not in (None, 0.):
        raise ValueError('Playback initialization is at t=0')
    if not boundary_series or not math.isfinite(step_s) or step_s <= 0 or options.output_every < 1:
        raise ValueError('Nonempty boundaries, positive step and output cadence required')
    series = {bid: value if isinstance(value, BoundarySeries) else BoundarySeries(**value)
              for bid, value in boundary_series.items()}
    if len({s.angular_reference for s in series.values()}) != 1:
        raise ValueError('All boundaries must declare the same angular reference')
    end = min(s._t[-1] for s in series.values()) if t_end_s is None else float(t_end_s)
    if not math.isfinite(end) or end <= 0:
        raise ValueError('Positive finite end time required')
    for s in series.values():
        s.at(0.)
        s.at(end)
    fixed = {bid: s.at(0.) for bid, s in series.items()}
    if isinstance(network, Network):
        from .dynamics.runner import RMSRunner
        model = network.to_model()
        point = fixed_voltage_loadflow(model, fixed)
        system = RMSRunner(network._source_dir or Path('.'), model=model, operating_point=point,
                           tstart=0., dt_s=step_s, events=[], monitored=[],
                           boundary_voltages=fixed, linear_solver=options.linear_solver)
    else:
        system = network
    solver = system.solver
    state = getattr(system, 'initial', getattr(system, 'state', None)).copy()
    if state.t != 0.:
        raise ValueError('Initial operating point must be at t=0')
    solver.step_s = step_s
    solver.linear_solver = options.linear_solver
    solver.retry_steps = options.retry_steps
    solver.min_step_s = options.min_step_s
    solver.max_step_retries = options.max_step_retries
    solver._step_factor = solver._step_key = None
    solver._last_state = solver._previous_state = solver._last_f = None
    solver.network.voltage_boundary_ids = set(series)
    _check_topology(solver, series)
    side = ['right']
    solver.voltage_provider = lambda t: {bid: s.at(t, side[0]) for bid, s in series.items()}
    f, g, kcl = solver._evaluate(0., state.x, state.z, state.v)
    if max(np.max(abs(f), initial=0), np.max(abs(g), initial=0), np.max(abs(kcl), initial=0)) > 1e-7:
        raise ValueError('Initial state does not match the fixed-boundary load flow equilibrium')
    schedule = EventTimeline(events or []).events
    if any(e.time_s > end for e in schedule):
        raise ValueError('Event is outside the playback interval')
    jumps = {float(t) for s in series.values() for t in s.discontinuities if t <= end}
    stops = sorted(jumps | {e.time_s for e in schedule} | {end})
    records, discontinuities = [], []
    steps = 0
    started = time.perf_counter()
    def record():
        row = {'time_s': float(state.t)}
        current = solver.boundary_currents(state)
        for bid in series:
            i = solver.network.index_of_bus(bid)
            s = state.v[i]*current[i].conjugate()*solver.network.s_base_mva
            base = next(b['un_kv'] for b in solver.network.model['buses'] if b['id'] == bid)
            row.update({f'boundary.{bid}.P_MW': float(s.real), f'boundary.{bid}.Q_Mvar': float(s.imag),
                        f'boundary.{bid}.I_kA': float(abs(current[i])*solver.network.s_base_mva/(math.sqrt(3)*base))})
        for bid, v in solver.network.bus_voltages(state.v).items():
            row[f'bus.{bid}.U_pu'] = float(abs(v))
            row[f'bus.{bid}.angle_deg'] = float(np.angle(v, deg=True))
        for dev, xs, zs in zip(solver.devices, solver.x_slices, solver.z_slices):
            for j, x in enumerate(state.x[xs]):
                row[f'state.{dev.object_id}.{j}'] = float(x)
            if not callable(getattr(dev, 'current_injections_multi', None)):
                v = state.v[solver.network.index_of_bus(dev.bus_id)]
                current_dev = (dev.current_injection(state.x[xs], v)
                               if solver.in_service[dev.object_id] else 0j)
                power_dev = v*np.conj(current_dev)*solver.network.s_base_mva
                row[f'device.{dev.object_id}.P_MW'] = float(power_dev.real)
                row[f'device.{dev.object_id}.Q_Mvar'] = float(power_dev.imag)
        records.append(row)
    record()
    for stop in stops:
        side[0] = 'left'
        while state.t < stop-1e-12:
            state, _ = solver.step(state, min(step_s, stop-state.t))
            if abs(state.t-stop) < 1e-12:
                state.t = stop
            steps += 1
            accept = getattr(system, '_accept_controls', None)
            if accept is not None and accept(state):
                state, _ = solver.solve_algebraic(state)
                solver.restart_discrete()
            if steps % options.output_every == 0 or state.t == stop:
                record()
        at_events = [e for e in schedule if e.time_s == stop]
        if stop in jumps or at_events:
            old_v = solver.network.bus_voltages(state.v)
            for event in at_events:
                if event.kind == 'gen_trip':
                    solver.trip_generator(event.target_id)
                else:
                    EventTimeline._apply(solver.network, event)
            solver.refresh_network_indexes()
            _check_topology(solver, series)
            side[0] = 'right'
            trial = DAEState(stop, state.x.copy(), state.z.copy(), solver.network.voltage_vector(old_v))
            state, _ = solver.solve_algebraic(trial)
            solver.restart_discrete()
            discontinuities.append({'time_s': stop, 'before_index': len(records)-1, 'after_index': len(records)})
            record()
    elapsed = time.perf_counter()-started
    summary = {'steps': steps, 'wall_s': elapsed, 'ms_per_step': elapsed*1000/max(steps, 1),
               'discontinuities': discontinuities, 'angular_reference': next(iter(series.values())).angular_reference,
               'sign': 'positive export from subsystem', 'step_retries': solver.step_retries}
    result = RmsResult(status='completed', samples=StudyTable('samples', tuple(records)), summary=summary)
    if options.output is not None:
        destination = Path(options.output)
        destination.mkdir(parents=True, exist_ok=True)
        import csv
        import json
        with (destination/'signals.csv').open('w', newline='', encoding='utf-8') as stream:
            writer = csv.DictWriter(stream, fieldnames=list(records[0]))
            writer.writeheader()
            writer.writerows(records)
        (destination/'run_report.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return result


