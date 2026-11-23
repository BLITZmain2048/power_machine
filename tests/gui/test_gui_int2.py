"""Recovery, event persistence and declared-frequency GUI integration."""
import json
from pathlib import Path
import pytest
from test_gui_shell_windows import shell
from power_machine.gui.events_editor import EventsEditor
from power_machine.gui.i18n import set_language, tr
from power_machine.gui.panels.modal import modal_base_supported


@pytest.mark.parametrize('frequency', [50, 60, 16.7])
def test_modal_accepts_declared_frequency(frequency):
    assert modal_base_supported(dict(s_base_mva=100, f_nom_hz=frequency))


@pytest.mark.parametrize('frequency', [None, 0, -1, float('nan'), float('inf'), '60', True])
def test_modal_rejects_missing_or_invalid_frequency(frequency):
    assert not modal_base_supported(dict(s_base_mva=100, f_nom_hz=frequency))
    assert not modal_base_supported(dict(s_base_mva=50, f_nom_hz=60))


def test_apply_includes_pending_fault_and_explicit_clearing(shell, monkeypatch):
    errors = []
    monkeypatch.setattr('power_machine.gui.events_editor.messagebox.showerror', lambda *a, **k: errors.append(a))
    editor = EventsEditor(shell, shell.document, shell.api, shell.root_path, shell.model, shell.index,
                          shell.update_document, preset=dict(kind='short_circuit', target_id='B16'))
    try:
        assert editor.form_pending()
        assert editor.rows == []
        assert not editor.include_clearing.get()
        assert not editor.clearing_time.get()  # No clearing time is silently invented.
        editor.time.set('.02')
        editor.clearing_time.set('.06')
        editor.include_clearing.set(True)
        editor.apply_button.invoke()
        assert not errors
        assert editor.winfo_exists()  # Apply preserves the editor and its form.
        events = [row.to_dict() for row in shell.document.study.events]
        assert [row['kind'] for row in events] == ['short_circuit', 'clear_short_circuit']
        assert [row['time_s'] for row in events] == [.02, .06]
        assert events[0]['params'] == dict(r_ohm=0., x_ohm=0.)
        editor.apply_button.invoke()
        assert len(shell.document.study.events) == 2
        set_language('en')
        assert 'aplicaron' not in editor.pending_text.get()
    finally:
        editor.destroy()


def test_recovery_actions_and_distinct_exports(shell):
    for ident in ('modal', 'calc_cct', 'calc_met', 'shortcircuit'):
        shell.open_parity_panel(ident)
        panel = shell.parity_panels[ident][1].panel
        assert callable(panel.recovery_action)
        assert panel.capability_gate.recovery_button.cget('text') == tr(panel.recovery_key)
        assert panel.action_buttons['export'].cget('text') == tr('int2.export_result')
        assert panel.export_table_button.cget('text') == tr('int2.export_table')
        shell.close_parity_panel(ident)


def test_history_explains_stale_options_and_preserves_archive(shell):
    from power_machine.loadflow import run_loadflow
    shell.loadflow_done(run_loadflow(shell.model))
    before = shell.document.runs_for_case()
    shell.document.edit(lambda case: setattr(case.options, 'dt_s', .002))
    win = shell.run_history_dialog()
    try:
        tree = win.history_tree
        tree.selection_set(before[0]['run_id'])
        tree.event_generate('<<TreeviewSelect>>')
        shell.update()
        assert tr('int2.dependency.options_sha256') in win.history_detail.get()
        assert set(win.history_actions) >= {'int2.recalculate', 'int2.open_result'}
        assert (shell.root_path / before[0]['result_path']).is_file()
        assert len(shell.document.runs_for_case()) == len(before)
        set_language('en')
        assert tree.item(before[0]['run_id'],'values')[-1] == tr('study.stale')
        assert tree.selection() == (before[0]['run_id'],)
        values = tree.item(before[0]['run_id'], 'values')
        assert values[0] == tr('shell.load_flow')
        assert values[1].endswith(' UTC') and '+' not in values[1]
        assert before[0]['created_at_utc'] in win.history_detail.get()
    finally:
        win.destroy()


def test_case_manager_exposes_case_actions(shell):
    win = shell.study_selector()
    try:
        assert set(win.case_actions) >= {'study.new_case', 'study.duplicate_case', 'study.activate'}
    finally:
        win.destroy()


def test_initial_empty_state_offers_study_actions(shell):
    assert shell.book.select() == str(shell.welcome_page)
    for code in ('en', 'es'):
        set_language(code)
        assert shell.book.tab(shell.welcome_page, 'text') == tr('int2.start_title')


def test_rms_recovery_opens_network_data_selector(shell, monkeypatch):
    calls = []
    monkeypatch.setattr(shell, 'choose_benchmark', lambda: calls.append('model'))
    window = shell.rms_recovery_dialog()
    window.open_model_button.invoke()
    assert calls == ['model']


def test_pf_reference_overlay_never_qualifies_current_run():
    from types import SimpleNamespace
    from power_machine.gui.pf_context import comparison_context
    page = SimpleNamespace(reference={}, reference_label='E39-DIN-01 / PowerFactory')
    badge, detail = comparison_context(page)
    assert badge == tr('int2.pf_reference_only')
    assert 'E39-DIN-01' in detail
    page.reference_label = 'User run B'
    badge, detail = comparison_context(page)
    assert badge == tr('int2.pf_no_comparison')
    assert 'User run B' in detail


def test_native_case_does_not_replace_a_modified_network_or_scenario():
    from power_machine.resources import readonly_path
    from power_machine.gui.native_case import is_native_case
    model = json.loads(readonly_path('cases', 'ieee39_pf', 'model.json').read_text(encoding='utf-8-sig'))
    assert is_native_case(model, {'scenario': {}})
    assert not is_native_case(model, {'scenario': {'load_scale': {'L1': 1.1}}})
    model['meta']['f_nom_hz'] = 50
    assert not is_native_case(model, {'scenario': {}})


def test_result_export_uses_full_cached_result_even_without_controller_cache(shell, monkeypatch, tmp_path):
    shell.open_parity_panel('shortcircuit')
    context = shell.parity_panels['shortcircuit'][1]
    payload = {'schema': 'pm-parity-result-v1', 'status': 'completed',
               'payload': {'Ik_initial_sym_ka': 12.3456789}, 'diagnostics': [{'message': 'scope'}]}
    context.results['shortcircuit'] = payload
    path = shell.root_path / 'export_result.json'
    monkeypatch.setattr('power_machine.gui.panels.panel_widgets.filedialog.asksaveasfilename',
                        lambda **kwargs: str(path))
    context.panel.action_buttons['export'].invoke()
    assert json.loads(path.read_text(encoding='utf-8')) == payload


def test_luna_catalogue_corrections():
    from power_machine.gui import i18n
    root = Path(i18n.__file__).parent / 'locale'
    en = json.loads((root / 'en.json').read_text(encoding='utf-8'))
    es = json.loads((root / 'es.json').read_text(encoding='utf-8'))
    assert en.keys() == es.keys()
    for key in ('events.rf', 'data.column.r1_ohm', 'data.column.x1_ohm', 'panel.shortcircuit.rf', 'panel.shortcircuit.xf'):
        assert 'Ω' in en[key]
    assert '�' not in en['window.diagram']
    assert es['panel.qds.tab.interface'] == 'Interfaz'
    assert es['panel.common.evaluable'] == 'evaluable'
