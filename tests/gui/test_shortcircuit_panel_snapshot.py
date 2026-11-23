from gui_support import ieee_model, ieee_root
"""Conservación de la instantánea y suplementos de interfaz INT-5B."""
from copy import deepcopy
from power_machine.gui.panels.calc_ccbar import build_request
from power_machine.gui.panels.calc_lfctl import build_request as controls_request
from gui_support import fixture, request_for, ROOT


def test_inline_sc_and_thevenin_preserve_active_snapshot():
    model, supplement = fixture()
    base = request_for(model, supplement)
    class Context:
        root = ROOT
        def get_snapshot(self):
            return base["snapshot"]
    before = deepcopy(base)
    boundary = {"maximum": {"sk_mva": 1000, "x_r": 10, "c": 1.1}}
    result = build_request(Context(), {}, supplemental=supplement,
        supplemental_sha256=base["input"]["supplemental_sha256"], power_machine_mode="thevenin", thevenin=boundary)
    assert result["input"]["supplemental"] == supplement
    assert result["input"]["thevenin"] == boundary
    result["input"]["supplemental"].clear()
    result["snapshot"]["model"].clear()
    assert base == before
    assert controls_request(Context(), {}, {})["snapshot"] == before["snapshot"]
