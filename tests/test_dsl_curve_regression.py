"""Preserva las curvas DSL, incluidos ceros con signo y entradas mutables."""
import struct

import pytest

from power_machine.dynamics.dsl_expr import linear_lookup


def bits(value):
    return struct.pack('!d', value)


@pytest.mark.parametrize('inverse', [False, True])
def test_distinct_curves_preserve_signed_zero(inverse):
    negative = tuple([tuple([0.0, -0.0]), tuple([1.0, 1.0])])
    positive = tuple([tuple([0.0, 0.0]), tuple([1.0, 1.0])])
    if inverse:
        negative = tuple([tuple([-0.0, 0.0]), tuple([1.0, 1.0])])
        positive = tuple([tuple([0.0, 0.0]), tuple([1.0, 1.0])])
    for _ in range(3):
        assert bits(linear_lookup(-1.0, negative, inverse=inverse)) == bits(-0.0)
        assert bits(linear_lookup(-1.0, positive, inverse=inverse)) == bits(0.0)


@pytest.mark.parametrize('outer_tuple', [False, True])
def test_mutable_rows_are_read_again_and_revalidated(outer_tuple):
    rows = [[0.0, 0.0], [1.0, 1.0]]
    curve = tuple(rows) if outer_tuple else rows
    assert bits(linear_lookup(0.5, curve)) == bits(0.5)
    rows[1][1] = 3.0
    assert bits(linear_lookup(0.5, curve)) == bits(1.5)
    rows[1][0] = 0.0
    with pytest.raises(ValueError, match='abscissae'):
        linear_lookup(0.5, curve)


def test_repeated_immutable_curve_preserves_segments_and_inverse():
    curve = ((-1.0, -3.0), (0.0, 1.0), (2.0, 5.0))
    for _ in range(3):
        for x, y in ((-1.0, -3.0), (0.25, 1.5), (1.5, 4.0)):
            assert bits(linear_lookup(x, curve)) == bits(y)
            assert bits(linear_lookup(y, curve, inverse=True)) == bits(x)


def test_invalid_immutable_curve_keeps_diagnostic():
    for _ in range(2):
        with pytest.raises(ValueError, match='must be finite'):
            linear_lookup(0.5, ((0.0, 0.0), (1.0, float('inf'))))

