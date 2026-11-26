"""Boundary constraints, signs, bases, jumps and native IEEE39 replay."""
from types import SimpleNamespace
import numpy as np
import pytest
from power_machine.playback import BoundarySeries, fixed_voltage_loadflow, run
from power_machine.api import RmsOptions, RmsResult
from power_machine.dynamics.network import DynamicNetwork
from power_machine.dynamics.solver import DAESolver, DAEState
from power_machine.dynamics.events import Event


def passive(base=100., kv=20.):
    model = {'meta': {'s_base_mva': base, 'f_nom_hz': 60.},
             'buses': [{'id': 'b', 'name': 'b', 'class': 'ElmTerm', 'un_kv': kv, 'outserv': 0}],
             'loads': [{'id': 'l', 'name': 'l', 'class': 'ElmLod', 'bus': 'b', 'p_mw': 10., 'q_mvar': 3., 'params': {'scale0': 1., 'plini': 10., 'qlini': 3.}}],
             'gens': [], 'branches': [], 'branches3': [], 'switches': [], 'shunts': []}
    net = DynamicNetwork(model, {'b': 1+0j})
    solver = DAESolver(net, [], linear_solver='numpy')
    return SimpleNamespace(solver=solver, initial=DAEState(0., np.array([]), np.array([]), np.array([1+0j])))


@pytest.mark.parametrize('base,kv', [(100., 20.), (23., 66.)])
def test_constant_source_signs_and_bases(base, kv):
    system = passive(base, kv)
    point = fixed_voltage_loadflow(system.solver.network.model, {'b': 1+0j})
    assert point['bus_voltages']['b'] == 1+0j
    result = run(system, {'b': BoundarySeries([0, .02], 'nominal', phasors=[1, 1])}, step_s=.01)
    assert isinstance(result, RmsResult)
    for row in result.samples.records:
        assert row['boundary.b.P_MW'] == pytest.approx(-10.)
        assert row['boundary.b.Q_Mvar'] == pytest.approx(-3.)
        assert row['boundary.b.I_kA'] == pytest.approx(abs(10+3j)/(np.sqrt(3)*kv))


def test_wrap_and_jump_exact_landing():
    s = BoundarySeries([0, .015, .015, .04], 'nominal', u_pu=[1, 1, .8, .8], angle_deg=[179, -179, 20, 20], discontinuities=(.015,))
    assert abs(np.angle(s.at(.0075), deg=True)) == pytest.approx(180)
    assert s.at(.015, 'left') != s.at(.015)
    # Begin at the declared initial phasor.
    system = passive()
    system.initial.v[:] = s.at(0)
    result = run(system, {'b': s}, step_s=.01)
    jump = result.summary['discontinuities'][0]
    before, after = [result.samples.records[jump[k]] for k in ('before_index', 'after_index')]
    assert before['time_s'] == after['time_s'] == .015
    assert before['bus.b.U_pu'] == pytest.approx(1.)
    assert after['bus.b.U_pu'] == pytest.approx(.8)
    assert after['boundary.b.P_MW'] == pytest.approx(-6.4)


@pytest.mark.parametrize('kwargs', [
    {'times': [0, 1], 'u_pu': [1, np.nan], 'angle_deg': [0, 0]},
    {'times': [0, 1], 'phasors': [1]},
    {'times': [0, 1, 1, 2], 'phasors': [1]*4},
    {'times': [0, 1, 2], 'phasors': [1]*3, 'discontinuities': (1,)},
    {'times': [0, 1], 'phasors': [1, 1], 'angular_reference': ''},
])
def test_reject_missing_or_ambiguous_samples(kwargs):
    with pytest.raises(ValueError):
        BoundarySeries(**{'angular_reference': 'nominal', **kwargs})


def test_no_extrapolation_and_initial_mismatch():
    s = BoundarySeries([0, .1], 'nominal', phasors=[1, 1])
    with pytest.raises(ValueError, match='extrapolation'):
        s.at(.2)
    with pytest.raises(ValueError, match='extrapolation'):
        run(passive(), {'b': s}, t_end_s=.2)
    system = passive()
    system.initial.v[:] = .9
    with pytest.raises(ValueError, match='equilibrium'):
        run(system, {'b': s})


def test_consistent_constraint_jacobian_and_event():
    system = passive()
    s = BoundarySeries([0, .03], 'nominal', phasors=[1, 1])
    result = run(system, {'b': s}, [Event(.015, 'load_factor', 'l', 2.)], step_s=.01)
    assert result.samples.records[-1]['boundary.b.P_MW'] == pytest.approx(-20.)
    solver = system.solver
    state = system.initial
    jac = solver._local_jacobian(0., state.x, state.z, state.v, method='algebraic')
    np.testing.assert_array_equal(jac, np.eye(2))
    # KCL remains physical, with no artificial large admittance.
    assert abs(solver.network.ybus[0, 0]) < 1.


@pytest.mark.pfdata
def test_ieee39_cut_replay():
    from benchmarks.playback_ieee39 import verify
    result = verify()
    assert result['initial_fixed_lf_error_pu'] < 1e-9
    assert result['errors']['P_MW'] < 1e-5
    assert result['errors']['Q_Mvar'] < 1e-5
    assert result['errors']['state'] < 1e-9
    assert result['errors']['U_pu'] < 1e-9




def test_islands_and_fused_boundaries_warn_and_reject_ambiguity():
    system = passive()
    model = system.solver.network.model
    model['buses'].append({**model['buses'][0], 'id': 'c', 'name': 'c'})
    s = BoundarySeries([0, .02], 'nominal', phasors=[1, 1])
    system.solver.network.rebuild()
    with pytest.warns(UserWarning, match='islands'):
        with pytest.raises(ValueError, match='Every playback island'):
            run(system, {'b': s})
    model['switches'] = [{'id': 'coupler', 'class': 'ElmCoup', 'bus1': 'b', 'bus2': 'c', 'closed': True}]
    system.solver.network.rebuild()
    with pytest.warns(UserWarning, match='fused together'):
        with pytest.raises(ValueError, match='fused boundaries'):
            run(system, {'b': s, 'c': s})


def test_passive_boundary_island_stays_energized_after_switching():
    system = passive()
    system.solver.network.voltage_boundary_ids = {'b'}
    system.solver.network.switching_active = True
    system.solver.network.rebuild()
    assert not system.solver.network.deenergized_bus_ids
    assert abs(system.solver.network.ybus[0, 0]) < 1

