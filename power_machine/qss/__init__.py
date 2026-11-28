"""Flujo cuasi dinámico offline, sin validar contra la referencia externa.

P/Q de cargas positivos consumidos, generación positiva inyectada.
"""

from .series import Series, SeriesStep, read_series
from .runner import run_series

__all__ = ["Series", "SeriesStep", "read_series", "run_series"]
