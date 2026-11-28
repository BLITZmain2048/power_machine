"""Dual-input stabilizer, IEEE 421.5-2005, 8.2, Figure 8-2.

Continuous transfer-block realization. Signals Vs1 (speed deviation) and
Vs2 (electrical power) use the machine bases. Positive Vst adds to the AVR
voltage error. No REF input-selector enumeration is inferred here.
"""
from dataclasses import dataclass
import math
import numpy as np


@dataclass(frozen=True)
class PSS2BParameters:
    """Illustrative parameters, not a manufacturer or plant parameter set."""
    Ks1: float = 5.0
    Ks2: float = 0.1
    Ks3: float = 1.0
    Tw1: float = 2.0
    Tw2: float = 2.0
    Tw3: float = 2.0
    Tw4: float = 0.0
    T6: float = 0.02
    T7: float = 0.2
    T8: float = 0.1
    T9: float = 0.1
    T1: float = 0.1
    T2: float = 0.02
    T3: float = 0.1
    T4: float = 0.02
    T10: float = 0.0
    T11: float = 0.0
    M: int = 2
    N: int = 1
    Vst_min: float = -0.1
    Vst_max: float = 0.1

    def __post_init__(self):
        if not all(math.isfinite(v) for v in vars(self).values()):
            raise ValueError('PSS2B parameters must be finite')
        if any(v < 0 for k, v in vars(self).items() if k.startswith('T')):
            raise ValueError('Negative time constant')
        if (isinstance(self.M, bool) or isinstance(self.N, bool)
                or int(self.M) != self.M or int(self.N) != self.N
                or not 0 <= self.M <= 5 or not 0 <= self.N <= 4):
            raise ValueError('PSS2B requires integer M=0..5, N=0..4')
        if self.N and self.T8 and (not self.M or not self.T9):
            raise ValueError('Improper ramp tracking transfer function')
        for lead, lag in ((self.T1, self.T2), (self.T3, self.T4),
                          (self.T10, self.T11)):
            if lead and not lag:
                raise ValueError('Lead requires a positive lag')
        if self.Vst_min > 0 or self.Vst_max < 0:
            raise ValueError('Output interval must include equilibrium zero')


class PSS2B:
    """IEEE 421.5-2005 Figure 8-2, including ramp tracking and 3 lead-lags.

    a=W2(W1(Vs1))/(1+s*T6), b=Ks2*W4(W3(Vs2))/(1+s*T7).
    c=R(a+Ks3*b)-b, R=[(1+s*T8)/(1+s*T9)**M]**N.
    Vst=clip(Ks1*L1*L2*L3*c). W(s)=s*Tw/(1+s*Tw), with
    Tw=0 meaning bypass. A zero lag with zero lead is also bypass.
    Evaluation is pure, so rejected Newton iterations cannot change memory.
    """
    def __init__(self, parameters=None):
        self.p = parameters or PSS2BParameters()
        p = self.p
        self.blocks = [(0., p.Tw1, 'wash'), (0., p.Tw2, 'wash'),
                       (0., p.T6, 'lag'), (0., p.Tw3, 'wash'),
                       (0., p.Tw4, 'wash'), (0., p.T7, 'lag')]
        # Each of N sections has one zero and M poles. Pair the zero
        # with a pole, then cascade the remaining poles (proper realization).
        self.ramp = []
        for _ in range(p.N):
            for j in range(p.M):
                self.ramp.append(len(self.blocks))
                self.blocks.append((p.T8 if j == 0 else 0., p.T9, 'lead'))
        self.phase = []
        for a, b in ((p.T1, p.T2), (p.T3, p.T4), (p.T10, p.T11)):
            self.phase.append(len(self.blocks))
            self.blocks.append((a, b, 'lead'))
        self.n_states = len(self.blocks)

    def _run(self, x, Vs1, Vs2, initialize=False):
        dx = np.zeros(self.n_states)
        def block(i, u):
            a, b, kind = self.blocks[i]
            if initialize:
                x[i] = u
            if b == 0:
                return u
            dx[i] = (u-x[i])/b
            if kind == 'wash':
                return u-x[i]
            return x[i]+a*dx[i]
        a = block(2, block(1, block(0, Vs1)))
        b = self.p.Ks2*block(5, block(4, block(3, Vs2)))
        c = a+self.p.Ks3*b
        for i in self.ramp:
            c = block(i, c)
        c = self.p.Ks1*(c-b)
        for i in self.phase:
            c = block(i, c)
        return float(np.clip(c, self.p.Vst_min, self.p.Vst_max)), dx

    def init(self, Vs1=0.0, Vs2=0.0):
        x = np.zeros(self.n_states)
        _, dx = self._run(x, Vs1, Vs2, initialize=True)
        if np.max(abs(dx)) > 1e-12:
            raise ValueError('PSS2B has no equilibrium for these inputs')
        return x

    def evaluate(self, x, Vs1, Vs2):
        x = np.asarray(x, dtype=float)
        if x.shape != (self.n_states,) or not np.isfinite(x).all():
            raise ValueError('Invalid PSS2B state')
        if not np.isfinite([Vs1, Vs2]).all():
            raise ValueError('Invalid PSS2B input')
        return self._run(x, Vs1, Vs2)
