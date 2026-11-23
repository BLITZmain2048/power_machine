from power_machine.gui.theme import theme
from power_machine.gui.i18n import tr
from gui_support import ieee_model, ieee_root
"""Contratos de presentación, geometría y conservación de datos study."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from power_machine.gui.diagram.layout import OFF_COLOR, build_scene, diagram_groups
from power_machine.gui.diagram.render import wrap_pixels, wire_power
from power_machine.gui.diagram.view import DiagramView, result_text

from gui_support import ROOT


def test_words_and_numeric_units_are_never_split():
    font = SimpleNamespace(measure=lambda s: len(s) * 7)
    assert wrap_pixels('P -244.68 MW\nÁngulo -12.535 °', font, 15) == 'P\n-244.68\nMW\nÁngulo\n-12.535\n°'


def test_ieee_mesh_is_compact_orthogonal_and_preserves_model():
    model = json.loads((ROOT / 'cases/ieee39/model.json').read_text(encoding='utf-8-sig'))
    original = copy.deepcopy(model)
    group = diagram_groups(model)[0]['id']
    scene = build_scene(model, group, result_boxes=True)
    buses = [s for s in scene.symbols.values() if s.kind == 'bus']
    assert len(buses) == 39
    assert len({s.y for s in buses}) > 2
    assert len({(s.x, s.y) for s in buses}) == 39
    assert scene.bounds[2] - scene.bounds[0] < 2200
    assert all(a[0] == b[0] or a[1] == b[1] for w in scene.wires for a, b in zip(w.points, w.points[1:]))
    assert {b['id'] for b in model['branches']} <= {w.id for w in scene.wires}
    assert model == original
    repeated = build_scene(model, group, result_boxes=True)
    assert [(s.x, s.y) for s in scene.symbols.values()] == [(s.x, s.y) for s in repeated.symbols.values()]


def test_format_end_units_zero_absence_and_loading():
    symbol = SimpleNamespace(kind='branch', item={'_local_bus': 'b'})
    result = dict(bus_from='a', bus_to='b', p_from_mw=10, q_from_mvar=3,
                  p_to_mw=0, q_to_mvar=None, loading_pct=87.25)
    text = result_text(symbol, result, digits={'p_to_mw': 2}, include_loading=True)
    assert text == 'P 0.00 MW\nQ — Mvar\n' + tr('diagram.loading') + ' 87.2 %'
    result.pop('loading_pct')
    assert tr('diagram.loading') + ' — %' in result_text(symbol, result, include_loading=True)
    bus = SimpleNamespace(kind='bus', item={})
    assert '—' in result_text(bus, dict(u_pu=float('nan')))


@pytest.mark.parametrize('mode,value,expected', [
    ('voltage_band', .9, theme.color('error')), ('voltage_band', 1, theme.color('ok')),
    ('loading', 0, theme.color('ok')), ('loading', 95, theme.color('warn')), ('loading', 101, theme.color('error')),
    ('loading', None, OFF_COLOR),
])
def test_color_modes_use_only_motor_values(mode, value, expected):
    view = SimpleNamespace(color_mode=mode, voltage_limits=(.95, 1.05))
    result = {('u_pu' if mode == 'voltage_band' else 'loading_pct'): value}
    assert DiagramView.element_color(view, {}, result, 220) == expected
    assert DiagramView.element_color(view, {'outserv': 1}, result, 220) == OFF_COLOR
    assert DiagramView.element_color(view, {}, dict(result, energized=False), 220) == OFF_COLOR


def test_existing_public_api_is_retained():
    for method in ('set_operating_state', 'set_loadflow_results', 'set_result_boxes_visible',
                   'highlight', 'fit', 'export_postscript'):
        assert callable(getattr(DiagramView, method))


def test_arrows_respect_both_ends_and_missing_data():
    row = dict(bus_from='a', bus_to='b', p_from_mw=10, p_to_mw=-9)
    assert wire_power(SimpleNamespace(start='a', end='equipment'), row) == 10
    assert wire_power(SimpleNamespace(start='equipment', end='b'), row) == 9
    assert wire_power(SimpleNamespace(start='b', end='equipment'), row) == -9
    assert wire_power(SimpleNamespace(start='unknown', end='equipment'), row) is None


def test_three_winding_end_is_explicit():
    symbol = SimpleNamespace(kind='transformer3', item={'_local_bus': 'low'})
    row = dict(bus_hv='high', bus_mv='medium', bus_lv='low', p_hv_mw=10,
               p_lv_mw=-4, q_lv_mvar=0)
    text = result_text(symbol, row)
    assert text == 'P -4.0 MW\nQ 0.0 Mvar'
