"""Estudios estáticos PV y QV, sin continuación predictor-corrector.

Los puntos se obtienen mediante llamados independientes al flujo de potencia
existente. Por ello un colapso informado por este paquete es una estimación por
no convergencia de Newton, no una rama de continuación validada.
"""
from .runner import execute_request, run_voltage_stability
from .schema import PVOptions, QVOptions

__all__ = ["PVOptions", "QVOptions", "run_voltage_stability", "execute_request"]
