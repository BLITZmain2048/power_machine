"""Integration invariants across shared study panels and event targets."""
import ast,json
from pathlib import Path
import pytest
from test_gui_shell_windows import shell
from power_machine.gui.i18n import tr,set_language
from power_machine.gui.presentation import element_label
from power_machine.gui.panels.panel_widgets import element_label as selector_label
from power_machine.gui.events_editor import EventsEditor,validate_events

def test_shared_labels_omit_missing_station():
    bus=dict(id='B01',name='Bus 1',un_kv=345,class_='ElmTerm')
    model=dict(buses=[bus])
    assert element_label(bus,{'B01':bus})=='B01 · 345 kV'
    assert selector_label(bus,model)=='B01 · 345 kV'
    bus['substation']='Test station'
    assert selector_label(bus,model)=='B01 · Test station · 345 kV'

def test_actions_are_unique_and_language_independent(shell):
    for code in ('en','es'):
        set_language(code)
        for ident in ('modal','calc_cct','calc_met','calc_pvqv','shortcircuit','contingency'):
            shell.open_parity_panel(ident)
            assert ident in shell.parity_panels
            frame=shell.parity_panels[ident][1].panel
            assert set(frame.action_buttons)=={'run','cancel','export'}
            visible=[w for w in frame.actions.winfo_children() if w.winfo_manager()]
            roles=[getattr(w,'action_id',None) for w in visible]
            assert roles.count('run')==1
            assert roles.count('cancel')==1
            assert roles.count('export')==1
            if hasattr(frame,'capability_gate'):
                assert frame.capability_gate.execute_button is frame.action_buttons['run']
            shell.close_parity_panel(ident)

def test_fault_defaults_and_explicit_preset(shell):
    for params in ({},{'r_ohm':.2,'x_ohm':.5}):
        editor=EventsEditor(shell,shell.document,shell.api,shell.root_path,shell.model,shell.index,
                            lambda:None,preset=dict(kind='short_circuit',target_id='B01',params=params))
        assert float(editor.params['r_ohm'].get())==params.get('r_ohm',0)
        assert float(editor.params['x_ohm'].get())==params.get('x_ohm',0)
        editor.destroy()

def test_worker_status_topics(shell):
    shell.bus.publish('worker.progress',kind='batch',completed=2,total=4,cancelling=False)
    assert float(shell.progress['value'])==50
    shell.bus.publish('worker.cancelled',kind='batch',requested=True)
    assert shell.state_text.get()==tr('integration.worker_cancelling',kind='batch')

def test_title_catalogues_available_before_panels(shell):
    for code in ('en','es'):
        set_language(code)
        assert 'study.' not in shell.title()
    import power_machine.gui.i18n as i18n
    folder=Path(i18n.__file__).parent/'locale'
    en=json.loads((folder/'en.json').read_text(encoding='utf8'))
    es=json.loads((folder/'es.json').read_text(encoding='utf8'))
    assert en.keys()==es.keys()


def test_engine_diagnostics_are_localized_for_display():
    from power_machine.gui.i18n import translate_text,language
    previous=language()
    try:
        set_language('en')
        assert translate_text('Motor CCT explícito')=='Explicit CCT engine'
        assert translate_text('La corrida no contiene signals.csv')=='The run does not contain signals.csv'
    finally:
        set_language(previous)


def test_reference_policy_keeps_canonical_options_in_both_languages(shell):
    shell.open_parity_panel('contingency')
    panel=shell.parity_panels['contingency'][1].panel
    for code in ('en','es'):
        set_language(code)
        assert panel.preflight_inputs()['options']['island_reference']=='largest_generator'
        panel.reference_selector.current(1)
        assert panel.preflight_inputs()['options']['island_reference']=='none'
        panel.reference_selector.current(0)

def test_network_palette_is_localized_without_changing_element_types(shell,monkeypatch):
    shell.open_parity_panel('network_editor')
    panel=shell.parity_panels['network_editor'][1].panel
    from power_machine.gui.panels import network_editor
    for code in ('en','es'):
        set_language(code)
        panel.palette_box.current(2)
        assert panel.palette_key()=='line'
        panel.create()
        assert shell.diagrams[None]._drawing_tool=='line'
        assert not panel.group_choice.get().startswith('integration.')


def test_diagram_group_identity_is_independent_of_display_language():
    from power_machine.gui.diagram.layout import diagram_groups
    from power_machine.gui.i18n import language
    model={'buses':[{'id':'B01','un_kv':345}]}
    previous=language()
    try:
        set_language('en')
        english=diagram_groups(model)[0]
        set_language('es')
        spanish=diagram_groups(model)[0]
        assert english['id']==spanish['id']=='sistema_interno::Raíz'
        assert english['name']!=spanish['name']
    finally:
        set_language(previous)
