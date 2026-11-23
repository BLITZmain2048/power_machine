"""Enlace trazable entre resultados de cortocircuito y coordinación de relés.

No calcula corrientes de cortocircuito ni ecuaciones de protección. Adapta las
contribuciones por terminal publicadas por ``shortcircuit`` y llama a
``protection.coordination.pair_point`` con la corriente propia de cada relé.
"""
from .adapter import adapt_fault, execute_request, tcc_fault_markers

__all__ = ("adapt_fault", "execute_request", "tcc_fault_markers")
