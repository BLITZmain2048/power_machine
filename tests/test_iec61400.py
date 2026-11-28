"""Public synthetic Type 4A cases. All parameters below are illustrative."""
import numpy as np
import pytest
from power_machine.dynamics.converters import StaticGeneratorRMS
from power_machine.dynamics.iec61400 import (
    IECType4A, QControl, PControl4A, CurrentLimitation, QLimitation,
    Curve, Protection, ReferenceFrameRotation, Measurement, lag, pi)
from power_machine.dynamics.network import DynamicNetwork
from power_machine.dynamics.solver import DAESolver, DAEState


def device(**kwargs):
    return IECType4A(StaticGeneratorRMS('wt', 'synthetic/wt', 'gen', 1,
                                       50., 2, s_base_mva=200., controlled=True), **kwargs)


class Grid:
    object_id, relative_path, bus_id = 'grid', 'synthetic/grid', 'grid'
    n_states = n_alg = 0
    discrete_state = {}
    emf = 1.+0j
    def current_injection(self, x, v):
        return (self.emf-v)/(.001+.02j)
    def f(self, t, x, z, v):
        return np.empty(0)
    def g(self, t, x, z, v):
        return np.empty(0)
    def events(self):
        return ()


def setup(h, mode=2):
    # 10 kV / 200 MVA base -> 0.5 ohm. Line X=.04 pu.
    m = dict(meta=dict(s_base_mva=200., f_nom_hz=50.),
             buses=[dict(id=b, un_kv=10., outserv=0) for b in ('gen', 'grid')],
             branches=[dict(id='line', name='line', **{'class': 'ElmLne'},
                 bus_from='gen', bus_to='grid', outserv=0,
                 params=dict(R1=.0025, X1=.02, G1=0., B1=0., nlnum=1))],
             branches3=[], shunts=[], loads=[], switches=[], gens=[])
    net = DynamicNetwork(m)
    d = device(q_control=QControl(MqG=mode), measurement=Measurement(Tufilt=0))
    g = Grid()
    # Solve the constant-PQ load flow independently of the dynamic controls.
    s = .3+.025j  # network base, 60 MW + 5 Mvar
    v = 1.+0j
    for _ in range(100):
        current = (s/v).conjugate()
        v = g.emf+(.006+.06j)*current
    vg = g.emf+(.001+.02j)*current
    x = d.init(v, 60+5j)
    solver = DAESolver(net, [d, g], step_s=h, linear_solver='numpy')
    voltages = np.array([v if bid == 'gen' else vg for bid in net.node_ids])
    state = DAEState(0., x, np.empty(0), voltages)
    state, _ = solver.solve_algebraic(state)
    return d, g, solver, state


def advance(solver, state, end):
    while state.t < end-1e-12:
        d = solver.devices[0]
        v = state.v[solver.network.index_of_bus(d.bus_id)]
        d.accept_step(state.t, state.x, v)
        deadline = d.next_event_time(state.t)
        h = min(solver.step_s, end-state.t)
        if deadline is not None:
            h = min(h, deadline-state.t)
        state, _ = solver.step(state, h)
        d.accept_step(state.t, state.x, state.v[solver.network.index_of_bus(d.bus_id)])
        # Commit changes the RHS. Invalidate the cached previous derivative.
        solver._last_f = None
    return state


@pytest.mark.parametrize('mode', range(5))
def test_loadflow_equilibrium_modes_and_bases(mode):
    d, g, solver, state = setup(.01, mode)
    v = state.v[solver.network.index_of_bus('gen')]
    assert np.max(abs(d.f(0., state.x, [], v))) < 1e-8
    assert v*d.current_injection(state.x, v).conjugate()*200 == pytest.approx(60+5j)
    end = advance(solver, state.copy(), .1)
    np.testing.assert_allclose(end.x, state.x, atol=1e-9)


@pytest.mark.parametrize('event', ['voltage', 'qref', 'fault'])
def test_network_events_and_halving(event):
    results = []
    for h in (.004, .002, .001):
        d, g, solver, state = setup(h)
        before_q = d.xWTref
        if event == 'qref':
            d.xWTref += .1
        elif event == 'voltage':
            g.emf = .95+0j
        else:
            g.emf = .2+0j
        state, _ = solver.solve_algebraic(state)
        state = advance(solver, state, .12)
        v = state.v[solver.network.index_of_bus('gen')]
        y, _, _ = d.signals(state.t, state.x, v)
        assert np.isfinite(state.x).all()
        assert abs(d.current_injection(state.x, v))*2 <= 1.2+1e-10
        if event == 'qref':
            assert y['qWT'] > before_q+.03
        if event == 'fault':
            assert y['FFRT'] == 1
            assert y['qWT'] > 0
            assert state.x[14] == pytest.approx(d.pWTref)
            g.emf = 1.+0j
            state, _ = solver.solve_algebraic(state)
            state = advance(solver, state, .3)
            v = state.v[solver.network.index_of_bus('gen')]
            assert d.signals(state.t, state.x, v)[0]['FFRT'] == 0
        results.append(np.r_[state.x, state.v.real, state.v.imag])
    coarse, fine = np.max(abs(results[0]-results[1])), np.max(abs(results[1]-results[2]))
    assert fine < coarse*.85
    assert fine < .02


def test_exact_point_two_frt_current_sign_freeze_and_purity():
    d = device()
    x = d.init(1.+0j, 60+5j)
    original = dict(d.discrete_state)
    y, dx, _ = d.signals(.1, x, .2+0j)
    # Measurement lag delays detection. Apply a settled 0.2 pu measurement.
    x[4] = .2
    y, dx, _ = d.signals(.1, x, .2+0j)
    assert y['FFRT'] == 1 and y['iqcmd'] > 0
    assert dx[14] == dx[16] == dx[17] == dx[18] == dx[19] == 0
    assert d.discrete_state == original
    assert d.current_injection(x, .2+0j).imag < 0


@pytest.mark.parametrize('priority', [0, 1])
def test_current_priority_circle(priority):
    c = CurrentLimitation(Mqpri=priority)
    p, q = c.limit(1., 1., .2, 1)
    assert np.hypot(p, q) <= 1.2+1e-12
    assert (p if priority == 0 else q) == 1.


def test_qp_qu_curves_intersection_and_invalid():
    c = Curve(((0., .5), (1., .2)))
    q = QLimitation(qmaxp=c, qmaxu=Curve(((.8, .4), (1.2, .1))))
    assert q.limits(.5, 1.) == pytest.approx((-1., .25))
    assert c(-1) == .5 and c(2) == .2
    with pytest.raises(ValueError):
        Curve(((1., 0.), (0., 1.)))
    with pytest.raises(ValueError):
        QLimitation(qmin=.6, qmaxp=c).limits(.5, 1.)


def test_protection_timer_reset_and_latch():
    p = Protection(uunder=.5, delays=(.1, 1., 1., 1.))
    p.accept_step(0., .2, 1.)
    assert p.next_event_time(0.) == .1
    p.accept_step(.08, 1., 1.)
    assert p.next_event_time(.08) is None
    p.accept_step(.2, .2, 1.)
    p.accept_step(.3, .2, 1.)
    assert p.tripped
    p.accept_step(.4, 1., 1.)
    assert p.tripped
    p.reset()
    assert not p.tripped


def test_zero_filter_freeze_antiwindup_and_pll():
    assert lag(0., 2., 0., freeze=True, held=1.) == (1., 0.)
    assert lag(0., 2., 0.) == (2., 0.)
    assert pi(1., 1., 1., 2., -1., 1.) == (1., 0.)
    pll = ReferenceFrameRotation()
    angle, dx = pll.evaluate([1., 0.], 0j, .3)
    assert angle == .3 and not np.any(dx)
    assert Measurement(Tufilt=0).evaluate(np.ones(5), .2, 1, 1, 1, 1)[0][0] == .2
    d = device(q_control=QControl(Tqord=0))
    x = d.init(1.+0j, 60+5j)
    d.xWTref = .2
    d.accept_step(.01, x, 1.+0j)
    assert d.discrete_state['qord'] == .2
    x[4] = .2
    assert d.signals(.02, x, .2+0j)[1][16] == 0.


def test_invalid_initialization_and_modes():
    with pytest.raises(ValueError):
        device().init(1.+0j, 150+150j)
    with pytest.raises(ValueError):
        QControl(MqFRT=3)
    with pytest.raises(ValueError):
        device(p_control=PControl4A(Tpordp4A=0))


def test_overvoltage_sign_and_trip_gate_after_accepted_time():
    d = device(protection=Protection(uunder=.5, delays=(.02, 1, 1, 1)),
               protection_measurement=Measurement(Tufilt=0))
    x = d.init(1.+0j, 60+5j)
    x[4] = 1.3
    assert d.signals(.01, x, 1.3+0j)[0]['iqcmd'] < 0
    # A tentative evaluation must not trip. Only accepted dwell time counts.
    d.f(.1, x, [], .2+0j)
    assert not d.protection.tripped
    d.accept_step(.1, x, .2+0j)
    assert d.next_event_time(.1) == pytest.approx(.12)
    d.accept_step(.12, x, .2+0j)
    assert d.current_injection(x, 1.+0j) == 0j


def test_packaged_attribute_map_and_static_generator_composition():
    import json
    from importlib.resources import files
    data = json.loads(files('power_machine.dynamics').joinpath('iec_ref_parameters.json').read_text())
    assert len(data['parameters']) == 92
    assert all(r['status'] in ('confirmed', 'candidate') and 'value' not in r for r in data['parameters'])
    port = StaticGeneratorRMS('a', 'synthetic/a', 'bus', 1, 100., 1)
    assert port.with_iec_type4a().generator is port


@pytest.mark.parametrize('frt, expected', [(0, .9), (1, 1.0), (2, 1.0)])
def test_frt_modes_and_postfault(frt, expected):
    qc = QControl(MqFRT=frt, iqpost=.2)
    base = dict(u=1., iq=.1)
    x = np.array([0., 0., .1, 1.])
    y, _ = qc.evaluate(x, 0., .5, .6, .05, (-1, 1), 1, base, 1., 0.)
    assert y == pytest.approx(expected)
    y, _ = qc.evaluate(x, 0., 1., .6, .05, (-1, 1), 2, base, 1., 0.)
    assert y == pytest.approx(.3 if frt == 2 else (0. if frt == 0 else .1))
