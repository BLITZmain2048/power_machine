"""Contratos de filtros, selección, tema y superposición de resultados."""
import csv
from types import SimpleNamespace

from power_machine.gui.bus import bus
from power_machine.gui.theme import theme
from power_machine.gui.plots import SignalBuffer, PlotPage, TIME_KEY, export_visible
from power_machine.gui.results_tables import ResultsTable
from power_machine.gui.output import OutputPanel

KEY = 'B1|m:u|p.u.'


def samples(rows):
    data = SignalBuffer()
    data.reset([TIME_KEY, KEY])
    data.append(rows)
    return data


def test_filter_violation_and_selection_bus(tk_root):
    table = ResultsTable(tk_root, 'buses', [dict(id='B1', name='Bus 1', u_pu=.9),
                                           dict(id='B2', name='Bus 2', u_pu=1.)])
    table.violations_only.set(True)
    table.populate()
    assert [r['object_id'] for r in table.visible_rows] == ['B1']
    table.text_filter.set('Bus 2')
    assert not table.visible_rows
    table.text_filter.set('')
    bus.publish('selection.changed', ids=['B1'], source='diagram')
    assert table.tree.selection() == ('B1',)
    captured = []
    token = bus.subscribe('selection.changed', lambda **payload: captured.append(payload))
    table._publish_selection()
    assert captured[-1] == dict(ids=['B1'], source='results')
    bus.unsubscribe(token)


def test_live_theme_overlay_cursor_and_vector_export(tk_root, tmp_path):
    own, ref, other = samples([[0, 1], [1, .9]]), samples([[0, 1.01], [.7, .91]]), samples([[0, 1.02], [2, .92]])
    page = PlotPage(tk_root, dict(axes=1, curves=[dict(key=KEY)], show_pf=True), own, ref)
    page.add_run('second', other, 'Second run')
    assert len(page.axes[0].lines) == 3
    page.motion(SimpleNamespace(inaxes=page.axes[0], xdata=.8))
    assert 'PF' in page.cursor.get() and '0.91' in page.cursor.get()
    old = theme.name
    try:
        theme.set('dark')
        from matplotlib.colors import to_rgba
        assert page.figure.get_facecolor() == to_rgba(theme.color('surface'))
        page.figure.savefig(tmp_path / 'plot.svg')
        assert '<svg' in (tmp_path / 'plot.svg').read_text('utf-8')
        page.remove_run('second')
        assert len(page.axes[0].lines) == 2
    finally:
        theme.set(old)


def test_export_retains_independent_grids_and_full_precision(tmp_path):
    path = tmp_path / 'runs.csv'
    own, other = samples([[0, 1.123456789], [1, .9]]), samples([[.25, .8], [2, .7]])
    export_visible(path, own, [KEY], overlays=[('Second', other)])
    with path.open(encoding='utf-8-sig', newline='') as stream:
        rows = list(csv.reader(stream))
    assert rows[1][3] == '1.123456789'
    assert rows[3][:2] == ['Second', '0.25']


def test_output_theme_and_level_filter(tk_root):
    panel = OutputPanel(tk_root)
    panel.log('INFO', 'Ordinary')
    panel.log('ERROR', 'Failed')
    panel.level_filter.set('ERROR')
    assert 'Failed' in panel.text.get('1.0', 'end')
    assert 'Ordinary' not in panel.text.get('1.0', 'end')
    panel._apply_theme()
    assert panel.text.tag_cget('ERROR', 'foreground') == theme.color('error')


def test_owned_translation_keys_exist_in_both_catalogues():
    import ast
    import json
    from pathlib import Path
    from power_machine.gui import plots
    directory = Path(plots.__file__).parent
    keys = set()
    for name in ('plots.py', 'results_tables.py', 'output.py', 'app_compare.py', 'study_report.py'):
        for node in ast.walk(ast.parse((directory / name).read_text('utf-8'))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'tr'
                    and node.args and isinstance(node.args[0], ast.Constant)):
                keys.add(node.args[0].value)
    for code in ('en', 'es'):
        catalogue = json.loads((directory / 'locale' / (code + '.json')).read_text('utf-8'))
        assert keys <= catalogue.keys()


def test_live_language_updates_table_labels(tk_root):
    from power_machine.gui.i18n import language, set_language
    previous = language()
    table = ResultsTable(tk_root, 'buses', [dict(id='B1', name='Bus 1', u_pu=1.)])
    try:
        set_language('es')
        assert table.tree.heading('name', 'text') == 'Barra'
        set_language('en')
        assert table.tree.heading('name', 'text') == 'Busbar'
    finally:
        set_language(previous)


def test_navigation_toolbar_initial_dark_theme_and_live_switch(tk_root):
    previous = theme.name
    try:
        theme.set('dark')
        page = PlotPage(tk_root, dict(axes=1, curves=[dict(key=KEY)]), samples([[0, 1], [1, .9]]))
        for button in page.toolbar._buttons.values():
            assert button.cget('background') == theme.color('surface')
            assert str(button.cget('image')) == str(button._ntimage_alt)
        theme.set('light')
        for button in page.toolbar._buttons.values():
            assert button.cget('background') == theme.color('surface')
            assert str(button.cget('image')) == str(button._ntimage)
    finally:
        theme.set(previous)


def test_output_all_and_warning_filters_survive_language_change(tk_root):
    from power_machine.gui.i18n import language, set_language
    previous = language()
    try:
        set_language('en')
        panel = OutputPanel(tk_root)
        assert panel.level_filter.get() == 'All'
        panel.log('WARNING', 'Warning detail')
        panel.log('INFO', 'Normal detail')
        panel.level_filter.set('WARNING')
        assert 'Normal detail' not in panel.text.get('1.0', 'end')
        set_language('es')
        assert panel.level_filter.get() == 'AVISO'
        assert 'Warning detail' in panel.text.get('1.0', 'end')
        set_language('en')
        assert panel.level_filter.get() == 'WARNING'
    finally:
        set_language(previous)
