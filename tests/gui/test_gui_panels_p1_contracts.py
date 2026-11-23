"""Contratos de presentación e idioma para los paneles P1."""
import json
from pathlib import Path
import re

from power_machine.gui.panels import calc_lfctl, calc_pvqv, calc_sens


ROOT = Path(__file__).resolve().parents[2]


def test_p1_panel_titles_publish_stable_translation_keys():
    specs = (calc_lfctl.PANEL_SPEC, calc_sens.PANEL_SPEC, calc_pvqv.PANEL_SPEC)
    assert {spec["title_key"] for spec in specs} == {
        "panel.loadflow.title", "panel.sensitivity.title", "panel.pvqv.title"}


def test_p1_catalogue_keys_exist_in_both_languages():
    locale = ROOT / "power_machine" / "gui" / "locale"
    catalogues = {code: json.loads((locale / f"{code}.json").read_text(encoding="utf8")) for code in ("en", "es")}
    expected_prefixes = ("panel.common.", "panel.loadflow.", "panel.sensitivity.", "panel.pvqv.")
    keys = {key for key in catalogues["en"] if key.startswith(expected_prefixes)}
    assert keys
    assert keys <= set(catalogues["es"])


def test_p1_sources_have_no_literal_hex_colours():
    panels = ROOT / "power_machine" / "gui" / "panels"
    for name in ("panel_widgets.py", "calc_lfctl.py", "calc_sens.py", "calc_pvqv.py"):
        assert not re.search(r"#[0-9a-fA-F]{3,8}\\b", panels.joinpath(name).read_text(encoding="utf8"))
