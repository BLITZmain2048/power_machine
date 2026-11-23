"""Comprobación del recorrido de paneles con recursos de IEEE 39."""
from types import SimpleNamespace
from gui_support import ieee_model
from power_machine.gui.panels.registry import PanelRegistry
from power_machine.gui.document import Document
from power_machine.study import StudyCase
import pytest


def test_registry_panel_roundtrip(tk_root):
    from power_machine.gui.panel_context import PanelContext
    model = ieee_model()
    document = Document(StudyCase())
    document.set_base(model)
    context = PanelContext(tk_root, __import__('os').environ['POWER_MACHINE_OUTPUT_DIR'],
        lambda: document, lambda: model, log=lambda *args: None, select_element=lambda ident: None)
    registry = PanelRegistry()
    assert not registry.errors
    panel = registry.create('calc_lfctl', tk_root, context)
    panel.pack(fill='both', expand=True)
    tk_root.update()
    panel.mark_stale('Cambio de red')
    assert panel.winfo_exists()
    context.close()
