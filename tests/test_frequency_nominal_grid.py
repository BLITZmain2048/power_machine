"""Estimador nominal offline, recurrencia, subdivisiones y saltos."""
import cmath

import numpy as np
import pytest

from power_machine.dynamics.measurements import PublishedFrequencyHistory
from power_machine.dynamics.published_frequency_grid import PublishedFrequencyGrid, FrequencyObservers


def test_uniform_grid_exact_original():
    original, grid = PublishedFrequencyHistory(f_nom_hz=60.), PublishedFrequencyGrid()
    for k in range(201):
        t = k*.001
        voltage = {"id": cmath.rect(1.+t/20., .4*t*t), "id2": cmath.rect(.9, -.2*t)}
        speed = 1.+t*.001
        assert original.accept(t, voltage, f_ref_pu=speed) == grid.accept(t, voltage, f_ref_pu=speed)


def test_step_subdivision_same_piecewise_linear_phasor_and_speed():
    coarse, divided = PublishedFrequencyGrid(), PublishedFrequencyGrid()
    def voltage(t):
        return {"id": 1. + (.4+1.2j)*t}
    for t in np.arange(0., .021, .004):
        coarse.accept(t, voltage(t), f_ref_pu=1.+t*.03)
    for t in (0., .00017, .00091, .00231, .003999, .004, .00757, .008, .01199, .012, .016, .020):
        divided.accept(t, voltage(t), f_ref_pu=1.+t*.03)
    assert coarse.next_index == divided.next_index == 21
    assert coarse.history.frequency["id"] == pytest.approx(divided.history.frequency["id"], abs=2e-13)
    assert coarse.history.instantaneous["id"] == pytest.approx(divided.history.instantaneous["id"], abs=2e-13)


def test_jump_matches_original_on_grid_and_does_not_measure_phase_jump():
    original, grid = PublishedFrequencyHistory(f_nom_hz=60.), PublishedFrequencyGrid()
    for k in range(8):
        t = k*.001
        v = {"id": cmath.rect(1., .002*k + (1. if k > 3 else 0.))}
        assert original.accept(t, v) == grid.accept(t, v)
        if k == 3:
            plus = {"id": cmath.rect(.8, .002*k+1.)}
            assert original.accept(t, plus, restart=True) == grid.accept(t, plus, restart=True)
    assert abs(grid.history.frequency["id"]-60.) < 1.


def test_event_between_grid_points_never_interpolates_across_jump():
    grid = PublishedFrequencyGrid()
    grid.accept(0., {"id": 1.+0j})
    grid.accept(.0015, {"id": 1.+0j})  # t- también integra el punto 1 ms
    assert grid.history.frequency["id"] == 60.
    grid.accept(.0015, {"id": 1j}, restart=True)
    grid.accept(.003, {"id": 1j})
    assert grid.history.frequency["id"] == 60.
    assert grid.history.instantaneous["id"] == 60.
    assert grid.next_index == 4


def test_zero_phasor_absent_preserves_memory_and_recovery():
    grid = PublishedFrequencyGrid()
    grid.accept(0., {"id": 1.+0j})
    grid.accept(.001, {"id": 1.+0j})
    assert np.isnan(grid.accept(.001, {"id": 0j}, restart=True)["id"])
    assert np.isnan(grid.accept(.002, {"id": 0j})["id"])
    assert grid.history.frequency["id"] == 60.
    grid.accept(.002, {"id": 1j}, restart=True)
    assert grid.accept(.003, {"id": 1j})["id"] == 60.


def test_polar_sensitivity_principal_angle_and_zero_fallback():
    grid = PublishedFrequencyGrid(interpolation="polar_linear")
    a, b = cmath.rect(1., np.deg2rad(179.)), cmath.rect(1., np.deg2rad(-179.))
    assert abs(grid._interpolate(a, b, .5)+1.) < 1e-14
    assert grid._interpolate(0j, 1j, .5) == .5j


def test_no_update_on_tiny_extra_accepted_step():
    grid = PublishedFrequencyGrid()
    grid.accept(0., {"id": 1.+0j})
    grid.accept(.001, {"id": 1.+.01j})
    before = dict(grid.history.frequency)
    grid.accept(.001+1e-9, {"id": 1.+.01000001j})
    assert grid.history.frequency == before
    assert grid.next_index == 2


@pytest.mark.parametrize("bad", ["fit", None, ""])
def test_unknown_estimator_rejected(bad):
    with pytest.raises(ValueError, match="estimator"):
        FrequencyObservers(bad)


def test_invalid_time_and_restart_order():
    grid = PublishedFrequencyGrid()
    with pytest.raises(ValueError, match="t=0"):
        grid.accept(.001, {"id": 1j})
    grid.accept(0., {"id": 1j})
    with pytest.raises(ValueError, match="t-"):
        grid.accept(.002, {"id": 1j}, restart=True)
    with pytest.raises(ValueError, match="must be finite"):
        grid.accept(np.nan, {"id": 1j})
    grid.accept(.001, {"id": 1j})
    with pytest.raises(ValueError, match="order"):
        grid.accept(0., {"id": 1j})
    with pytest.raises(ValueError, match="identities"):
        grid.accept(.002, {"other": 1j})


def test_input_dict_not_mutated():
    voltage = {"id": 1.+.01j}
    before = voltage.copy()
    grid = PublishedFrequencyGrid()
    grid.accept(0., voltage)
    grid.accept(.004, voltage)
    assert voltage == before

