"""Synthetic REF-shaped graphs. No plant exports or private parameters."""
import numpy as np
import pytest
from power_machine.dynamics.dsl_compile import DynamicCatalog, DSLCompileError
from power_machine.dynamics.frames import FrameError, NativeBlock, NetworkBlock, port_groups
from power_machine.dynamics.pss2b import PSS2B
from power_machine.dynamics.iec61400 import Measurement


class Synthetic:
    def __init__(self):
        self.cat = object.__new__(DynamicCatalog)
        self.cat.objects, self.cat.attributes, self.cat.children = {}, {}, {}
        self.counter = 0

    def obj(self, path, kind, parent='', **attrs):
        row = dict(object_id=path, relative_path=path, **{'class': kind})
        self.cat.objects[path] = row
        self.cat.attributes[path] = attrs
        self.cat.children.setdefault(parent, []).append(row)
        return {'relative_path': path}

    def frame(self, path, inputs='', outputs=''):
        return self.obj(path, 'BlkDef', sInput=inputs or [], sOutput=outputs or [])

    def slot(self, frame, name, inputs='', outputs=''):
        return self.obj(frame+'/'+name, 'BlkSlot', frame, sInput=inputs or [], sOutput=outputs or [])

    def comp(self, name, frame, slots, elements):
        return self.obj(name, 'ElmComp', typ_id={'relative_path': frame}, pblk=slots, pelm=elements)

    def dsl(self, name, inputs, outputs, states, equations):
        typ = self.obj(name+'Type', 'BlkDef', sInput=inputs, sOutput=outputs,
                       sStates=states, sAddEquat=equations)
        return self.obj(name, 'ElmDsl', typ_id=typ, parnam=[], params=[])

    def wire(self, frame, src, dst, si=0, ti=0, sc=2, tc=1):
        self.counter += 1
        self.obj(frame+'/w'+str(self.counter), 'BlkSig', frame,
                 pnodfrom=src, pnodto=dst, inodfrom=si, inodto=ti, iconfrom=sc, iconto=tc)


@pytest.mark.parametrize('dynamic_machine', [False, True])
def test_avr_pss_infinite_bus_backward_and_response(dynamic_machine):
    s = Synthetic()
    s.frame('F', 'vref', 'efd')
    machine = s.obj('machine', 'ElmSym')
    pss = s.obj('pss', 'ElmDsl')  # native implementation explicitly supplied
    avr = s.dsl('avr', 'ut,vs,vref', 'efd', 'x',
                ['efd=x', 'x.=(10*(vref-ut+vs)-x)/0.1', 'inc(x)=efd', 'inc(vref)=ut+efd/10-vs'])
    ms = s.slot('F', 'machine', 've', 'ut,speed,P')
    ps = s.slot('F', 'pss', 'Vs1,Vs2', 'Vst')
    av = s.slot('F', 'avr', 'ut,vs,vref', 'efd')
    s.comp('C', 'F', [ms, ps, av], [machine, pss, avr])
    s.wire('F', ms, av)
    s.wire('F', ms, ps, 1, 0)
    s.wire('F', ms, ps, 2, 1)
    s.wire('F', ps, av, 0, 1)
    s.wire('F', {'relative_path':'F'}, av, 0, 2, 1)
    s.wire('F', av, ms)
    s.wire('F', av, {'relative_path':'F'}, 0, 0, 2, 2)
    obs = {'ut':1., 'speed':0., 'P':.8}
    net = NetworkBlock('ElmSym', lambda t: obs, {'ve':1.2, **obs})
    if dynamic_machine:
        from power_machine.dynamics.synchronous import SynchronousMachine
        typ = dict(iturbo=0, xd=1.8, xq=1.7, xl=.15, xds=.3,
                   xdss=.2, xqss=.25, xrl=0., xrlq=0., tds0=5.,
                   tdss0=.04, tqss0=.06, tag=8., cosn=.9, rstr=.002,
                   dpu=0., ugn=13.8, sgn=100., isat=0)
        gen = dict(id='synthetic', path='synthetic', bus='infinite',
                   name='synthetic', type_params=typ, params={})
        model = SynchronousMachine(gen, bus_un_kv=13.8)
        net = NativeBlock.synchronous(model, lambda t: 1.+0j, 80.+10j)
    # Explicit base conversion: this adapter's speed channel is deviation pu.
    native = NativeBlock.pss2b(PSS2B())
    # Slot names represent REF names, explicit mapping at the network boundary.
    f = s.cat.compile_frame('C', adapters={'machine':net, 'pss':native})
    x, u = f.initialize({})
    y, dx = f.evaluate(0., x, u)
    initial_efd = y['efd']
    if not dynamic_machine:
        assert initial_efd == pytest.approx(1.2)
    assert max(abs(dx)) < 1e-10
    if not dynamic_machine:
        assert f.commands()['machine']['ve'] == pytest.approx(1.2)
    u['vref'] += .05
    _, dx = f.evaluate(1., x, u)
    assert dx[-1] == pytest.approx(5.)
    # Independent RK4 step refinement, including rotor dynamics and PSS feedback.
    initial = x.copy()
    solutions = []
    for h in (.001, .0005):
        x = initial.copy()
        for i in range(round(.2/h)):
            t = 1+i*h
            k1 = f.evaluate(t, x, u)[1]
            k2 = f.evaluate(t+h/2, x+h*k1/2, u)[1]
            k3 = f.evaluate(t+h/2, x+h*k2/2, u)[1]
            k4 = f.evaluate(t+h, x+h*k3, u)[1]
            x += h*(k1+2*k2+2*k3+k4)/6
        solutions.append(x)
    np.testing.assert_allclose(*solutions, atol=2e-7, rtol=2e-7)
    y = f.evaluate(1.2, x, u)[0]['efd']
    assert np.isfinite(x).all()
    assert y > initial_efd
    if not dynamic_machine:
        assert y == pytest.approx(initial_efd+.5*(1-np.exp(-2)), abs=1e-8)


def test_nested_ppc_context_input_and_grouped_ports():
    s = Synthetic()
    s.frame('inner', 'pref;qref', 'p;q')
    ctrl = s.dsl('ctrl', 'pref,qref', 'p,q', [], ['p=pref', 'q=qref'])
    cs = s.slot('inner', 'control', 'pref;qref', 'p;q')
    s.obj('InputType', 'BlkDef', loc_name='Input', sOutput='y;z')
    macro = s.obj('inner/input', 'BlkRef', 'inner', typ_id={'relative_path':'InputType'}, sParams='pref;qref')
    s.wire('inner', macro, cs)
    s.wire('inner', cs, {'relative_path':'inner'}, tc=2)
    child = s.comp('ppc', 'inner', [cs], [ctrl])
    s.frame('outer', 'pref;qref', 'p;q')
    ps = s.slot('outer', 'ppc', 'pref;qref', 'p;q')
    gs = s.slot('outer', 'generator', 'Pext;Qext', 'ut')
    gen = s.obj('gen', 'ElmGenstat')
    s.comp('plant', 'outer', [ps, gs], [child, gen])
    s.wire('outer', ps, gs)
    s.wire('outer', {'relative_path':'outer'}, ps, sc=1)
    s.wire('outer', ps, {'relative_path':'outer'}, tc=2)
    net = NetworkBlock('ElmGenstat', lambda t: {'ut':1.}, {'Pext':.7, 'Qext':-.2, 'ut':1.})
    f = s.cat.compile_frame('plant', adapters={'gen':net})
    assert f.evaluate(0., np.empty(0), {'pref':.7, 'qref':-.2})[0] == {'p':.7, 'q':-.2}
    assert f.commands()['gen'] == {'Pext':.7, 'Qext':-.2}
    assert port_groups('a;b,c') == (('a','b'),('c',))


def test_algebraic_loop_diagnostic_and_null_duplicate_pelm():
    s = Synthetic()
    s.frame('F')
    a = s.dsl('a', 'u', 'y', [], ['y=2*u'])
    b = s.dsl('b', 'u', 'y', [], ['y=u+1'])
    aa = s.slot('F','a','u','y')
    bb = s.slot('F','b','u','y')
    unused = s.slot('F','unused')
    alias = s.slot('F','alias','u','y')
    s.comp('C','F',[aa,bb,unused,alias,None],[a,b,None,a,None])
    s.wire('F', aa, bb)
    s.wire('F', bb, aa)
    f = s.cat.compile_frame('C')
    assert len(f.bindings) == 5
    assert len(f.blocks) == 2
    with pytest.raises(FrameError, match='Algebraic loop.*a.*b'):
        f.evaluate(0.,np.empty(0),{})
    s.wire('F', unused, aa)
    with pytest.raises(FrameError, match='null pelm'):
        s.cat.compile_frame('C')


def test_optional_curves_and_required_mode():
    s = Synthetic()
    typ = s.obj('T','BlkDef',sParams='oarray_a',sInput='mode,u',sOutput='y',
                sAddEquat=['y=select(mode,lapprox(u,oarray_a),u)'])
    s.obj('D','ElmDsl',typ_id=typ,parnam=[],params=[])
    d = s.cat.compile_dsl('D')
    assert d.evaluate(0.,np.empty(0),{'mode':0.,'u':.5})[0]['y'] == .5
    s.cat.attributes['D']['matrix'] = [[2,0],[0,1],[1,3]]
    d = s.cat.compile_dsl('D')
    assert d.evaluate(0.,np.empty(0),{'mode':1.,'u':.5})[0]['y'] == 2.
    s.cat.attributes['D']['matrix'] = [[0,0]]
    assert s.cat.curve_map(s.cat.objects['D'],s.cat.objects['T']) == {}
    b = s.cat.compile_dsl('D')
    assert b.evaluate(0.,np.empty(0),{'mode':0., 'u':.3})[0]['y'] == .3
    with pytest.raises(DSLCompileError,match='Required curve missing.*oarray_a'):
        b.evaluate(0.,np.empty(0),{'mode':1., 'u':.3})


def test_native_iec_measurement_slot():
    s = Synthetic()
    s.frame('F','u,i,p,q,f','u,i,p,q,f')
    m = s.obj('measurement','ElmDsl')
    slot = s.slot('F','measurement','u,i,p,q,f','u,i,p,q,f')
    s.comp('C','F',[slot],[m])
    for i in range(5):
        s.wire('F',{'relative_path':'F'},slot,i,i,1,1)
        s.wire('F',slot,{'relative_path':'F'},i,i,2,2)
    f = s.cat.compile_frame('C',adapters={'measurement':NativeBlock.measurement(Measurement())})
    u = {'u':1.,'i':.8,'p':.8,'q':0.,'f':1.}
    x, _ = f.initialize({('C','in',k):v for k,v in u.items()})
    assert max(abs(f.evaluate(0.,x,u)[1])) == 0.
    u['u'] += .05
    assert f.evaluate(1.,x,u)[1][0] == pytest.approx(2.5)


@pytest.mark.parametrize('kind,ports', [('StaVmea', ('U','ur','ui')),
                                     ('StaPqmea', ('P','Q')), ('StaFrqmea', ('f',))])
def test_network_measurement_boundaries(kind, ports):
    s = Synthetic()
    s.frame('F', outputs=','.join(ports))
    obj = s.obj('meter', kind)
    slot = s.slot('F','meter',outputs=','.join(ports))
    s.comp('C','F',[slot],[obj])
    expected = {n:float(i+1) for i,n in enumerate(ports)}
    for i in range(len(ports)):
        # Reversed graphical segment, roles determined from icon.
        s.wire('F',{'relative_path':'F'},slot,i,i,2,2)
    f = s.cat.compile_frame('C',adapters={'meter':NetworkBlock(kind,lambda t:expected,expected)})
    assert f.evaluate(0.,np.empty(0),{})[0] == expected


def test_alias_state_is_allocated_once_and_conflicting_drivers_fail():
    s = Synthetic()
    s.frame('F','u','y')
    obj = s.dsl('lag','u','y','x',['y=x','x.=u-x','inc(x)=y'])
    a = s.slot('F','a','u','y')
    b = s.slot('F','b','u','y')
    s.comp('C','F',[a,b],[obj,obj])
    s.wire('F',{'relative_path':'F'},a,sc=1)
    s.wire('F',b,{'relative_path':'F'},tc=2)
    f = s.cat.compile_frame('C')
    assert f.n_states == 1
    x,u = f.initialize({('C','in','u'):.4,('C','out','y'):.4})
    assert f.evaluate(0.,x,u)[0]['y'] == .4
    assert f.evaluate(0.,x,{'u':.5})[1][0] == pytest.approx(.1)
    s.wire('F',b,a)
    with pytest.raises(FrameError,match='Multiple signal drivers'):
        s.cat.compile_frame('C')


def test_graphical_dsl_state_feedback_is_not_an_algebraic_loop():
    s = Synthetic()
    s.frame('F', outputs='y')
    s.obj('lagType','BlkDef',sInput='u',sOutput='y',sStates='x',
          sAddEquat=['y=x','x.=u-x','inc(x)=y'])
    s.obj('graph','BlkDef',sInput='u',sOutput='y',sStates='X')
    ref = s.obj('graph/ref','BlkRef','graph',typ_id={'relative_path':'lagType'},sStates='X',sParams=[])
    s.wire('graph',{'relative_path':'graph'},ref,sc=1)
    s.wire('graph',ref,{'relative_path':'graph'},tc=2)
    obj = s.obj('lag','ElmDsl',typ_id={'relative_path':'graph'},parnam=[],params=[])
    slot = s.slot('F','lag','u','y')
    s.comp('C','F',[slot],[obj])
    s.wire('F',slot,slot)
    s.wire('F',slot,{'relative_path':'F'},tc=2)
    f = s.cat.compile_frame('C')
    x,_ = f.initialize({('C','out','y'):.4})
    y,dx = f.evaluate(0.,x,{})
    assert y == {'y':.4}
    assert dx[0] == 0.


def test_absent_optional_curve_fails_only_when_selected():
    s = Synthetic()
    t = s.obj('T','BlkDef',sParams='oarray_a',sInput='mode,u',sOutput='y',
              sAddEquat=['y=select(mode,lapprox(u,oarray_a),u)'])
    s.obj('D','ElmDsl',typ_id=t,parnam=[],params=[])
    b = s.cat.compile_dsl('D')
    assert b.evaluate(0.,np.empty(0),{'mode':0.,'u':.4})[0]['y'] == .4
    with pytest.raises(DSLCompileError,match='Required curve missing.*oarray_a'):
        b.evaluate(0.,np.empty(0),{'mode':1.,'u':.4})
