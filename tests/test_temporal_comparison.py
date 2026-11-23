"""La comparación temporal conserva lados del evento y define el observable."""
import math
import cmath
import numpy as np
import pytest
from power_machine.dynamics.measurements import PublishedFrequencyHistory


def test_published_frequency_rejects_non_monotonic_samples():
    history = PublishedFrequencyHistory()
    history.accept(0.0, {"b": 1 + 0j})
    history.accept(0.001, {"b": cmath.exp(1j * 0.001)})
    with pytest.raises(ValueError, match="time order"):
        history.accept(0.0, {"b": 1 + 0j})


def test_pf04_filter_is_step_dependent_even_for_identical_trajectory():
    def at_end(step):
        history=PublishedFrequencyHistory()
        for t in np.arange(0.,.002+step/2,step):
            result=history.accept(float(t),{'b':cmath.exp(2j*math.pi*.01*t)})
        return result['b']
    assert at_end(.001)==pytest.approx(50.005)
    assert at_end(.0005)==pytest.approx(50.00875)

