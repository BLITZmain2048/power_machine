'Avr exponential support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import math

from .avr_ieeet1 import IEEET1AVR


class ExponentialIEEET1AVR(IEEET1AVR):
    'Explicit exponential saturation experiment. The registered IEEET1AVR model is unchanged.'

    def __init__(self, *args, frozen_measurement=False, **kwargs):
        self.frozen_measurement = bool(frozen_measurement)
        super().__init__(*args, **kwargs)

    def _fit_saturation(self):
        p = self.parameters
        self.exp_enabled = not (p["Se1"] == p["Se2"] == 0)
        if not self.exp_enabled:
            self.exp_b_per_pu = 0.0
        else:
            if not (0 < p["E1"] < p["E2"] and 0 < p["Se1"] < p["Se2"]):
                raise _coded_error(ValueError, 'missing_avr_exponential_data', 'Exponential saturation requires 0<E1<E2 and 0<Se1<Se2')
            self.exp_b_per_pu = math.log(p["Se2"]/p["Se1"])/(p["E2"]-p["E1"])
        # A y B cuadráticos no se utilizan en esta variante.
        return 0.0, 0.0, False

    def saturation(self, efd_pu):
        'Return dimensionless saturation. Disable the negative-field branch.'
        if not math.isfinite(efd_pu):
            raise _coded_error(ValueError, 'invalid_avr_exponential_data', 'Efd must be finite')
        if not self.exp_enabled or efd_pu <= 0:
            return 0.0
        return self.parameters["Se1"]*math.exp(
            self.exp_b_per_pu*(efd_pu-self.parameters["E1"]))

    def _saturation_drop(self, efd_pu):
        """(E Se, d(E Se)/dE), pu y adimensional respectivamente."""
        se = self.saturation(efd_pu)
        return efd_pu*se, se*(1+self.exp_b_per_pu*efd_pu)

    def evaluate(self, t_s, x, inputs):
        out, dx = super().evaluate(t_s, x, inputs)
        if self.frozen_measurement and self.parameters["Tr"] == 0:
            dx[0] = 0.0
        return out, dx

    def jacobian(self, t_s, x, inputs):
        df, dy = super().jacobian(t_s, x, inputs)
        if self.frozen_measurement and self.parameters["Tr"] == 0:
            df[0] = 0.0
        return df, dy
