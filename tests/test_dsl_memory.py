"""Memoria DSL transaccional según User Manual REF 2024 §29.16."""
import pytest

from power_machine.dynamics.dsl_expr import DSLMemory, evaluate, parse_expression


def _read(memory, expression, time_s, **signals):
    return evaluate(expression, signals, time_s=time_s, runtime=memory)


def _accept(memory, expression, time_s, **signals):
    memory.begin_accept()
    value = _read(memory, expression, time_s, **signals)
    memory.finish_accept()
    return value


@pytest.mark.parametrize("function", ["picdro", "picdro_const"])
def test_pickup_dropout_and_no_newton_mutation(function):
    expr = parse_expression(f"{function}(u,0.2,0.1)")
    memory = DSLMemory()
    assert _accept(memory, expr, 0.0, u=0) == 0
    assert _read(memory, expr, 0.1, u=1) == 0
    assert _read(memory, expr, 0.1, u=1) == 0
    assert len(memory.records) == 1
    _accept(memory, expr, 0.1, u=1)
    assert memory.next_event_time(0.1) == pytest.approx(0.3)
    assert _read(memory, expr, 0.299, u=1) == 0
    assert _accept(memory, expr, 0.3, u=1) == 1
    _accept(memory, expr, 0.4, u=0)
    assert _read(memory, expr, 0.499, u=0) == 1
    assert _accept(memory, expr, 0.5, u=0) == 0


def test_flipflop_and_analog_latch():
    flip = parse_expression("flipflop(s,r)")
    analog = parse_expression("aflipflop(x,s,r)")
    memory = DSLMemory()
    assert _accept(memory, flip, 0, s=0, r=0) == 0
    assert _read(memory, flip, 1, s=1, r=0) == 0
    _accept(memory, flip, 1, s=1, r=0)
    assert _read(memory, flip, 1, s=0, r=0) == 1
    _accept(memory, flip, 2, s=0, r=1)
    assert _read(memory, flip, 2, s=0, r=0) == 0
    with pytest.raises(ValueError, match='simultaneous'):
        _read(DSLMemory(), flip, 0, s=1, r=1)
    _accept(memory, analog, 0, x=2, s=0, r=0)
    assert _read(memory, analog, 1, x=3, s=0, r=0) == 3
    _accept(memory, analog, 1, x=3, s=1, r=0)
    assert _read(memory, analog, 2, x=7, s=0, r=0) == 3
    _accept(memory, analog, 2, x=7, s=0, r=1)
    assert _read(memory, analog, 2, x=8, s=0, r=0) == 8


def test_delay_gradient_lastvalue_moving_average():
    delay = parse_expression("delay(x,1)")
    gradient = parse_expression("gradlim_const(x,-2,3)")
    last = parse_expression("lastvalue(x)")
    avg = parse_expression("movingavg(x,0,2)")
    memory = DSLMemory()
    for expr in (delay, gradient, last, avg):
        _accept(memory, expr, 0, x=0)
    assert _read(memory, gradient, 0.5, x=10) == pytest.approx(1.5)
    assert _read(memory, gradient, 0.5, x=10) == pytest.approx(1.5)
    assert _read(memory, last, 0.5, x=10) == 0
    _accept(memory, gradient, 0.5, x=10)
    _accept(memory, last, 0.5, x=10)
    assert _read(memory, last, 0.5, x=20) == 0
    assert _read(memory, last, 0.6, x=20) == 10
    _accept(memory, delay, 1, x=2)
    assert memory.next_event_time(1) == pytest.approx(2)
    assert _read(memory, delay, 1.5, x=4) == pytest.approx(1)
    _accept(memory, avg, 2, x=2)
    assert _read(memory, avg, 2, x=2) == pytest.approx(1)


def test_select_const_evaluates_only_chosen_branch():
    expr = parse_expression("select_const(flag, 2, missing)")
    assert evaluate(expr, {"flag": 1}) == 2
    assert evaluate(parse_expression("select_const(flag,2,3)"), {"flag": 0}) == 3

