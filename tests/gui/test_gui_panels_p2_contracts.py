"""Phase 2 presentation contracts for short circuit and N-1 panels."""
from __future__ import annotations

import json


def test_p2_catalogue_keys_are_bilingual():
    from power_machine.resources import resource_root
    root = resource_root() / "gui" / "locale"
    en = json.loads((root / "en.json").read_text(encoding="utf8"))
    es = json.loads((root / "es.json").read_text(encoding="utf8"))
    keys = {"panel.shortcircuit.profile", "panel.shortcircuit.scope", "panel.scsweep.scope",
            "panel.protection.scope", "panel.contingency.rank_criterion", "panel.contingency.nonconverged_group"}
    assert keys <= set(en) and keys <= set(es)


def test_contingency_rank_mirrors_public_api_and_excludes_nonconverged_group():
    from power_machine.gui.panels.contingency import severity_order
    cases = [
        {"case_id": "G39", "status": "not_converged", "metrics": {"unsupplied_p_mw": 0, "max_loading_pct": 0}},
        {"case_id": "L21-22", "status": "limit_violation", "metrics": {"unsupplied_p_mw": 0, "max_loading_pct": 157.72}},
        {"case_id": "L01-02", "status": "islanded", "metrics": {"unsupplied_p_mw": 0, "max_loading_pct": 107}},
    ]
    ranked = sorted((case for case in cases if case["status"] != "not_converged"), key=severity_order)
    assert [case["case_id"] for case in ranked] == ["L21-22", "L01-02"]


def test_shortcircuit_display_derives_sk_and_rx_without_calculating():
    from power_machine.gui.panels.shortcircuit import presentation_quantities
    result = {"payload": {"fault": {"bus_id": "B1"}, "Ik_initial_sym_ka": 10.0,
                          "z_th_ohm": {"real": 2.0, "imag": 10.0}}}
    shown = presentation_quantities(result, {"buses": [{"id": "B1", "un_kv": 110.0}]})
    assert shown["payload"]["Sk_initial_mva"] > 0
    assert shown["payload"]["r_x_ratio"] == 0.2
