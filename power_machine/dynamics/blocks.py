'Blocks support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass
from typing import Callable
import numpy as np


def clamp(value: float, lower: float, upper: float) -> float:
    if lower > upper:
        raise _coded_error(ValueError, 'invalid_blocks_data', 'Limits are reversed')
    return min(max(value, lower), upper)


@dataclass(frozen=True)
class LocalJacobian:
    'Local derivatives of f and g with respect to (x, y, u), in that order.'
    df: np.ndarray
    dg: np.ndarray


class Primitive:
    state_count = 1

    def f(self, x: float, y: float, u: float) -> float:
        raise _coded_error(NotImplementedError, 'abstract_dynamic_block', 'Dynamic block method must be implemented')

    def g(self, x: float, y: float, u: float) -> float:
        'Return the algebraic residual, zero when y equals the block output.'
        raise _coded_error(NotImplementedError, 'abstract_dynamic_block', 'Dynamic block method must be implemented')

    def jacobian(self, x: float, y: float, u: float) -> LocalJacobian:
        'Return local central differences. At a breakpoint average the two sides.'
        values = np.array([x, y, u], dtype=float)
        df = np.empty(3)
        dg = np.empty(3)
        for j in range(3):
            h = 1e-6 * max(1.0, abs(values[j]))
            plus = values.copy(); minus = values.copy()
            plus[j] += h; minus[j] -= h
            df[j] = (self.f(*plus) - self.f(*minus)) / (2*h)
            dg[j] = (self.g(*plus) - self.g(*minus)) / (2*h)
        return LocalJacobian(df, dg)

    def backward(self, output: float) -> tuple[float, float]:
        'Return equilibrium state and input for an imposed output.'
        raise _coded_error(NotImplementedError, 'abstract_dynamic_block', 'Dynamic block method must be implemented')


@dataclass(frozen=True)
class FirstOrderLag(Primitive):
    time_s: float
    gain: float = 1.0

    def __post_init__(self) -> None:
        if self.time_s < 0:
            raise _coded_error(ValueError, 'invalid_blocks_data', 'time_s must be nonnegative')

    def f(self, x: float, y: float, u: float) -> float:
        return 0.0 if self.time_s == 0 else (self.gain*u-x)/self.time_s

    def g(self, x: float, y: float, u: float) -> float:
        return y-(x if self.time_s > 0 else self.gain*u)

    def backward(self, output: float) -> tuple[float, float]:
        if self.gain == 0:
            if output != 0:
                raise _coded_error(ValueError, 'invalid_blocks_data', 'Output is incompatible with zero gain')
            return 0.0, 0.0
        return output, output/self.gain


@dataclass(frozen=True)
class LeadLag(Primitive):
    lead_s: float
    lag_s: float
    gain: float = 1.0

    def __post_init__(self) -> None:
        if self.lag_s < 0:
            raise _coded_error(ValueError, 'invalid_blocks_data', 'lag_s must be nonnegative')

    def f(self, x: float, y: float, u: float) -> float:
        return 0.0 if self.lag_s == 0 else (u-x)/self.lag_s

    def g(self, x: float, y: float, u: float) -> float:
        if self.lag_s == 0:
            return y-self.gain*u
        return y-self.gain*(x+self.lead_s*self.f(x,y,u))

    def backward(self, output: float) -> tuple[float, float]:
        if self.gain == 0:
            if output != 0:
                raise _coded_error(ValueError, 'invalid_blocks_data', 'Incompatible output')
            return 0.0, 0.0
        u = output/self.gain
        return u, u


@dataclass(frozen=True)
class Integrator(Primitive):
    gain_per_s: float = 1.0
    lower: float = -np.inf
    upper: float = np.inf
    mode: str = "no_windup"  # no_windup, output, antiwindup
    tracking_per_s: float = 0.0

    def __post_init__(self) -> None:
        if self.lower > self.upper or self.mode not in ("no_windup", "output", "antiwindup"):
            raise _coded_error(ValueError, 'invalid_blocks_data', 'Invalid limits or mode')
        if self.tracking_per_s < 0:
            raise _coded_error(ValueError, 'invalid_blocks_data', 'tracking_per_s must be nonnegative')

    def f(self, x: float, y: float, u: float) -> float:
        raw = self.gain_per_s*u
        if self.mode == "no_windup":
            if x >= self.upper and raw > 0 or x <= self.lower and raw < 0:
                return 0.0
        if self.mode == "antiwindup":
            return raw+self.tracking_per_s*(clamp(x,self.lower,self.upper)-x)
        return raw

    def g(self, x: float, y: float, u: float) -> float:
        return y-clamp(x,self.lower,self.upper)

    def backward(self, output: float) -> tuple[float, float]:
        if not self.lower <= output <= self.upper:
            raise _coded_error(ValueError, 'invalid_blocks_data', 'Initial output is outside limits')
        return output, 0.0


@dataclass(frozen=True)
class RateLimiter(Primitive):
    time_s: float
    rate_down_per_s: float
    rate_up_per_s: float

    def __post_init__(self) -> None:
        if self.time_s <= 0 or self.rate_down_per_s > 0 or self.rate_up_per_s < 0:
            raise _coded_error(ValueError, 'invalid_blocks_data', 'Invalid time constant or ramp signs')

    def f(self, x: float, y: float, u: float) -> float:
        return clamp((u-x)/self.time_s,self.rate_down_per_s,self.rate_up_per_s)

    def g(self, x: float, y: float, u: float) -> float:
        return y-x

    def backward(self, output: float) -> tuple[float, float]:
        return output, output


@dataclass(frozen=True)
class Deadband(Primitive):
    lower: float
    upper: float
    state_count = 0

    def __post_init__(self) -> None:
        if self.lower > 0 or self.upper < 0:
            raise _coded_error(ValueError, 'invalid_blocks_data', 'The band must contain zero')

    def output(self, u: float) -> float:
        return u-self.upper if u > self.upper else u-self.lower if u < self.lower else 0.0

    def f(self, x: float, y: float, u: float) -> float:
        return 0.0

    def g(self, x: float, y: float, u: float) -> float:
        return y-self.output(u)

    def backward(self, output: float) -> tuple[float, float]:
        return 0.0, output+(self.upper if output > 0 else self.lower if output < 0 else 0.0)


@dataclass(frozen=True)
class Hysteresis(Primitive):
    lower: float
    upper: float
    state_count = 0

    def __post_init__(self) -> None:
        if self.lower >= self.upper:
            raise _coded_error(ValueError, 'invalid_blocks_data', 'Thresholds are reversed')

    def next_state(self, previous: bool, u: float) -> bool:
        return True if u >= self.upper else False if u <= self.lower else previous

    def f(self, x: float, y: float, u: float) -> float:
        return 0.0

    def g(self, x: float, y: float, u: float) -> float:
        return y-float(bool(x))

    def backward(self, output: float) -> tuple[float, float]:
        if output not in (0,1):
            raise _coded_error(ValueError, 'invalid_blocks_data', 'Hysteresis output must be binary')
        return output, self.upper if output else self.lower
