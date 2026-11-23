'Measurements support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import cmath
import math
from dataclasses import dataclass, field

import numpy as np


def phasor_frequency_hz(v_previous: complex, v_current: complex, dt_s: float,
                        *, f_nom_hz: float = 50.0,
                        f_ref_pu: float = 1.0) -> float:
    'Return F_ref + unwrapped phase increment divided by 2*pi*dt.'
    if dt_s <= 0 or f_nom_hz <= 0 or abs(v_previous) == 0 or abs(v_current) == 0:
        raise _coded_error(ValueError, 'invalid_measurements_data', 'Invalid interval, nominal value or phasor')
    delta_rad = cmath.phase(complex(v_current) / complex(v_previous))
    return f_nom_hz * f_ref_pu + delta_rad / (2 * math.pi * dt_s)


@dataclass
class PhasorFrequencyHistory:
    'Phase derivative between accepted samples on a continuous segment. Use the RMS common-result phasor convention and explicit nominal frequency.'

    f_nom_hz: float = 50.0
    time_s: float | None = None
    voltage: dict[str, complex] = field(default_factory=dict)
    frequency: dict[str, float] = field(default_factory=dict)

    def accept(self, time_s: float, voltage: dict[str, complex], *,
               restart: bool = False) -> dict[str, float]:
        if self.time_s is not None and time_s < self.time_s-1e-12:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Frequency history is out of time order')
        dt = 0. if self.time_s is None else time_s-self.time_s
        measured = {}
        for bid, v in voltage.items():
            old = self.voltage.get(bid)
            held = self.frequency.get(bid, self.f_nom_hz)
            measured[bid] = (phasor_frequency_hz(old, v, dt, f_nom_hz=self.f_nom_hz)
                             if not restart and dt > 1e-12 and old and v else held)
        self.time_s = float(time_s)
        self.voltage = dict(voltage)
        self.frequency = measured
        return dict(measured)


@dataclass
class PublishedFrequencyHistory:
    'Published PF frequency observable. It never feeds physical controls. Preserve the configured pre-event history and reference-frame convention.'

    f_nom_hz: float = 50.0
    time_s: float | None = None
    voltage: dict[str, complex] = field(default_factory=dict)
    frequency: dict[str, float] = field(default_factory=dict)
    instantaneous: dict[str, float] = field(default_factory=dict)
    skip_interval: bool = False

    def accept(self, time_s: float, voltage: dict[str, complex], *,
               restart: bool = False, f_ref_pu: float = 1.) -> dict[str, float]:
        if self.time_s is not None and time_s < self.time_s-1e-12:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Frequency history is out of time order')
        first = self.time_s is None
        dt = 0. if first else time_s-self.time_s
        for bid, v in voltage.items():
            self.frequency.setdefault(bid, self.f_nom_hz*f_ref_pu)
            self.instantaneous.setdefault(bid, self.f_nom_hz*f_ref_pu)
            if dt > 1e-12 and not restart:
                self.frequency[bid] = .5*(self.frequency[bid]+self.instantaneous[bid])
                old = self.voltage.get(bid)
                if not self.skip_interval and old and v:
                    self.instantaneous[bid] = phasor_frequency_hz(
                        old, v, dt, f_nom_hz=self.f_nom_hz, f_ref_pu=f_ref_pu)
        if restart and not first:
            self.skip_interval = True
        elif dt > 1e-12:
            self.skip_interval = False
        self.time_s, self.voltage = float(time_s), dict(voltage)
        return dict(self.frequency)


@dataclass
class VoltageMeasurement:
    'Balanced StaVmea with Tf=3/Fnom. States are [cos(phi), sin(phi)]. Frequency derives from those states, not from node speed.'

    object_id: str
    relative_path: str
    f_nom_hz: float = 50.0

    def init(self, v: complex) -> np.ndarray:
        if abs(v) == 0:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Cannot initialize StaVmea at zero voltage')
        u = complex(v) / abs(v)
        return np.array([u.real, u.imag], dtype=float)

    def f(self, state: np.ndarray, v: complex) -> np.ndarray:
        if self.f_nom_hz <= 0 or abs(v) == 0 or len(state) != 2:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Invalid voltage measurement')
        u = complex(v) / abs(v)
        return (np.array([u.real, u.imag]) - np.asarray(state, dtype=float)) * self.f_nom_hz / 3.0

    def outputs(self, state: np.ndarray, v: complex,
                *, f_ref_pu: float = 1.0) -> dict[str, float]:
        derivative = self.f(state, v)
        cosine, sine = float(state[0]), float(state[1])
        # TechRef (6): elige el denominador de mayor módulo para evitar cero.
        if abs(cosine) > abs(sine):
            df_pu = derivative[1] / cosine / (2 * math.pi * self.f_nom_hz)
        else:
            df_pu = -derivative[0] / sine / (2 * math.pi * self.f_nom_hz)
        voltage = complex(v)
        return {"ur_pu": voltage.real, "ui_pu": voltage.imag,
                "u_pu": abs(voltage), "fe_pu": f_ref_pu + df_pu,
                "fe_hz": (f_ref_pu + df_pu) * self.f_nom_hz}


@dataclass
class PQMeasurement:
    'Positive-sequence StaPqmea with explicit orientation and network-base current injection.'

    object_id: str
    relative_path: str
    s_base_mva: float = 100.0
    snom_mva: float | None = None
    orientation: int = 0

    def measure(self, v: complex, current_injection_pu: complex) -> dict[str, float]:
        if self.s_base_mva <= 0 or self.orientation not in (0, 1):
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Invalid StaPqmea base or orientation')
        s_mva = complex(v) * complex(current_injection_pu).conjugate() * self.s_base_mva
        if self.orientation == 0:
            s_mva = -s_mva
        output = {"p_mw": s_mva.real, "q_mvar": s_mva.imag}
        if self.snom_mva is not None:
            if self.snom_mva <= 0:
                raise _coded_error(ValueError, 'invalid_measurements_data', 'Snom must be positive')
            output.update(p_pu=s_mva.real / self.snom_mva,
                          q_pu=s_mva.imag / self.snom_mva)
        return output


@dataclass
class PhaseLockedLoop:
    'RMS ElmPhi PLL with PI, limits and version 1 or 3. States include integral frequency and phase. Version 3 adds reference-frequency tracking phase.'

    object_id: str
    relative_path: str
    version: int
    kp: float
    ki: float
    fmin_pu: float
    fmax_pu: float
    f_nom_hz: float = 50.0
    umin_pu: float = 0.0
    blocking_enabled: bool = False
    reference_frequency_pu: float = 1.0
    discrete_state: dict[str, object] = field(default_factory=lambda: {"blocked": False})

    @classmethod
    def from_params(cls, object_id: str, relative_path: str, params: dict,
                    *, f_nom_hz: float = 50.0) -> "PhaseLockedLoop":
        needed = ("mversion", "Kp", "Ki", "fmin", "fmax", "umin", "iopt_bl")
        missing = [k for k in needed if params.get(k) is None]
        if missing:
            raise _coded_error(ValueError, 'missing_measurements_data', f'Missing PLL parameters: {missing}')
        return cls(object_id, relative_path, int(params["mversion"]),
                   float(params["Kp"]), float(params["Ki"]),
                   float(params["fmin"]), float(params["fmax"]),
                   f_nom_hz=f_nom_hz, umin_pu=float(params["umin"]),
                   blocking_enabled=bool(params["iopt_bl"]))

    def __post_init__(self) -> None:
        if self.version not in (1, 3):
            raise _coded_error(NotImplementedError, 'unsupported_measurements_data', f'Versión PLL {self.version}')
        if self.f_nom_hz <= 0 or not 0 < self.fmin_pu < self.fmax_pu or self.ki < 0:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Invalid PLL parameters')

    @property
    def n_states(self) -> int:
        return 3 if self.version == 3 else 2

    def init(self, v: complex) -> np.ndarray:
        if abs(v) == 0:
            raise _coded_error(ValueError, 'missing_measurements_data', 'PLL requires an initial phasor')
        state = [0.0, cmath.phase(v)]
        if self.version == 3:
            state.append(0.0)
        return np.array(state, dtype=float)

    def update_blocking(self, v: complex) -> bool:
        'Apply voltage blocking hysteresis: enter below umin and release above umin+5 percent.'
        if not self.blocking_enabled:
            return False
        blocked = bool(self.discrete_state["blocked"])
        if abs(v) < self.umin_pu:
            blocked = True
        elif abs(v) > self.umin_pu + 0.05:
            blocked = False
        self.discrete_state["blocked"] = blocked
        return blocked

    def _signals(self, state: np.ndarray, v: complex) -> tuple[float, float, float]:
        if len(state) != self.n_states:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Incorrect PLL state dimension')
        tracking = float(state[2]) if self.version == 3 else 0.0
        transformed = complex(v) * cmath.exp(-1j * tracking)
        dphi = (transformed * cmath.exp(-1j * float(state[1]))).imag
        omega_nom = 2 * math.pi * self.f_nom_hz
        f_center = 1.0 if self.version == 3 else self.reference_frequency_pu
        dom_raw = self.kp * dphi + float(state[0])
        dom = min(max(dom_raw, omega_nom * (self.fmin_pu - f_center)),
                  omega_nom * (self.fmax_pu - f_center))
        return dphi, dom, f_center

    def f(self, state: np.ndarray, v: complex) -> np.ndarray:
        dphi, dom, center = self._signals(state, v)
        omega_nom = 2 * math.pi * self.f_nom_hz
        lower, upper = omega_nom * (self.fmin_pu-center), omega_nom * (self.fmax_pu-center)
        raw = self.kp*dphi + float(state[0])
        x1dot = 0.0 if (raw >= upper and dphi > 0) or (raw <= lower and dphi < 0) else self.ki*dphi
        if self.discrete_state["blocked"]:
            x1dot = 0.0
            dom = 0.0
        out = [x1dot, dom]
        if self.version == 3:
            out.append(0.0 if self.discrete_state["blocked"] else
                       omega_nom * (self.reference_frequency_pu-1.0))
        return np.array(out, dtype=float)

    def outputs(self, state: np.ndarray, v: complex) -> dict[str, float]:
        _, dom, center = self._signals(state, v)
        frequency_pu = center + dom / (2*math.pi*self.f_nom_hz)
        phi = float(state[1]) + (float(state[2]) if self.version == 3 else 0.0)
        return {"f_meas_pu": frequency_pu, "f_meas_hz": frequency_pu*self.f_nom_hz,
                "phi_rad": phi, "cosphi": math.cos(phi), "sinphi": math.sin(phi)}


@dataclass(frozen=True)
class PeriodicClock:
    'Rising edges at phase_s+n*period_s and a high pulse with the specified duty ratio.'

    period_s: float
    phase_s: float = 0.0
    duty: float = 0.5

    @classmethod
    def from_pf_params(cls, params: dict, *, phase_s: float = 0.0) -> "PeriodicClock":
        'Convert explicit ElmClock.Tp from milliseconds to seconds.'
        if params.get("Tp") is None or params.get("tonTp") is None:
            raise _coded_error(ValueError, 'missing_measurements_data', 'Missing clock Tp/tonTp')
        return cls(float(params["Tp"])/1000.0, phase_s,
                   float(params["tonTp"]))

    @classmethod
    def from_ppc_dsl(cls, catalog, dsl_path: str) -> "PeriodicClock":
        'Read the supported Clock_DMAP expression modulo(time(),T)>=T/2 through the public DSL compiler. Reject other clock semantics.'
        compiled = catalog.compile_dsl(dsl_path)
        if not compiled.type_path.endswith("Clock_DMAP.BlkDef"):
            raise _coded_error(ValueError, 'invalid_measurements_data', 'ElmDsl does not use Clock_DMAP')
        period = compiled.parameters.get("T")
        if period is None:
            raise _coded_error(ValueError, 'missing_measurements_data', 'Missing PPC clock T')
        return cls(float(period), float(period)/2, 0.5)

    def __post_init__(self) -> None:
        if self.period_s <= 0 or not 0 < self.duty < 1:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Invalid clock period or cycle')

    def rising_edges(self, start_s: float, stop_s: float) -> tuple[float, ...]:
        if stop_s < start_s:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'Time window is reversed')
        first = math.ceil((start_s-self.phase_s-1e-12)/self.period_s)
        last = math.floor((stop_s-self.phase_s+1e-12)/self.period_s)
        return tuple(self.phase_s+n*self.period_s for n in range(first,last+1))

    def value(self, time_s: float) -> int:
        phase = ((time_s-self.phase_s)/self.period_s) % 1.0
        return int(phase < self.duty)


@dataclass
class SampleHold:
    'ElmSamp captures its input only on a rising clock edge.'

    object_id: str
    relative_path: str
    clock: PeriodicClock
    held_value: float | None = None
    previous_clock: bool | None = None

    def init(self, input_value: float, *, clock_value: float | None = None) -> float:
        'Initialize the held input and explicit pre-start clock state.'
        self.held_value = float(input_value)
        self.previous_clock = None if clock_value is None else bool(clock_value > .5)
        return self.held_value

    def accept_clock(self, clock_value: float, input_value: float) -> bool:
        'Commit an accepted rising edge, including the inc-to-runtime transition. Never sample during tentative Newton iterations.'
        if self.previous_clock is None or self.held_value is None:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'SampleHold has no initial clock')
        current = bool(clock_value > .5)
        rising = current and not self.previous_clock
        if rising:
            self.held_value = float(input_value)
        self.previous_clock = current
        return rising

    def sample(self, time_s: float, input_value: float) -> float:
        edges = self.clock.rising_edges(time_s-1e-10, time_s+1e-10)
        if not edges:
            raise _coded_error(ValueError, 'missing_measurements_data', 'SampleHold.sample requires a rising edge')
        self.held_value = float(input_value)
        return self.held_value

    def output(self) -> float:
        if self.held_value is None:
            raise _coded_error(ValueError, 'invalid_measurements_data', 'SampleHold is not initialized')
        return self.held_value
