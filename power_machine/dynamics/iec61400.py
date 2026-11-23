"""IEC 61400-27-1:2020 (edition 2) Type 4A RMS building blocks.

Engineering implementation candidate. References identify the intended module,
not a certification of diagram or vendor parity. See docs/iec_61400_27_1.md.
All P,Q and currents use equipment bases and positive generation. Thus
I_network=(ip-j*iq)*exp(j*theta)*S_equipment/S_network.
Only accepted steps commit FRT, freeze and protection memories.
"""
from dataclasses import dataclass
import cmath
import math
import numpy as np


def clip(x, lo, hi):
    if lo > hi:
        raise ValueError('Empty limit intersection')
    return float(min(max(x, lo), hi))


def lag(x, u, T, lo=-math.inf, hi=math.inf,
        down=-math.inf, up=math.inf, freeze=False, held=None):
    """IEC 2020 D.5, Figures D.5-D.7, bounded lag and freeze.

    T*x'=u-x with rate and absolute limits. T=0 is an algebraic
    bypass, except that freeze returns the last accepted output.
    """
    if freeze:
        return (x if T else held), 0.
    target = clip(u, lo, hi)
    if T == 0:
        if math.isfinite(down) or math.isfinite(up):
            raise ValueError('Finite rate with T=0 requires a separate ramp state')
        return target, 0.
    d = clip((target-x)/T, down, up)
    if (x <= lo and d < 0) or (x >= hi and d > 0):
        d = 0.
    return clip(x, lo, hi), d


def pi(x, error, kp, ki, lo, hi, freeze=False):
    """IEC 2020 D.9, Figures D.12-D.13, conditional anti-windup PI."""
    raw = x+kp*error
    d = ki*error
    if freeze or (raw >= hi and d > 0) or (raw <= lo and d < 0):
        d = 0.
    return clip(raw, lo, hi), d


@dataclass(frozen=True)
class Curve:
    """IEC 2020 D.6, Figure D.8, linear table with held endpoints.

    Points must be explicit. An absent PF oarray is not inferred as a curve.
    """
    points: tuple

    def __post_init__(self):
        a = np.asarray(self.points, dtype=float)
        if (a.ndim != 2 or a.shape[1] != 2 or len(a) < 2
                or not np.isfinite(a).all() or np.any(np.diff(a[:, 0]) <= 0)):
            raise ValueError('Curve requires finite, increasing abscissas')

    def __call__(self, u):
        a = np.asarray(self.points)
        return float(np.interp(u, a[:, 0], a[:, 1]))


@dataclass(frozen=True)
class QLimitation:
    """IEC 2020 7.7.7-7.7.8, Figures 36-37, constant or QP/QU limits."""
    qmin: float = -1.
    qmax: float = 1.
    qminp: Curve | None = None
    qmaxp: Curve | None = None
    qminu: Curve | None = None
    qmaxu: Curve | None = None

    def limits(self, pWT, uWT):
        lo = max([self.qmin]+[c(v) for c, v in
                 ((self.qminp, pWT), (self.qminu, uWT)) if c is not None])
        hi = min([self.qmax]+[c(v) for c, v in
                 ((self.qmaxp, pWT), (self.qmaxu, uWT)) if c is not None])
        if lo > hi:
            raise ValueError('Incompatible QP/QU capability')
        return lo, hi


@dataclass(frozen=True)
class CurrentLimitation:
    """IEC 2020 7.7.6, Figure 35, Type 4 total converter current.

    ip**2+iq**2 <= imax**2. Mqpri applies during FRT. Type 3 stator
    subtraction is excluded. Optional voltage tables are explicit.
    """
    imax: float = 1.1
    imaxdip: float = 1.2
    Mqpri: int = 1
    MDFSLim: int = 1
    ipmaxuWT: Curve | None = None
    iqmaxuWT: Curve | None = None
    Kpqu: float = 0.
    upqumax: float = 1.2

    def __post_init__(self):
        if (not np.isfinite([self.imax, self.imaxdip, self.Kpqu, self.upqumax]).all()
                or self.imax <= 0 or self.imaxdip <= 0 or self.Mqpri not in (0, 1)
                or self.MDFSLim != 1):
            raise ValueError('Invalid current limit or priority')

    def limit(self, ipcmd, iqcmd, uWT, FFRT):
        radius = self.imaxdip if FFRT == 1 else self.imax
        plim = min(radius, self.ipmaxuWT(uWT) if self.ipmaxuWT else radius)
        qlim = min(radius, self.iqmaxuWT(uWT) if self.iqmaxuWT else radius)
        if min(plim, qlim) < 0:
            raise ValueError('Current table must be nonnegative')
        qlo, qhi = -qlim, qlim
        if self.Kpqu:
            qhi = min(qhi, self.Kpqu*(self.upqumax-uWT))
        qhi = max(qlo, qhi)
        if FFRT and self.Mqpri:
            q = clip(iqcmd, qlo, qhi)
            p = clip(ipcmd, 0., min(plim, math.sqrt(max(0., radius**2-q*q))))
        else:
            p = clip(ipcmd, 0., plim)
            residual = math.sqrt(max(0., radius**2-p*p))
            q = clip(iqcmd, max(qlo, -residual), max(-residual, min(qhi, residual)))
        return p, q


@dataclass(frozen=True)
class Measurement:
    """IEC 2020 7.8.2, Figure 39, independent control/protection filters.

    Frequency f is an explicit per-unit input from the network observer.
    No nominal-frequency substitution is made for missing frequency data.
    """
    Tufilt: float = .02
    Tifilt: float = .02
    Tpfilt: float = .02
    Tqfilt: float = .02
    Tffilt: float = .02

    def evaluate(self, x, u, i, p, q, f):
        values = [lag(a, b, T) for a, b, T in zip(x, (u, i, p, q, f), vars(self).values())]
        return np.array([v[0] for v in values]), np.array([v[1] for v in values])


@dataclass(frozen=True)
class ReferenceFrameRotation:
    """IEC 2020 7.4.5, Figure 25, rectangular voltage PLL filter.

    Above uPLL1 use the voltage directly, between thresholds filter its
    rectangular components, below uPLL2 hold the last accepted angle.
    """
    TPLL: float = .02
    uPLL1: float = .9
    uPLL2: float = .1

    def evaluate(self, x, u, held_angle):
        if abs(u) <= self.uPLL2:
            return held_angle, np.zeros(2)
        _, dr = lag(x[0], u.real, self.TPLL)
        _, di = lag(x[1], u.imag, self.TPLL)
        z = u if abs(u) > self.uPLL1 or self.TPLL == 0 else complex(*x)
        return cmath.phase(z) if abs(z) else held_angle, np.array([dr, di])


@dataclass(frozen=True)
class GeneratorSetType4:
    """IEC 2020 7.4.4, Figure 24, bounded first-order current generation."""
    Tg: float = .02
    dipmax: float = 10.
    diqmin: float = -10.
    diqmax: float = 10.

    def evaluate(self, x, ipcmd, iqcmd):
        return np.array([clip((ipcmd-x[0])/self.Tg, -math.inf, self.dipmax),
                         clip((iqcmd-x[1])/self.Tg, self.diqmin, self.diqmax)])


@dataclass(frozen=True)
class PControl4A:
    """IEC 2020 7.7.3, Figure 32, reference and order filters.

    Two bounded lags, freeze of the reference during voltage dip,
    voltage scaling selected by MpUscale. Diagram audit remains pending.
    """
    TpWTref4A: float = .05
    Tpordp4A: float = .05
    dprefmin4A: float = -1.
    dprefmax4A: float = 1.
    dpmaxp4A: float = 2.
    updip: float = .85
    MpUscale: int = 1

    def evaluate(self, x, pWTref, uWT, held):
        pref, dref = lag(x[0], pWTref, self.TpWTref4A,
                         down=self.dprefmin4A, up=self.dprefmax4A,
                         freeze=uWT <= self.updip, held=held)
        order, dord = lag(x[1], pref, self.Tpordp4A, down=-math.inf, up=self.dpmaxp4A)
        return order/(max(uWT, .01) if self.MpUscale else 1.), np.array([dref, dord])


@dataclass(frozen=True)
class QControl:
    """IEC 2020 7.7.5, Figure 34, Tables 28-31, cascaded Q/U and FRT.

    MqG=0 voltage, 1 Q, 2 open Q, 3 power factor, 4 open power factor.
    Power factor command is tan(phi), positive for injected reactive power.
    MqFRT=0 voltage contribution, 1 pre-fault plus contribution,
    2 same during fault, pre-fault plus iqpost after fault. User mode 3
    requires a user-defined controller and is rejected by this class.
    """
    MqG: int = 2
    MqFRT: int = 1
    KPq: float = .1
    KIq: float = 1.
    KPu: float = 1.
    KIu: float = 2.
    KPuFRT: float = 1.
    Kqv: float = 2.
    deltaudb1: float = -.05
    deltaudb2: float = .05
    uqdip: float = .85
    uqrise: float = 1.15
    Tpost: float = .1
    Tqord: float = .02
    Tuss: float = .1
    iqpost: float = 0.
    iqmin: float = -1.2
    iqmax: float = 1.2
    iqh1: float = 1.2
    umin: float = .8
    umax: float = 1.2
    rdrop: float = 0.
    xdrop: float = 0.

    def __post_init__(self):
        if self.MqG not in range(5) or self.MqFRT not in range(3):
            raise ValueError('Unsupported Q or user-defined FRT mode')
        if not (self.deltaudb1 <= 0 <= self.deltaudb2 and self.iqmin <= self.iqmax
                and self.umin < self.umax and self.uqdip < self.uqrise):
            raise ValueError('Invalid Q control limits')

    def fault(self, u):
        return u <= self.uqdip or u >= self.uqrise

    def evaluate(self, x, xWTref, u, p, q, qlimits, FFRT, base, uref0, held):
        qref = xWTref*p if self.MqG in (3, 4) else xWTref
        qref = clip(qref, *qlimits)
        qord, dqord = lag(x[0], qref, self.Tqord, freeze=FFRT != 0, held=held)
        du = math.hypot(u-(self.rdrop*p+self.xdrop*q)/max(u, .01),
                        (self.xdrop*p-self.rdrop*q)/max(u, .01))
        ur, dxq = pi(x[1], qord-q, self.KPq, self.KIq,
                     self.umin-uref0, self.umax-uref0, FFRT != 0)
        target = uref0+xWTref if self.MqG == 0 else uref0+ur
        iq, dxu = pi(x[2], target-du, self.KPuFRT if FFRT else self.KPu,
                     self.KIu, self.iqmin, self.iqmax, FFRT != 0)
        if self.MqG in (2, 4):
            iq, dxq, dxu = qord/max(u, .01), 0., 0.
        elif self.MqG == 0:
            dxq, dqord = 0., 0.
        uss, duss = lag(x[3], u, self.Tuss, freeze=FFRT != 0, held=base['u'])
        if FFRT:
            delta = u-uss
            dead = delta-self.deltaudb1 if delta < self.deltaudb1 else (
                delta-self.deltaudb2 if delta > self.deltaudb2 else 0.)
            extra = -self.Kqv*dead
            iq = extra if self.MqFRT == 0 else base['iq']+extra
            if FFRT == 2 and self.MqFRT == 2:
                iq = base['iq']+self.iqpost
            iq = clip(iq, self.iqmin, self.iqh1)
        return clip(iq, self.iqmin, self.iqmax), np.array([dqord, dxq, dxu, duss])


class Protection:
    """IEC 2020 7.8.1, Figure 38, voltage/frequency duration curves.

    Four independent definite or curve-time relays. Each excursion resets
    upon return to its permitted region. Trip is latched until explicit init.
    Curve ordinate is seconds and frequency abscissa is per unit.
    """
    def __init__(self, uunder=0., uover=2., funder=0., fover=2.,
                 delays=(1., 1., 1., 1.)):
        self.thresholds = (uunder, uover, funder, fover)
        self.delays = tuple(delays)
        if len(self.delays) != 4 or not uunder < uover or not funder < fover:
            raise ValueError('Invalid protection settings')
        for d in self.delays:
            if not callable(d) and (not math.isfinite(d) or d < 0):
                raise ValueError('Invalid protection duration')
        self.reset()

    def reset(self):
        self.starts = [None]*4
        self.tripped = False
        self.deadlines = [None]*4

    def accept_step(self, t, u, f):
        values = (u, u, f, f)
        for i, (v, threshold, duration) in enumerate(zip(values, self.thresholds, self.delays)):
            active = v <= threshold if i % 2 == 0 else v >= threshold
            self.starts[i] = (t if self.starts[i] is None else self.starts[i]) if active else None
            seconds = duration(v) if callable(duration) else duration
            if seconds < 0:
                raise ValueError('Negative protection curve duration')
            self.deadlines[i] = None if not active else self.starts[i]+seconds
            if active and t+1e-12 >= self.deadlines[i]:
                self.tripped = True

    def next_event_time(self, after_s):
        return min((d for d in self.deadlines if d is not None and d > after_s+1e-12), default=None)


class IECType4A:
    """Current-source DynamicDevice composed with StaticGeneratorRMS.

    IEC 2020 6.2.5.2 Figures 8-9. init takes MW+jMvar from load flow.
    Parameters are explicit module objects. No automatic DLL replacement.
    n_states=20: current(2), PLL(2), control measurement(5), protection
    measurement(5), P control(2), Q control(4). Network solves KCL.
    """
    n_states = 20
    n_alg = 0

    def __init__(self, generator, *, p_control=None, q_control=None, current=None,
                 q_limit=None, pll=None, measurement=None, protection_measurement=None,
                 generator_set=None, protection=None):
        if generator.mode != 1 or generator.angle_delay:
            raise ValueError('IEC Type4A requires mode 1 without a second angle delay')
        self.generator = generator
        self.object_id, self.relative_path, self.bus_id = (
            generator.object_id, generator.relative_path, generator.bus_id)
        self.pc, self.qc = p_control or PControl4A(), q_control or QControl()
        self.limiter, self.ql = current or CurrentLimitation(), q_limit or QLimitation()
        self.pll = pll or ReferenceFrameRotation()
        self.meas = measurement or Measurement()
        self.protmeas = protection_measurement or Measurement()
        self.gs, self.protection = generator_set or GeneratorSetType4(), protection or Protection()
        for module in (self.pc, self.qc, self.pll, self.meas, self.protmeas, self.gs):
            for k, v in vars(module).items():
                if isinstance(v, (int, float)) and not math.isfinite(v):
                    raise ValueError('Nonfinite IEC parameter: '+k)
                if k.startswith('T') and v < 0:
                    raise ValueError('Negative time constant: '+k)
        if self.gs.Tg <= 0 or self.pc.TpWTref4A <= 0 or self.pc.Tpordp4A <= 0:
            raise ValueError('Current and rate-limited P lags require positive T')
        if (self.pc.MpUscale not in (0, 1) or self.pc.dprefmin4A > 0
                or min(self.pc.dprefmax4A, self.pc.dpmaxp4A, self.gs.dipmax, self.gs.diqmax) < 0
                or self.gs.diqmin > 0 or not 0 <= self.pll.uPLL2 <= self.pll.uPLL1):
            raise ValueError('Invalid selector, ramp or PLL thresholds')
        self.discrete_state = {}
        self.frequency_pu = 1.

    def set_reference_frequency(self, value_pu):
        if not math.isfinite(value_pu):
            raise ValueError('Frequency must be finite')
        self.frequency_pu = value_pu

    def init(self, v_bus, i_or_s_from_lf):
        self.generator.init(v_bus, i_or_s_from_lf)
        s = complex(i_or_s_from_lf)/self.generator.equipment_base_mva
        u = abs(v_bus)
        ip, iq = s.real/u, s.imag/u
        if self.qc.fault(u) or self.protection.thresholds[0] >= u or self.protection.thresholds[1] <= u:
            raise ValueError('Load flow must be outside FRT/protection region')
        if not self.ql.limits(s.real, u)[0] <= s.imag <= self.ql.limits(s.real, u)[1]:
            raise ValueError('Load flow outside Q capability')
        limited = self.limiter.limit(ip, iq, u, 0)
        if np.max(abs(np.array(limited)-[ip, iq])) > 1e-10:
            raise ValueError('Load flow outside current capability')
        self.pWTref = s.real if self.pc.MpUscale else ip
        self.xWTref = (s.imag/s.real if s.real else 0.) if self.qc.MqG in (3, 4) else (
            0. if self.qc.MqG == 0 else s.imag)
        self.uref0 = math.hypot(u-(self.qc.rdrop*s.real+self.qc.xdrop*s.imag)/u,
                               (self.qc.xdrop*s.real-self.qc.rdrop*s.imag)/u)
        self.protection.reset()
        self.discrete_state = dict(fault=False, post_until=-math.inf, angle=cmath.phase(v_bus),
                                   u=u, iq=iq, qord=s.imag, pref=self.pWTref, time=0.)
        measurements = [u, abs(s)/u, s.real, s.imag, self.frequency_pu]
        x = np.array([ip, iq, v_bus.real, v_bus.imag, *measurements, *measurements,
                      self.pWTref, self.pWTref, s.imag, 0., iq, u])
        if max(abs(self.f(0., x, np.empty(0), v_bus))) > 1e-8:
            raise ValueError('No IEC equilibrium with supplied mode and limits')
        return x

    def _current(self, x, v):
        theta, _ = self.pll.evaluate(x[2:4], v, self.discrete_state['angle'])
        # Hard physical envelope also applies during changes of FRT radius.
        ip, iq = self.limiter.limit(x[0], x[1], abs(v), int(self.qc.fault(abs(v))))
        if self.protection.tripped:
            ip = iq = 0.
        return complex(ip, -iq)*cmath.exp(1j*theta)

    def current_injection(self, x, v):
        return self._current(x, v)*self.generator.equipment_base_mva/self.generator.s_base_mva

    def signals(self, t, x, v):
        i = self._current(x, v)
        power = v*i.conjugate()
        measured, dm = self.meas.evaluate(x[4:9], abs(v), abs(i), power.real, power.imag, self.frequency_pu)
        protected, dp = self.protmeas.evaluate(x[9:14], abs(v), abs(i), power.real, power.imag, self.frequency_pu)
        u, _, p, q, _ = measured
        flag = 1 if self.qc.fault(u) else (2 if self.discrete_state['fault'] or t < self.discrete_state['post_until'] else 0)
        ipc, dpc = self.pc.evaluate(x[14:16], self.pWTref, u, self.discrete_state['pref'])
        iqc, dqc = self.qc.evaluate(x[16:20], self.xWTref, u, p, q, self.ql.limits(p, u),
                                   flag, self.discrete_state, self.uref0, self.discrete_state['qord'])
        ip, iq = self.limiter.limit(ipc, iqc, u, flag)
        ipmax = self.limiter.limit(math.inf, iqc, u, flag)[0]
        iqmax = self.limiter.limit(ipc, math.inf, u, flag)[1]
        iqmin = self.limiter.limit(ipc, -math.inf, u, flag)[1]
        dg = self.gs.evaluate(x[:2], ip, iq)
        angle, dpll = self.pll.evaluate(x[2:4], v, self.discrete_state['angle'])
        outputs = dict(ipcmd=ipc, iqcmd=iqc, iplimited=ip, iqlimited=iq,
                       ipmax=ipmax, iqmax=iqmax, iqmin=iqmin,
                       idref=x[0], iqref=-x[1], i_gs=i, pWT=p, qWT=q, uWT=u,
                       FFRT=flag, CB_open=float(self.protection.tripped), angle=angle)
        return outputs, np.r_[dg, dpll, dm, dp, dpc, dqc], protected

    def f(self, t, x, z, v):
        return self.signals(t, x, v)[1]

    def g(self, t, x, z, v):
        return np.empty(0)

    def accept_step(self, t, x, v):
        y, _, pr = self.signals(t, x, v)
        d = self.discrete_state
        before = (d['fault'], d.get('post_active', False), self.protection.tripped)
        fault = self.qc.fault(y['uWT'])
        if d['fault'] and not fault:
            d['post_until'] = t+self.qc.Tpost
        if y['FFRT'] == 0:
            qref = self.xWTref*y['pWT'] if self.qc.MqG in (3, 4) else self.xWTref
            qord = clip(qref, *self.ql.limits(y['pWT'], y['uWT'])) if self.qc.Tqord == 0 else x[16]
            d.update(u=y['uWT'], iq=x[1], qord=qord, pref=x[14])
        d.update(fault=fault, angle=y['angle'], time=t)
        self.protection.accept_step(t, pr[0], pr[4])
        d['post_active'] = not fault and t < d['post_until']
        return before != (fault, d['post_active'], self.protection.tripped)

    def next_event_time(self, after_s):
        times = [self.protection.next_event_time(after_s), self.discrete_state['post_until']]
        return min((t for t in times if t is not None and t > after_s+1e-12), default=None)

    def events(self):
        return ()
