"""Guided start integration: no engine calculations are needed."""
from test_gui_shell_windows import shell
from power_machine.gui.start_page import show_start_page, reopen_start_page, PREFERENCE
from power_machine.gui.settings import Settings
from power_machine.gui.i18n import set_language, tr
from power_machine.gui.theme import theme


def test_start_construction_and_locales(shell):
    page = shell.welcome_page
    assert page is show_start_page(shell)
    assert page.workspace_area == 'start'
    assert shell.analysis.results.select()==str(shell.analysis.start_host)
    assert tuple(page.cards) == ('case', 'study', 'run', 'results')
    assert '39' in page.case_summary.cget('text')
    for code in ('es', 'en'):
        set_language(code)
        assert shell.book.tab(page, 'text') == tr('v3.start.title')
        assert page.buttons['flow'].cget('text') == tr('v3.start.flow')
    for name in ('light', 'dark'):
        theme.set(name)
        from tkinter import ttk
        assert ttk.Style(page).lookup('Start.Card.TFrame', 'background') == theme.color('paper')


def test_start_event_states_and_case_switch(shell):
    page = shell.welcome_page
    assert page.states['results'] == 'pending'
    shell.bus.publish('results.updated', study='loadflow', result_id='test')
    assert page.states['results'] == 'ready'
    shell.bus.publish('network.edited', ids=['B01'])
    assert page.states['results'] == 'stale'
    shell.bus.publish('results.updated', study='loadflow', result_id='test')
    shell.bus.publish('results.stale', reason='test')
    assert page.states['results'] == 'stale'
    shell.bus.publish('selection.changed', ids=['B02'], source='test')
    assert page.states['case'] == 'ready'
    # Same public variable path used when activating a case in the shell.
    old = shell.document.active_case_id
    page._case = (id(shell.document), old + '-previous')
    shell.case_label.set(shell.case_label.get())
    assert page.states['results'] == 'pending'


def test_start_study_run_and_navigation(shell, monkeypatch):
    page = shell.welcome_page
    for key in ('flow', 'shortcircuit', 'contingency', 'rms'):
        page.buttons[key].invoke()
        assert shell.analysis.active_study == key
        assert shell.analysis.study_tree.selection() == (key,)
    calls = []
    monkeypatch.setattr(shell.analysis, 'run_active', lambda: calls.append('run'))
    page.buttons['execute'].invoke()
    assert calls == ['run']
    assert page.buttons['events'].grid_info()
    page.buttons['events'].invoke()
    assert shell.analysis.event_editor is not None
    page.show_results('log')
    assert shell.analysis.results.select() == str(shell.analysis.result_hosts['log'])
    page.choose_study('flow')
    assert not page.buttons['events'].grid_info()


def test_start_preference_persists_and_explicit_reopen(shell):
    page = shell.welcome_page
    page.hidden.set(True)
    page.save_preference()
    assert Settings(shell.settings.path).get(PREFERENCE) is True
    assert show_start_page(shell) is None
    assert reopen_start_page(shell) is page
    page.destroy()
    assert show_start_page(shell) is None
    new = reopen_start_page(shell)
    assert new is shell.welcome_page and new is not page
    assert new.hidden.get()
    new.hidden.set(False)
    new.save_preference()
    assert Settings(shell.settings.path).get(PREFERENCE) is False


def test_start_small_window_keeps_preference_visible_and_scrolls(shell):
    page = shell.welcome_page
    shell.geometry('1264x664')
    shell.update()
    assert page.preference.winfo_rooty()+page.preference.winfo_height() <= shell.winfo_rooty()+shell.winfo_height()
    page.buttons['network'].focus_force()
    shell.update()
    top = page.canvas.winfo_rooty()
    bottom = top+page.canvas.winfo_height()
    button = page.buttons['network']
    assert top <= button.winfo_rooty() < bottom
    # Case changes and result events cannot call a destroyed page.
    page.destroy()
    shell.bus.publish('results.updated', study='loadflow', result_id='after-close')
    shell.case_label.set(shell.case_label.get())
