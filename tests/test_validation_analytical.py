"""Fast validation tests. The full offline campaign is a separate command."""
from __future__ import annotations

import json
from pathlib import Path

from validation.analytical import run_cases


ROOT = Path(__file__).resolve().parents[1]


def test_closed_form_electrical_cases_are_strict():
    tolerances = json.loads((ROOT / "validation" / "tolerances.json").read_text(encoding="utf-8"))
    results = run_cases(tolerances["analytical"])
    assert len(results) == 7
    assert all(result["pass"] for result in results)


def test_capability_matrix_keeps_classical_and_native_rms_separate():
    text = (ROOT / "validation" / "CAPABILITIES.md").read_text(encoding="utf-8")
    assert "Classical RMS IEEE 39" in text
    assert "Native RMS IEEE 39" in text
    assert "It has **no** PF comparison label" in text


def test_included_ieee39_loadflow_reference_is_comparable():
    """Small external-reference subset kept separate from the full campaign."""
    from power_machine.benchmarks.ieee39_pf import DEFAULT_PF_DIR, LDF_OPTIONS, build_model
    from power_machine.loadflow import run_loadflow
    from power_machine.pfcompare.lf import compare_results

    model = build_model(DEFAULT_PF_DIR)
    comparison = compare_results(model, run_loadflow(model, options=LDF_OPTIONS), DEFAULT_PF_DIR / "E39-LF-01")
    assert comparison["pass"]
    assert {"buses", "branches", "generators", "reference", "totals"} <= set(comparison["counts"])

