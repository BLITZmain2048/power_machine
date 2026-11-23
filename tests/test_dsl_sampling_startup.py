"""Inicio de muestreo DSL con relojes sintéticos."""

import pytest

from power_machine.dynamics.measurements import PeriodicClock, SampleHold


@pytest.mark.parametrize("initial_clock,runtime_clock,captures", [(0.0, 1.0, True), (0.0, 0.0, False), (1.0, 1.0, False)])
def test_sample_hold_initial_edge_is_signal_driven(initial_clock, runtime_clock, captures):
    hold = SampleHold("sample", "synthetic.ElmSamp", PeriodicClock(3.0, phase_s=0.7))
    hold.init(2.0, clock_value=initial_clock)
    assert hold.accept_clock(runtime_clock, 9.0) is captures
    assert hold.output() == (9.0 if captures else 2.0)
    assert hold.accept_clock(runtime_clock, 18.0) is False


def test_sample_hold_requires_initial_clock_for_signal_driven_mode():
    hold = SampleHold("sample", "synthetic.ElmSamp", PeriodicClock(1.0))
    hold.init(2.0)
    with pytest.raises(ValueError, match="initial clock"):
        hold.accept_clock(1.0, 3.0)
