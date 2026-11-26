"""Regression checks for navigation and constrained v3 workspaces."""
from tkinter import ttk
from test_gui_shell_windows import shell
from power_machine.gui.i18n import set_language, tr
from power_machine.gui.theme import theme
from power_machine.gui.start_page import reopen_start_page


def test_start_has_own_view_and_help_reopens_it(shell):
    app=shell
    page=reopen_start_page(app)
    app.update()
    assert app.analysis.results.select()==str(app.analysis.start_host)
    assert not app.analysis.time_cursor.winfo_ismapped()
    assert not app.analysis.curve_events.winfo_ismapped()
    assert page.body.winfo_width()<=1200
    assert page.buttons['execute'].cget('style')=='Run.TButton'
    assert set(app.menu_bar.buttons)==set(app.menus)
    assert 'indicator' not in str(ttk.Style(app).layout('Chrome.TMenubutton')).lower()
    app.book.select(app.plot_pages[0])
    app.update()
    assert app.analysis.time_cursor.winfo_ismapped()
    assert reopen_start_page(app) is page


def test_short_tab_row_hides_overflow_navigation(shell):
    app=shell
    app.geometry('1904x1040')
    app.book.select(app.plot_pages[0])
    app.update()
    book=app.analysis.books['curves']
    book.layout_tabs()
    app.update()
    assert not book.previous_button.winfo_manager()
    assert not book.next_button.winfo_manager()
    assert not book.list_button.winfo_manager()
    assert len(book.buttons)==len(book.tabs())
    extra=ttk.Frame(app.book)
    app.book.add(extra,text='Long result tab title '*30,area='curves')
    app.book.select(extra)
    app.update()
    book.layout_tabs()
    app.update()
    assert book.previous_button.winfo_manager()
    assert book.next_button.winfo_manager()
    assert book.list_button.winfo_manager()
    assert book.list_menu.index('end')+1==len(book.tabs())


def test_events_scroll_and_localized_times_at_small_window(shell):
    app=shell
    app.geometry('1264x680')
    editor=app.analysis.show_events()
    editor.rows=[dict(time_s=1.05,kind='short_circuit',target_id='B16',params=dict(r_ohm=0.,x_ohm=0.))]
    for code in ('es','en'):
        set_language(code)
        editor.refresh()
        app.update()
        assert editor.tree.item('0','values')[0]==('1,050' if code=='es' else '1.050')
        assert editor.apply_button.winfo_ismapped()
        for button in editor.action_bar.winfo_children():
            assert button.winfo_height()>=button.winfo_reqheight()
            assert button.winfo_y()+button.winfo_height()<=editor.action_bar.winfo_height()
        assert editor.form_scroll.canvas.winfo_height()>25
        editor.form_scroll.reveal(editor.clearing_entry)
        app.update()
        y=editor.clearing_entry.winfo_rooty()-editor.form_scroll.canvas.winfo_rooty()
        assert y>=0
        assert y+editor.clearing_entry.winfo_height()<=editor.form_scroll.canvas.winfo_height()+1


def test_network_mode_and_central_scale_theme(shell):
    app=shell
    view=app.diagrams[None]
    red=app.windows.window.workspace
    red.documents.select(red.single_line)
    app.update()
    assert red.mode.get()==tr('v3.int.mode.view')
    view.set_edit_mode(True)
    assert red.mode.get()==tr('v3.int.mode.edit')
    view.set_edit_mode(False)
    for name in ('light','dark'):
        theme.set(name)
        style=ttk.Style(app)
        assert style.lookup('Horizontal.TScale','background')==theme.color('bg')
        assert style.lookup('Horizontal.TScale','troughcolor')==theme.color('soft')
