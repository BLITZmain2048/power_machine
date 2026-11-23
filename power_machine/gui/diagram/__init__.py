"""Contrato público del diagrama unifilar power_machine."""

from .layout import diagram_groups
from .view import DiagramCallbacks, DiagramView

__all__ = ["DiagramWidget", "DiagramCallbacks", "DiagramView", "diagram_groups"]

from .widget import DiagramWidget
