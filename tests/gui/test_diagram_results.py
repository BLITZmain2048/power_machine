from gui_support import ieee_model, ieee_root
"""Regresiones del estudio study sobre el modelo real."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from power_machine.gui.diagram.layout import build_scene, diagram_groups, terminals
from power_machine.gui.diagram.view import result_text

from gui_support import ROOT


@pytest.mark.parametrize('state', [
    {'energized': False}, {'status': 'sin_energia_pasiva'},
    {'status': 'ambos_puertos_abiertos'}, {'status': 'sin_referencia'},
    {'status': 'fuera_de_servicio'},
])
def test_deenergized_results_are_a_dash(state):
    for kind in ('bus', 'branch', 'transformer', 'outgoing'):
        symbol = SimpleNamespace(kind=kind, item={})
        result = dict(p_from_mw=0.0, q_from_mvar=0.0, u_pu=0.0, **state)
        assert result_text(symbol, result) == '—'


def test_energized_zero_flow_is_still_numeric():
    symbol = SimpleNamespace(kind='branch', item={})
    for status in ('resuelto', 'malla_no_unica', 'extremo_abierto'):
        text = result_text(symbol, dict(status=status, p_from_mw=0.0, q_from_mvar=0.0))
        assert text == 'P 0.0 MW\nQ 0.0 Mvar'


def test_out_of_service_overrides_old_numeric_results():
    symbol = SimpleNamespace(kind='branch', item={})
    result = dict(status='resuelto', p_from_mw=15.0, q_from_mvar=3.0)
    assert result_text(symbol, result, item={'outserv': 1}) == '—'


def test_overview_has_exactly_one_trace_per_connected_pair():
    model = json.loads((ROOT / 'cases/ieee39/model.json').read_text(encoding='utf-8-sig'))
    by_bus = {b: g['id'] for g in diagram_groups(model) for b in g['bus_ids']}
    pairs = set()
    for category in ('branches', 'branches3', 'switches'):
        for item in model[category]:
            groups = sorted({by_bus[b] for b in terminals(item) if b in by_bus})
            for i, a in enumerate(groups):
                for b in groups[i + 1:]:
                    pairs.add((a, b))
    scene = build_scene(model)
    assert len(scene.wires) == len(pairs)
    assert {tuple(sorted((w.start, w.end))) for w in scene.wires} == pairs
    assert all(len(w.points) <= 3 for w in scene.wires)
    # Una traza no puede desaparecer por detrás de una caja intermedia.
    for wire in scene.wires:
        for a, b in zip(wire.points, wire.points[1:]):
            for n in range(1, 200):
                x, y = a[0] + (b[0] - a[0]) * n / 200, a[1] + (b[1] - a[1]) * n / 200
                for key, symbol in scene.symbols.items():
                    left, top, right, bottom = symbol.bbox
                    assert not (left + 1 < x < right - 1 and top + 1 < y < bottom - 1), (wire.id, key)


def test_only_usage_zero_terminals_are_busbars():
    model = json.loads((ROOT / 'cases/ieee39/model.json').read_text(encoding='utf-8-sig'))
    for group in diagram_groups(model):
        scene = build_scene(model, group['id'])
        for bus in model['buses']:
            if bus['id'] in group['bus_ids']:
                symbol = scene.symbols[bus['id']]
                assert symbol.kind == ('bus' if bus['params'].get('iUsage', 0) == 0 else 'node')
