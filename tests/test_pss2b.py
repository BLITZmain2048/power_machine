"""Independent transfer and synthetic synchronous SMIB checks for PSS2B."""
import numpy as np
import pytest
from power_machine.dynamics.pss2b import PSS2B, PSS2BParameters


def rk4(fun, x, t, h):
    a = fun(t, x)
    b = fun(t+h/2, x+h*a/2)
    c = fun(t+h/2, x+h*b/2)
    d = fun(t+h, x+h*c)
    return x+h*(a+2*b+2*c+d)/6


def test_dc_equilibrium_and_output_limits():
    pss = PSS2B()
    x = pss.init(0., .8)
    y, dx = pss.evaluate(x, 0., .8)
    assert y == 0 and max(abs(dx)) == 0
    assert pss.evaluate(x, 10., .8)[0] == 0.  # Strictly proper input chain
    fast = PSS2B(PSS2BParameters(T6=0., M=0, N=0))
    xf = fast.init(0., .8)
    assert fast.evaluate(xf, 10., .8)[0] == fast.p.Vst_max
    assert fast.evaluate(xf, -10., .8)[0] == fast.p.Vst_min
    assert pss.evaluate(x, 0., .8)[0] == 0


@pytest.mark.parametrize('M,N', [(1, 1), (2, 4), (5, 1), (0, 0)])
def test_transfer_against_complex_frequency_response(M, N):
    p = PSS2BParameters(M=M, N=N, Ks1=.2, T1=.04, T2=.03, T3=.06, T4=.04)
    b = PSS2B(p)
    omega, amp, h = 4., .001, .002
    x = b.init(0., .8)
    times, ys = [], []
    def rhs(t, x):
        return b.evaluate(x, amp*np.sin(omega*t), .8+.4*amp*np.cos(omega*t))[1]
    for j in range(12000):
        t = j*h
        x = rk4(rhs, x, t, h)
        if j > 10000:
            times.append(t+h)
            ys.append(b.evaluate(x, amp*np.sin(omega*(t+h)), .8+.4*amp*np.cos(omega*(t+h)))[0])
    s = 1j*omega
    H = (s*p.Tw1/(1+s*p.Tw1))*(s*p.Tw2/(1+s*p.Tw2))/(1+s*p.T6)
    R = ((1+s*p.T8)/(1+s*p.T9)**M)**N
    B = p.Ks2*(s*p.Tw3/(1+s*p.Tw3))/(1+s*p.T7)
    H = R*H+.4j*(p.Ks3*R-1)*B
    H *= p.Ks1*(1+s*p.T1)/(1+s*p.T2)*(1+s*p.T3)/(1+s*p.T4)
    t = np.array(times)
    coeff, *_ = np.linalg.lstsq(np.c_[np.sin(omega*t), np.cos(omega*t), np.ones(len(t))], ys, rcond=None)
    assert complex(coeff[0], coeff[1])/amp == pytest.approx(H, abs=2e-5)


def smib(h, end=.6):
    # Synthetic one-axis flux-decay synchronous machine against V=1.
    # Pe=E*V*sin(delta)/X, E'= (Efd-E-(Xd-Xd')*Id)/Tdo.
    # AVR Efd=Efd0+Ka*(Vref-Vt+Vst). Network X includes transformer.
    pss = PSS2B(PSS2BParameters(Ks1=.5))
    delta0, E0, X, xd_diff, Tdo, H = .3, 1.1, .5, .3, 3., 3.5
    pe0 = E0*np.sin(delta0)/X
    id0 = (E0-np.cos(delta0))/X
    efd0 = E0+xd_diff*id0
    x0 = np.r_[delta0, 0., E0, pss.init(0., pe0)]
    def rhs(t, x, step=True):
        delta, speed, E = x[:3]
        i = (E*np.exp(1j*delta)-1)/(1j*X)
        vt = 1.+.2j*i
        i0 = (E0*np.exp(1j*delta0)-1)/(1j*X)
        vt0 = abs(1.+.2j*i0)
        pe = E*np.sin(delta)/X
        vs, dp = pss.evaluate(x[3:], speed, pe)
        efd = efd0+10.*(vt0-abs(vt)+vs)
        pm = pe0+(.02 if step else 0.)
        return np.r_[2*np.pi*50*speed, (pm-pe-speed)/(2*H),
                     (efd-E-xd_diff*(E-np.cos(delta))/X)/Tdo, dp]
    assert max(abs(rhs(0., x0, step=False))) < 1e-12
    x = x0.copy()
    peak = 0.
    for j in range(round(end/h)):
        x = rk4(rhs, x, j*h, h)
        pe = x[2]*np.sin(x[0])/X
        vs = pss.evaluate(x[3:], x[1], pe)[0]
        peak = max(peak, abs(vs))
        assert abs(vs) <= pss.p.Vst_max
    assert x[1] != 0 and peak > 1e-5
    return x


def test_synchronous_smib_pm_step_and_halving():
    a, b, c = [smib(h) for h in (.004, .002, .001)]
    assert np.max(abs(b-c)) < np.max(abs(a-b))*.2
    assert np.max(abs(b-c)) < 1e-6


def test_zero_time_bypasses_and_invalid_orders():
    p = PSS2B(PSS2BParameters(T6=0., T7=0., T10=0., T11=0., M=0, N=0))
    assert p.evaluate(p.init(0., .8), 0., .8)[0] == 0
    dc = PSS2B(PSS2BParameters(Tw1=0., Tw2=0., Tw3=0., Tw4=0., M=0, N=0))
    y, dx = dc.evaluate(dc.init(.01, .8), .01, .8)
    assert y == pytest.approx(.05) and max(abs(dx)) == 0.
    for params in (dict(M=1.5), dict(N=-1), dict(T2=0., T1=.1), dict(T9=0.)):
        with pytest.raises(ValueError):
            PSS2BParameters(**params)
