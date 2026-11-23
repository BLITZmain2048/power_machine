'Published frequency grid support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import cmath
import math

from .measurements import PublishedFrequencyHistory


class PublishedFrequencyGrid:
    'Interpolate accepted phasors and update at integer multiples of 0.001 s. Supply t-minus before restarting at t-plus. Preserve pre-event memory.'

    step_s = .001

    def __init__(self, *, f_nom_hz=60., interpolation="complex_linear"):
        if interpolation not in ("complex_linear", "polar_linear"):
            raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Unknown interpolation')
        if not math.isfinite(f_nom_hz) or f_nom_hz <= 0:
            raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Invalid nominal frequency')
        self.interpolation = interpolation
        self.history = PublishedFrequencyHistory(f_nom_hz=f_nom_hz)
        self.time_s = None
        self.voltage = {}
        self.f_ref_pu = 1.
        self.next_index = 1

    def _interpolate(self, left, right, fraction):
        if self.interpolation == "complex_linear" or not left or not right:
            return left + fraction*(right-left)
        magnitude_pu = abs(left) + fraction*(abs(right)-abs(left))
        phase_rad = cmath.phase(left) + fraction*cmath.phase(right/left)
        return cmath.rect(magnitude_pu, phase_rad)

    def accept(self, time_s, voltage, *, restart=False, f_ref_pu=1.):
        'Accept the right endpoint without modifying caller data.'
        time_s = float(time_s)
        if not math.isfinite(time_s) or not math.isfinite(f_ref_pu):
            raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Reference time and speed must be finite')
        if any(not (math.isfinite(v.real) and math.isfinite(v.imag)) for v in voltage.values()):
            raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Phasor must be finite')
        if self.time_s is None:
            if abs(time_s) > 1e-12 or restart:
                raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Nominal grid must start at t=0 without a jump')
            self.history.accept(0., voltage, f_ref_pu=f_ref_pu)
        else:
            if time_s < self.time_s-1e-12:
                raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'History is out of time order')
            if voltage.keys() != self.voltage.keys():
                raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Bus identities differ')
            if restart:
                if abs(time_s-self.time_s) > 1e-12:
                    raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Provide t- before t+ at the same timestamp')
                self.history.accept(time_s, voltage, restart=True, f_ref_pu=f_ref_pu)
            else:
                while self.next_index*self.step_s <= time_s+1e-12:
                    grid_s = self.next_index*self.step_s
                    # En coincidencia usar el extremo original, evita errores
                    # de redondeo y reproduce la historia uniforme bit a bit.
                    if abs(grid_s-time_s) <= 1e-12:
                        vv, speed_pu = voltage, f_ref_pu
                    else:
                        fraction = (grid_s-self.time_s)/(time_s-self.time_s)
                        vv = {bid: self._interpolate(self.voltage[bid], v, fraction)
                              for bid, v in voltage.items()}
                        speed_pu = self.f_ref_pu + fraction*(f_ref_pu-self.f_ref_pu)
                    self.history.accept(grid_s, vv, f_ref_pu=speed_pu)
                    self.next_index += 1
        self.time_s, self.voltage, self.f_ref_pu = time_s, dict(voltage), f_ref_pu
        # El estado interno conserva la memoria original a V=0, la salida
        # es ausente porque la fase y su frecuencia no están definidas.
        return {bid: (self.history.frequency[bid] if v else float("nan"))
                for bid, v in voltage.items()}


class FrequencyObservers:
    'Three observables of one physical trajectory with an explicit output selection.'

    def __init__(self, freq_estimator="accepted_step"):
        if freq_estimator not in ("accepted_step", "nominal_grid"):
            raise _coded_error(ValueError, 'invalid_published_frequency_grid_data', 'Unknown estimator')
        self.selected = freq_estimator
        self.histories = {
            "accepted_step": PublishedFrequencyHistory(f_nom_hz=60.),
            "nominal_grid": PublishedFrequencyGrid(),
            "polar_sensitivity": PublishedFrequencyGrid(interpolation="polar_linear"),
        }
        self.outputs = {}

    def accept(self, time_s, voltage, *, restart=False, f_ref_pu=1.):
        self.outputs = {name: history.accept(time_s, voltage, restart=restart,
                                             f_ref_pu=f_ref_pu)
                        for name, history in self.histories.items()}
        return self.outputs[self.selected]
