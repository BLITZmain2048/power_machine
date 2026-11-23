'Schema support for power-system critical clearing time.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass
import math


def _positive(value, label):
    if isinstance(value, bool) or not math.isfinite(float(value)) or float(value) <= 0:
        raise _coded_error(ValueError, 'invalid_schema_data', f'{label} must be positive and finite')
    return float(value)


@dataclass(frozen=True)
class CCTOptions:
    'Explicit CCT thresholds in seconds, milliseconds, degrees and p.u.'

    lower_clearing_s: float = 0.05
    upper_clearing_s: float = 0.35
    resolution_ms: float = 5.0
    end_s: float = 5.0
    step_ms: float = 2.0
    max_angle_from_coi_deg: float = 180.0
    recovery_min_voltage_pu: float = 0.90
    recovery_window_s: float = 0.20

    def __post_init__(self):
        lower = _positive(self.lower_clearing_s, "lower_clearing_s")
        upper = _positive(self.upper_clearing_s, "upper_clearing_s")
        resolution = _positive(self.resolution_ms, "resolution_ms")
        end = _positive(self.end_s, "end_s")
        step = _positive(self.step_ms, "step_ms")
        angle = _positive(self.max_angle_from_coi_deg, "max_angle_from_coi_deg")
        voltage = _positive(self.recovery_min_voltage_pu, "recovery_min_voltage_pu")
        window = _positive(self.recovery_window_s, "recovery_window_s")
        if lower >= upper:
            raise _coded_error(ValueError, 'invalid_schema_data', 'The stable endpoint must be less than the unstable endpoint')
        if upper >= end:
            raise _coded_error(ValueError, 'invalid_schema_data', 'upper_clearing_s must be less than end_s')
        if voltage > 2.0:
            raise _coded_error(ValueError, 'invalid_schema_data', 'recovery_min_voltage_pu must not exceed 2 p.u.')
        object.__setattr__(self, "lower_clearing_s", lower)
        object.__setattr__(self, "upper_clearing_s", upper)
        object.__setattr__(self, "resolution_ms", resolution)
        object.__setattr__(self, "end_s", end)
        object.__setattr__(self, "step_ms", step)
        object.__setattr__(self, "max_angle_from_coi_deg", angle)
        object.__setattr__(self, "recovery_min_voltage_pu", voltage)
        object.__setattr__(self, "recovery_window_s", window)

    @property
    def resolution_s(self):
        return self.resolution_ms / 1000.0

    @property
    def step_s(self):
        return self.step_ms / 1000.0


@dataclass(frozen=True)
class CCTInput:
    'Fault and clearing description without implicit electrical parameters.'

    engine: str = "auto"
    fault_bus_id: str = ""
    fault_inception_s: float = 1.0
    clearing_action: str = "simple"
    clearing_branch_id: str | None = None
    fault_r_ohm: float | None = None
    fault_x_ohm: float | None = None

    def __post_init__(self):
        if self.engine not in ("auto", "ieee39_classical", "pm_rms"):
            raise _coded_error(ValueError, 'invalid_schema_data', 'Unknown CCT engine')
        if not isinstance(self.fault_bus_id, str):
            raise _coded_error(ValueError, 'invalid_schema_data', 'fault_bus_id must be text')
        inception = _positive(self.fault_inception_s, "fault_inception_s")
        if self.clearing_action not in ("simple", "open_branch"):
            raise _coded_error(ValueError, 'invalid_schema_data', 'clearing_action must be simple or open_branch')
        if self.clearing_action == "open_branch" and not self.clearing_branch_id:
            raise _coded_error(ValueError, 'missing_schema_data', 'open_branch requires clearing_branch_id')
        for value, name in ((self.fault_r_ohm, "fault_r_ohm"), (self.fault_x_ohm, "fault_x_ohm")):
            if value is not None and (isinstance(value, bool) or not math.isfinite(float(value))):
                raise _coded_error(ValueError, 'invalid_schema_data', f'{name} must be finite when supplied')
        if self.fault_r_ohm is not None and float(self.fault_r_ohm) < 0:
            raise _coded_error(ValueError, 'invalid_schema_data', 'fault_r_ohm must be nonnegative')
        object.__setattr__(self, "fault_inception_s", inception)
