from .i18n import tr
"""Exclusión de cálculos pesados por documento, incluso tras cancelar."""


class CalculationGate:
    def __init__(self):
        self.handle = None
        self.reserved = False

    @property
    def running(self):
        if self.handle is not None and self.handle.finished.is_set():
            self.handle = None
        return self.reserved or self.handle is not None

    def reserve(self):
        if self.running:
            raise ValueError(tr('shell.wait_for_the_active_calculation_to_finish_b470cb'))
        self.reserved = True

    def attach(self, handle):
        self.handle = handle
        self.reserved = False


def document_gate(document):
    gate = getattr(document, '_calculation_gate', None)
    if gate is None:
        gate = CalculationGate()
        document._calculation_gate = gate
    return gate
