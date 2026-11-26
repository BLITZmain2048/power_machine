"""Two engineering workspaces keep editing, calculation and selection connected."""
from tkinter import ttk
from test_gui_shell_windows import shell
from power_machine.gui.i18n import set_language,tr
from power_machine.gui.theme import theme


def test_network_layout_focus_and_shared_selection(shell):
    app=shell
    red=app.windows.window.workspace
    assert len(red.panes.panes())==3
    assert red.documents.tab(red.single_line,'text')==tr('v3.single_line')
    assert red.documents.tab(red.equipment_host,'text')==tr('v3.equipment')
    assert app.data_manager.winfo_toplevel() is app.windows.window
    assert app.properties_dock.winfo_toplevel() is app.windows.window
    app.data_manager.tree.selection_set('B04')
    app.data_manager._tree_selected()
    assert app.selected_id=='B04'
    assert app.diagrams[None].selected_ids=={'B04'}
    assert app.properties_dock.editor.item['id']=='B04'
    set_language('en')
    assert app.data_manager.tree.selection()==('B04',)
    red.focus_var.set(True)
    red.toggle_focus()
    assert len(red.panes.panes())==1
    red.focus_var.set(False)
    red.toggle_focus()
    assert len(red.panes.panes())==3


def test_analysis_regions_route_existing_pages_and_events(shell,monkeypatch):
    app=shell
    analysis=app.analysis
    assert tuple(analysis.result_hosts)==('curves','events','tables','log')
    assert app.output.master is analysis.result_hosts['log']
    assert str(app.plot_pages[0]) in analysis.books['curves'].tabs()
    dialog=app.events(dict(kind='short_circuit',target_id='B04'))
    assert isinstance(dialog,ttk.Frame)
    assert dialog.winfo_toplevel() is app
    assert not dialog.include_clearing.get()
    dialog.include_clearing.set(True)
    dialog.time.set('0.1')
    dialog.clearing_time.set('0.2')
    assert dialog.store(False)
    assert len(dialog.rows)==2
    assert app.events() is dialog
    app.open_parity_panel('modal')
    frame,context=app.parity_panels['modal']
    assert context.panel._analysis_external
    assert context.panel.form_host.master is analysis.options_host
    assert 'run' in context.panel._action_sources
    assert str(frame) in analysis.books['tables'].tabs()
    app.book.select(frame)
    assert analysis.active_study=='modal'
    called=[]
    context.panel._action_sources['run'].configure(command=lambda:called.append('run'))
    context.panel.action_buttons['run'].state(['!disabled'])
    analysis.run_active()
    assert called==['run']
    app.book.select(app.plot_pages[0])
    assert app.current_tab() is app.plot_pages[0]


def test_v3_style_contract_live_themes_and_locales(shell):
    app=shell
    style=ttk.Style(app)
    for name in ('light','dark'):
        theme.set(name)
        for token in ('panel','paper','soft','line','ink','muted','accent2','sky','select','alert','kv_345','v_ok','load_100'):
            assert theme.color(token)
        for name in ('Toolbar.TButton','Run.TButton','PanelHeader.TLabel','Explorer.Treeview',
                     'Inspector.Code.TLabel','Status.Mode.TLabel','Titlebar.Brand.TLabel'):
            assert style.lookup(name,'background')
        assert 'Doc.stripe' in str(style.layout('Doc.TNotebook.Tab'))
        assert 'Results.stripe' in str(style.layout('Results.TNotebook.Tab'))
        assert style.lookup('Titlebar.TFrame','background')==theme.color('titlebar')
    for code in ('en','es'):
        set_language(code)
        from power_machine.gui.results_tables import display_value,format_value
        assert display_value(1.2345,'u_pu')==('1,2345' if code=='es' else '1.2345')
        assert format_value(1.2345,'u_pu')=='1.2345'
        assert app.analysis.results.tab(app.analysis.result_hosts['events'],'text')==tr('v3.tab.events')
        assert app.windows.window.workspace.documents.tab(0,'text')==tr('v3.single_line')
        assert app.case_caption.cget('text')==tr('v3.case')


def test_shared_chrome_menu_status_and_titles(shell):
    app=shell
    assert not str(app.cget('menu'))
    assert set(app.menu_bar.buttons)==set(app.menus)
    red=app.windows.window
    assert set(red.menu_bar.buttons)==set(app.menus)
    for code in ('en','es'):
        set_language(code)
        assert app.title()==tr('v3.window.analysis')+' · '+app.case_label.get()+' · Power Machine'
        assert red.title()==tr('v3.window.network')+' · '+app.case_label.get()+' · Power Machine'
        for key,button in app.menu_bar.buttons.items():
            assert str(button.cget('menu'))==str(app.menus[key])
            assert button.cget('text')==tr('menu.'+key)
    assert app.progress.master is app.status_segments
    assert app.evidence_button.master is app.status_segments
    assert app.validity_label.master is app.status_segments
    assert len(app.status.winfo_children())==2  # Segment row and the hidden legacy shortcut hint.
    assert not app._study_shortcut_hint.winfo_manager()
    app.menu_bar.focus_menu()
    assert app.bind('<F10>') and red.bind('<F10>')
