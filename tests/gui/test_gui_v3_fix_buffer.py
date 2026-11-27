"""Append-only temporal snapshots preserve all samples and reset boundaries."""
import numpy as np
import pytest
from power_machine.gui.plots import SignalBuffer,TIME_KEY
from power_machine.gui.time_cursor import TemporalResult

MODEL={'buses':[{'id':'B1','path':'node'}]}
KEY='node|m:u|p.u.'

def buffer():
    value=SignalBuffer();value.reset([TIME_KEY,KEY,'node|energized|bool'])
    value.append([[0,1.,1],[1,.9,1]])
    return value


def assert_rebuilt_equal(value,previous):
    incremental=TemporalResult.from_buffer(value,MODEL,previous)
    rebuilt=TemporalResult(value,MODEL)
    for key in value.keys:np.testing.assert_allclose(incremental.columns[key],rebuilt.columns[key],equal_nan=True)
    for t in np.linspace(rebuilt.start,rebuilt.end,11):assert incremental.state(float(t))==rebuilt.state(float(t))
    return incremental


def test_incremental_append_preserves_duplicate_events_nan_gaps_and_previous_snapshot():
    value=buffer();previous=TemporalResult.from_buffer(value,MODEL)
    value.append([[1,.4,0],[2,.8,1],[3,None,1]])
    current=assert_rebuilt_equal(value,previous)
    assert current.sample(KEY,.5)==pytest.approx(.95)
    assert current.state(1)[0]['B1']==dict(u_pu=.4,energized=False)
    assert current.sample(KEY,2.5) is None
    assert previous.sample(KEY,1)==.9
    assert len(previous.times)==2


def test_reset_with_identical_keys_and_matching_boundary_never_reuses_old_prefix():
    value=buffer();previous=TemporalResult.from_buffer(value,MODEL)
    generation=value.generation
    value.reset(value.keys);value.append([[0,.1,1],[1,.9,1],[2,.8,1]])
    assert value.generation!=generation
    current=assert_rebuilt_equal(value,previous)
    assert current.sample(KEY,0)==.1


def test_out_of_order_append_falls_back_to_stable_sort():
    value=buffer();previous=TemporalResult.from_buffer(value,MODEL)
    value.append([[.5,.7,0]])
    current=assert_rebuilt_equal(value,previous)
    assert current.sample(KEY,.5)==.7
    value.append([[2,.8,1]])
    assert_rebuilt_equal(value,current)


def test_nonfinite_times_are_filtered_and_future_updates_remain_correct():
    value=buffer();value.append([[None,.3,0]])
    previous=TemporalResult.from_buffer(value,MODEL)
    value.append([[2,.8,1]])
    assert_rebuilt_equal(value,previous)


def test_empty_append_does_not_change_the_acquired_interval():
    value=buffer();previous=TemporalResult.from_buffer(value,MODEL)
    value.append([])
    current=assert_rebuilt_equal(value,previous)
    assert (current.start,current.end)==(0,1)
