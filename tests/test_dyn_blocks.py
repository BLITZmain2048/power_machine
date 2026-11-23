"""Pruebas migradas de bloques dinámicos elementales."""

import numpy as np
import pytest

from power_machine.dynamics.blocks import Deadband, FirstOrderLag, Integrator, RateLimiter


def test_lag_de_primer_orden_y_jacobiano():
    block = FirstOrderLag(2.0, 3.0)
    assert block.f(1.0, 1.0, 2.0) == pytest.approx(2.5)
    np.testing.assert_allclose(block.jacobian(1.0, 1.0, 2.0).df, [-0.5, 0.0, 1.5])


def test_integrador_y_limitador_de_tasa():
    integrator = Integrator(2.0, 0.0, 1.0)
    assert integrator.f(1.0, 1.0, 1.0) == 0
    rate = RateLimiter(0.5, -2.0, 3.0)
    assert rate.f(0.0, 0.0, 10.0) == 3.0
    assert rate.f(0.0, 0.0, -10.0) == -2.0


def test_banda_muerta():
    block = Deadband(-0.1, 0.2)
    assert [block.output(value) for value in (-0.3, 0.0, 0.5)] == pytest.approx([-0.2, 0.0, 0.3])
