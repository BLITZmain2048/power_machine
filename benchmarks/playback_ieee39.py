"""Native IEEE 39 cut replay, with no reference simulator runtime."""
import copy
from types import SimpleNamespace
import numpy as np
from power_machine.benchmarks.ieee39_ref import build_rms_case
from power_machine.dynamics.events import Event, EventTimeline
from power_machine.dynamics.network import DynamicNetwork
from power_machine.dynamics.ieee39_native import NativeIEEESolver
from power_machine.dynamics.solver import DAEState
from power_machine.playback import BoundarySeries, fixed_voltage_loadflow, run
from power_machine.loadflow import _stamp


def verify():
    full = build_rms_case(step_s=.01, stage='loads')
    by_name = {b['name']: b['id'] for b in full.model['buses']}
    interior = {by_name[f'Bus {i:02}'] for i in (6, 7, 8, 10, 11, 31, 32)}
    boundary = {by_name[f'Bus {i:02}'] for i in (5, 9, 12, 13)}
    keep = interior | boundary
    events = [Event(.02, 'fault_apply', by_name['Bus 16'], 0j),
              Event(.06, 'fault_clear', by_name['Bus 16'])]
    trajectory = EventTimeline(events).run(full.solver, full.initial, .10)
    model = copy.deepcopy(full.network.model)
    model['buses'] = [b for b in model['buses'] if b['id'] in keep]
    retained = [b for b in model['branches'] if b['bus_from'] in keep and b['bus_to'] in keep
                and (b['bus_from'] in interior or b['bus_to'] in interior)]
    model['branches'] = retained
    model['branches3'] = []
    model['switches'] = []
    for group in ('gens', 'loads', 'shunts'):
        model[group] = [g for g in model[group] if g['bus'] in interior]
    voltage = full.network.bus_voltages(full.initial.v)
    lf_model = copy.deepcopy(model)
    lf_model['loads'] = [copy.deepcopy(d) for d in full.model['loads'] if d['bus'] in interior]
    for load in lf_model['loads']:
        load['outserv'] = 0
    for gen in lf_model['gens']:
        power = full.initial_values[gen['id']]
        gen['params']['pgini'] = power.real/(gen['params']['ngnum']*gen['params'].get('scale0', 1.))
        gen['v_set_pu'] = abs(voltage[gen['bus']])
    point = fixed_voltage_loadflow(lf_model, {b: voltage[b] for b in boundary})
    lf_error = max(abs(point['bus_voltages'][b]-voltage[b]) for b in keep)
    net = DynamicNetwork(model, {b: voltage[b] for b in keep})
    selected = [(d, xs, zs) for d, xs, zs in zip(full.devices, full.solver.x_slices, full.solver.z_slices)
                if d.bus_id in interior]
    devices = [d for d, _, _ in selected]
    solver = NativeIEEESolver(net, devices, step_s=.01, atol=1e-11, rtol=1e-9)
    for d, xs in zip(devices, solver.x_slices):
        if hasattr(d, 'machine') and d.machine.elm['ip_ctrl'] == 1:
            solver.reference_speed_index = xs.start+d.machine.n_states-2
    initial = DAEState(0., np.concatenate([full.initial.x[xs] for _, xs, _ in selected]),
                       np.concatenate([full.initial.z[zs] for _, _, zs in selected]),
                       net.voltage_vector(voltage))
    series = {bid: BoundarySeries([s.t for s in trajectory.samples], 'nominal 60 Hz common frame',
                                 phasors=[s.v[full.network.index_of_bus(bid)] for s in trajectory.samples],
                                 discontinuities=(.02, .06)) for bid in boundary}
    result = run(SimpleNamespace(solver=solver, initial=initial), series, step_s=.01)
    errors = {'P_MW': 0., 'Q_Mvar': 0., 'state': 0., 'U_pu': 0.}
    assert len(result.samples.records) == len(trajectory.samples), ([r['time_s'] for r in result.samples.records], [s.t for s in trajectory.samples])
    buses = {b['id']: b for b in full.model['buses']}
    for row, state in zip(result.samples.records, trajectory.samples):
        assert abs(row['time_s']-state.t) < 1e-12
        expected = {b: 0j for b in boundary}
        for branch in retained:
            ids, _, matrix = _stamp(branch, buses, net.s_base_mva)
            v = np.array([state.v[full.network.index_of_bus(b)] for b in ids])
            currents = matrix@v
            for b, vb, current in zip(ids, v, currents):
                if b in boundary:
                    expected[b] -= vb*current.conjugate()*net.s_base_mva
        for bid, value in expected.items():
            errors['P_MW'] = max(errors['P_MW'], abs(row[f'boundary.{bid}.P_MW']-value.real))
            errors['Q_Mvar'] = max(errors['Q_Mvar'], abs(row[f'boundary.{bid}.Q_Mvar']-value.imag))
        for d, xs, _ in selected:
            for j, value in enumerate(state.x[xs]):
                errors['state'] = max(errors['state'], abs(row[f'state.{d.object_id}.{j}']-value))
        for b in keep:
            errors['U_pu'] = max(errors['U_pu'], abs(row[f'bus.{b}.U_pu']-abs(state.v[full.network.index_of_bus(b)])))
    return {'errors': errors, 'timing': result.summary, 'cut': [buses[b]['name'] for b in sorted(boundary)],
            'interior': [buses[b]['name'] for b in sorted(interior)], 'states': solver.nx,
            'initial_fixed_lf_error_pu': lf_error}

if __name__ == '__main__':
    import json
    print(json.dumps(verify(), indent=2))



