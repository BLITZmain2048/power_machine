"""Two-window shell integration, using the existing three-attempt Tk policy."""
import ast
import json
from pathlib import Path
import tkinter as tk
import pytest
from power_machine.gui.i18n import language, set_language, tr
from power_machine.gui.theme import theme


@pytest.fixture
def shell(tmp_path, monkeypatch):
    from power_machine.gui.__main__ import create_application
    monkeypatch.setenv('POWER_MACHINE_CONFIG_DIR', str(tmp_path/'config'))
    old_language, old_theme = language(), theme.name
    try:
        app = create_application(pause_s=.2)
    except RuntimeError as exc:
        pytest.skip(f'Tk unavailable after three attempts: {exc}')
    errors = []
    app.report_callback_exception = lambda *exc: errors.append(exc)
    assert app.document is not None
    yield app
    app._closed = True
    app.manager.shutdown()
    app.close_parity_panels()
    app.destroy()
    set_language(old_language)
    theme.set(old_theme)
    assert not errors


def test_detached_hide_reopen_and_dock(shell):
    app = shell
    view = app.diagrams[None]
    assert view.winfo_toplevel() is app.windows.window
    assert str(view) not in app.book.tabs()
    assert not hasattr(app.windows.window, 'fit_button')
    assert not hasattr(app.windows.window, 'toolbar')
    app.update()
    view.status.set('123, 456 · B04 · 100 %')
    assert str(app.windows.window.status_label.cget('textvariable')) == str(view.status)
    app.windows.hide_diagram()
    assert app.windows.window.state() == 'withdrawn'
    app.open_diagram()
    assert app.diagrams[None] is view
    view.positions['B01'] = (121.,342.)
    app.windows.dock_diagram(True)
    assert app.diagrams[None].winfo_toplevel() is app
    assert app.diagrams[None].positions['B01'] == (121.,342.)
    app.windows.dock_diagram(False)
    assert app.diagrams[None].winfo_toplevel() is app.windows.window


def test_selection_open_and_rms_requests(shell, monkeypatch):
    app = shell
    view = app.diagrams[None]
    view.select_ids(['B01','B02'])
    assert app.selected_id == 'B01'
    assert view.selected_ids == {'B01','B02'}
    app.select('B03')
    assert view.selected_ids == {'B03'}
    opened, events = [], []
    monkeypatch.setattr(app, 'properties', opened.append)
    monkeypatch.setattr(app, 'events', lambda preset:events.append(preset))
    app.bus.publish('element.open', id='B02')
    app.bus.publish('study.event.requested', kind='fault', target_id='B02')
    app.bus.publish('study.event.requested', kind='disconnect', target_id=app.model['branches'][0]['id'])
    assert opened == ['B02']
    assert events[0] == dict(kind='short_circuit',target_id='B02')
    assert events[1]['kind'] == 'switch_element'
    assert events[1]['action'] == 'open'


def test_live_theme_language_and_stable_menu_keys(shell):
    app = shell
    assert set(app.menus) == {'file','edit','view','calculation','rms','plots','tools','window','help'}
    view = app.diagrams[None]
    assert str(app.cget('menu')) == ''
    assert set(app.menu_bar.buttons)==set(app.menus)
    assert all(str(button.cget('menu'))==str(app.menus[key]) for key,button in app.menu_bar.buttons.items())
    original_name = app.document.study.name
    for name in ('dark','light'):
        theme.set(name)
        assert app.cget('background') == theme.color('bg')
        assert view.canvas.cget('background') == theme.color('diagram_bg')
    from power_machine.loadflow import run_loadflow
    app.loadflow_done(run_loadflow(app.model))
    for code in ('en','es'):
        set_language(code)
        assert app.book.tab(app.table_pages[0],'text') == 'LF · '+tr('shell.busbars')
        assert app.menu.entrycget(0,'label') == tr('menu.file')
        assert app.case_label.get() == tr('shell.ieee39_base_case')
        assert app.document.study.name == original_name
        assert app.windows.window.title() == tr('v3.window.network')+' · '+app.case_label.get()+' · Power Machine'
        assert app.diagrams[None] is view
    app.document.study.name = 'User study · caso base'
    for code in ('en','es'):
        set_language(code)
        assert app.case_label.get() == 'User study · caso base'
    assert app.bind_all('<F9>') and '<F9>' in app._study_shortcut_bindings


def test_owned_catalogue_keys_and_no_literal_colours():
    from power_machine.gui import i18n
    root = Path(i18n.__file__).parent
    names = ['app.py','app_shell.py','app_views.py','app_workspace.py','app_data_views.py',
             'app_jobs.py','display.py','toolbar_icons.py','panel_context.py','coordination.py',
             'windows.py','__main__.py']
    catalogues = [json.loads((root/'locale'/f'{code}.json').read_text(encoding='utf8')) for code in ('en','es')]
    import re
    for name in names:
        source = (root/name).read_text(encoding='utf8')
        assert not re.search(r'#[0-9a-fA-F]{6}\b',source), name
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id == 'tr':
                if node.args and isinstance(node.args[0],ast.Constant):
                    assert all(node.args[0].value in catalogue for catalogue in catalogues), (name,node.lineno)
