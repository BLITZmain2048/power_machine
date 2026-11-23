"""Review: theme coverage, detached study shortcuts and 720p legend."""
import tkinter as tk
from tkinter import ttk
from collections import Counter
import pytest
from power_machine.gui.theme import theme
from power_machine.gui.i18n import language,set_language,tr


@pytest.fixture
def app():
    from power_machine.gui.__main__ import create_application
    old_theme,old_code=theme.name,language()
    app=create_application(pause_s=.2)
    errors=[]
    app.report_callback_exception=lambda *error:errors.append(error)
    app.update()
    yield app
    app._closed=True
    app.manager.shutdown()
    app.close_parity_panels()
    app.destroy()
    theme.set(old_theme)
    set_language(old_code)
    assert not errors


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def unthemed_surfaces(root):
    """Audit the complete live tree, including each separately opened Toplevel."""
    allowed={root.winfo_rgb(color) for color in theme.palettes['dark'].values()}
    style=ttk.Style(root)
    failures=[]
    for widget in walk(root):
        for option in ('background','fieldbackground','troughcolor','highlightbackground'):
            value=None
            if isinstance(widget,ttk.Widget):
                name=widget.cget('style') or widget.winfo_class()
                if name in ('TScrollbar','TProgressbar','TScale'):
                    name=str(widget.cget('orient')).title()+'.'+name
                value=style.lookup(name,option,state=tuple(widget.state()))
                if option in widget.keys() and widget.cget(option):
                    value=widget.cget(option)
            elif option in widget.keys():
                value=widget.cget(option)
            if value and root.winfo_rgb(str(value)) not in allowed:
                failures.append((str(widget),widget.winfo_class(),option,str(value)))
    return failures


def test_theme_dark_application_and_each_open_toplevel(app):
    theme.set('dark')
    # Real panel and editor are deliberately created after selecting the theme.
    app.open_parity_panel('shortcircuit')
    app.events(dict(kind='short_circuit',target_id='B04'))
    extra=tk.Toplevel(app)
    extra.title('Theme coverage')
    native=(tk.LabelFrame,tk.Label,tk.Canvas,tk.Text,tk.Listbox,tk.Scrollbar,
            tk.Entry,tk.Spinbox,tk.PanedWindow,tk.Checkbutton,tk.Radiobutton,tk.Button)
    for factory in native:
        widget=factory(extra)
        if isinstance(widget,(tk.Text,tk.Listbox,tk.Canvas)):widget.configure(height=1)
        widget.pack()
    book=ttk.Notebook(extra)
    book.pack()
    page=ttk.Frame(book)
    book.add(page,text='Widgets')
    for factory in (ttk.LabelFrame,ttk.Treeview,ttk.Scrollbar,ttk.Spinbox,ttk.Combobox,ttk.Panedwindow,ttk.Progressbar):
        widget=factory(page)
        widget.pack()
    menu=tk.Menu(extra,tearoff=False)
    menu.add_command(label='Item')
    app.update()
    assert not unthemed_surfaces(app)
    # Constructing the Tcl popdown must also obtain themed defaults.
    combo=next(w for w in walk(extra) if isinstance(w,ttk.Combobox))
    popup=combo.tk.call('ttk::combobox::PopdownWindow',str(combo))
    assert combo.tk.call(str(popup)+'.f.l','cget','-background')==theme.color('surface')
    theme.set('light')
    theme.set('dark')
    app.update()
    assert not unthemed_surfaces(app)


def test_theme_audit_detects_a_new_white_surface(app):
    theme.set('dark')
    box=tk.Toplevel(app)
    text=tk.Text(box,height=2)
    text.pack()
    app.update()
    # A deliberate non-token factory color proves this audit actually fails.
    text.configure(background='white')
    assert any(row[0]==str(text) for row in unthemed_surfaces(app))


def test_shortcuts_from_detached_canvas_once_and_not_in_event_editor(app,monkeypatch):
    from power_machine.gui.shortcuts import install_study_shortcuts
    calls=Counter()
    for name in ('run_loadflow','initial_conditions','simulation_dialog','stop','save_case','undo','redo'):
        monkeypatch.setattr(app,name,lambda key=name:calls.update([key]))
    install_study_shortcuts(app)
    install_study_shortcuts(app)
    canvas=app.diagrams[None].canvas
    app.windows.window.deiconify()
    app.update()
    canvas.focus_force()
    app.update()
    for key in ('<F10>','<F11>','<F12>','<Escape>','<Control-s>','<Control-z>','<Control-y>'):
        canvas.event_generate(key)
        app.update()
    assert calls==Counter(dict(run_loadflow=1,initial_conditions=1,simulation_dialog=1,stop=1,save_case=1,undo=1,redo=1))
    # A modal form owns its editing keys and must not start another study.
    editor=tk.Toplevel(app)
    entry=ttk.Entry(editor)
    entry.pack()
    entry.focus_force()
    app.update()
    entry.event_generate('<F10>')
    app.update()
    assert calls['run_loadflow']==1
    editor.destroy()
    canvas.focus_force()
    app.update()
    canvas.event_generate('<F9>')
    app.update()
    assert app.focus_get().winfo_toplevel() is app
    app.event_generate('<F10>')
    app.update()
    assert calls['run_loadflow']==2


@pytest.mark.parametrize('code',['en','es'])
def test_diagram_legend_fits_detached_1280x720(app,code):
    set_language(code)
    window=app.windows.window
    window.geometry('1280x720+0+0')
    view=app.diagrams[None]
    view.set_color_mode('loading')
    view.set_contingency('L05-08',['L21-22','L13-14'])
    app.update()
    footer=view._footer
    assert footer.winfo_height()>=50
    assert footer.winfo_rooty()+footer.winfo_height()<=window.winfo_rooty()+window.winfo_height()
    for item in footer.find_all():
        left,top,right,bottom=footer.bbox(item)
        assert left>=0 and top>=0 and right<=footer.winfo_width() and bottom<=footer.winfo_height()
    text=' '.join(footer.itemcget(i,'text') for i in footer.find_all() if footer.type(i)=='text')
    assert all(label in text for label in tr('diagram.symbols_full').split('   '))
    view.toggle_legend()
    app.update()
    assert not footer.winfo_ismapped()
    view.toggle_legend()
    app.update()
    assert footer.winfo_ismapped()
