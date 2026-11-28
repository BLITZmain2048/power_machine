from gui_support import ieee_model, ieee_root
"""Pruebas dirigidas de presentación RMS study."""
import csv
import math

import numpy as np
import pytest

from power_machine.gui.plots import (
    TIME_KEY,
    SignalBuffer,
    axis_scale,
    cursor_difference,
    display_series,
    export_visible,
    nearest_sample,
    reference_availability,
    series_statistics,
    validate_plot_specs,
    xy_figure,
    xy_series,
)
from power_machine.gui.presentation import plot_events


FREQUENCY_KEY = 'Interface|m:fehz|Hz'
VOLTAGE_KEY = 'Interface|m:u|p.u.'


def make_buffer(rows):
    buffer = SignalBuffer()
    buffer.reset([TIME_KEY, FREQUENCY_KEY, VOLTAGE_KEY])
    buffer.append(rows)
    return buffer


def test_frequency_mode_is_explicit_and_does_not_modify_source_samples():
    x = np.array([0.0, 1.0])
    y = np.array([49.98, 50.02])
    absolute = display_series(FREQUENCY_KEY, x, y, 'absolute', 50.0)
    deviation = display_series(FREQUENCY_KEY, x, y, 'deviation', 50.0)
    assert absolute[2] == 'Hz'
    assert deviation[2] == 'Hz de desviación'
    assert np.allclose(deviation[1], [-.02, .02])
    assert np.allclose(y, [49.98, 50.02])
    with pytest.raises(ValueError, match='Modo de frecuencia'):
        display_series(FREQUENCY_KEY, x, y, 'relativo')


def test_reference_reports_unavailable_or_partial_signal_coverage():
    own = make_buffer([[0, 50, 1]])
    reference = SignalBuffer()
    reference.reset([TIME_KEY, FREQUENCY_KEY])
    reference.append([[0, 50]])
    assert reference_availability(None, [FREQUENCY_KEY]) == (False, 'No se cargó una curva de referencia externa')
    assert reference_availability(reference, [VOLTAGE_KEY])[0] is False
    available, notice = reference_availability(reference, [FREQUENCY_KEY, VOLTAGE_KEY])
    assert available and 'no contiene todas' in notice
    assert own.version > 0


def test_statistics_nearest_sample_and_double_cursor_preserve_missing_values():
    x = np.array([0.0, .1, .2, .3])
    y = np.array([1.0, math.nan, .9, 1.1])
    assert series_statistics(x, y) == {'minimum': .9, 'maximum': 1.1, 'final': 1.1, 'samples': 3}
    assert nearest_sample(x, y, .12) == (.2, .9)
    assert nearest_sample(x, y, math.nan) is None
    first = {'time_s': .1, 'values': {VOLTAGE_KEY: 1.0, FREQUENCY_KEY: math.nan}}
    second = {'time_s': .3, 'values': {VOLTAGE_KEY: .96, FREQUENCY_KEY: 50.0}}
    delta = cursor_difference(first, second)
    assert delta['delta_time_s'] == pytest.approx(.2)
    assert delta['delta_values'][VOLTAGE_KEY] == pytest.approx(-.04)


def test_xy_chart_uses_only_finite_pairs_and_black_line():
    buffer = make_buffer([[0, 50, 1], [.1, None, 1.01], [.2, 49.9, 1.02]])
    x, y = xy_series(buffer, FREQUENCY_KEY, VOLTAGE_KEY)
    assert np.allclose(x, [50, 49.9]) and np.allclose(y, [1, 1.02])
    figure = xy_figure(buffer, FREQUENCY_KEY, VOLTAGE_KEY)
    line = figure.axes[0].lines[0]
    from power_machine.gui.theme import theme
    assert line.get_color() == theme.color('text')
    assert figure.axes[0].get_xlabel() == 'm:fehz [Hz]'


def test_manual_scales_and_extended_specs_are_checked():
    spec = {
        'name': 'RMS', 'axes': 2,
        'curves': [{'key': FREQUENCY_KEY, 'axis': 0}, {'key': VOLTAGE_KEY, 'axis': 1}],
        'frequency_mode': 'deviation', 'frequency_base_hz': 50.0,
        'events': [{'time_s': .1, 'label': 'Falla'}],
        'axis_scales': {'0': {'xlim_s': [0, 1], 'ylim': [-.5, .5]}},
    }
    validate_plot_specs([spec], {FREQUENCY_KEY, VOLTAGE_KEY})
    assert axis_scale(spec, 0) == ((0.0, 1.0), (-.5, .5))
    spec['axis_scales']['0']['ylim'] = [1, 1]
    with pytest.raises(ValueError, match='crecientes'):
        validate_plot_specs([spec], {FREQUENCY_KEY, VOLTAGE_KEY})


def test_events_and_full_csv_export_keep_all_rows_and_empty_cells(tmp_path):
    class Event:
        time_s = .15
        kind = 'fault'
        target_id = 'ref_interface'
    assert plot_events([Event(), {'time_s': .2, 'kind': 'open'}]) == [
        {'time_s': .15, 'label': 'fault · ref_interface'}, {'time_s': .2, 'label': 'open'}]
    rows = [[index / 1000, 50 + index / 1e6, None if index == 1500 else 1.0] for index in range(3001)]
    buffer = make_buffer(rows)
    path = tmp_path / 'rms.csv'
    export_visible(path, buffer, [FREQUENCY_KEY, VOLTAGE_KEY])
    with path.open(encoding='utf-8-sig', newline='') as stream:
        exported = list(csv.DictReader(stream))
    assert len(exported) == 6002
    assert exported[4501]['Valor'] == ''
