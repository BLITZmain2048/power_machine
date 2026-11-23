"""Desktop identity, shared property editor and reversible load scaling."""
from pathlib import Path
import pytest
from test_gui_shell_windows import shell
from power_machine.gui.load_tools import scaling_preview


def test_scale_preview_commit_and_undo(shell):
    from power_machine.gui.properties_dock import ensure_session
    model=ensure_session(shell).model
    load=model['loads'][0]
    commands,before,after=scaling_preview(model,{load['id']},1.2,.9)
    assert after[0]==pytest.approx(before[0]*1.2)
    assert after[1]==pytest.approx(before[1]*.9)
    result=shell.document.network.commit(commands,shell.document.network.revision,'test')
    assert result['accepted'],result
    assert shell.document.network.model['loads'][0]['p_mw']==pytest.approx(load['p_mw']*1.2)
    shell.network_changed()
    shell._tools_network_revision=(id(shell.document),shell.document.revision)
    shell.undo()
    assert shell.document.network.model['loads'][0]['p_mw']==load['p_mw']


def test_logo_optional_and_ribbon(shell,tmp_path):
    from power_machine.gui.identity import apply_window_icon
    apply_window_icon(shell,tmp_path)
    assert len(shell.ribbon.tabs())==5
    assert all(button.cget('text') for button in shell.action_buttons)
    shell.select(shell.model['loads'][0]['id'])
    assert shell.properties_dock.editor is not None
    shell.geometry('1264x664+0+0')
    shell.update()
    apply=shell.properties_dock.editor.apply_button
    assert apply.winfo_rooty()+apply.winfo_height() <= shell.properties_dock.winfo_rooty()+shell.properties_dock.winfo_height()
    from power_machine.gui.ribbon import edit_network
    edit_network(shell)
    assert shell.diagrams[None].editing


def test_identity_windows_call(monkeypatch):
    from types import SimpleNamespace
    from power_machine.gui import identity
    import ctypes
    calls=[]
    monkeypatch.setattr(identity,'sys',SimpleNamespace(platform='win32'))
    monkeypatch.setattr(ctypes,'windll',SimpleNamespace(shell32=SimpleNamespace(SetCurrentProcessExplicitAppUserModelID=calls.append)))
    identity.initialize_windows_identity()
    assert calls==['PowerMachine.Desktop.1']


def test_inline_load_pq_edits_are_atomic(shell):
    from power_machine.gui.datamanager import build_operations
    load=shell.model['loads'][0]
    row={**load,'object_id':load['id']}
    edits=build_operations([row],'p_mw','123 MW')+build_operations([row],'q_mvar','42 Mvar')
    result=shell.document.apply_operations(edits,shell.document.revision,'P/Q')
    assert result['accepted'],result
    changed=next(l for l in shell.document.network.model['loads'] if l['id']==load['id'])
    assert changed['p_mw']==123 and changed['q_mvar']==42
    assert changed['params']['plini']==123 and changed['params']['qlini']==42


def test_wide_tab_does_not_oscillate_scroll_position():
    from types import SimpleNamespace
    from power_machine.gui.app_shell import WorkspaceNotebook
    left=[0.]
    def move(fraction):left[0]=fraction*400
    frame=SimpleNamespace(winfo_x=lambda:0,winfo_width=lambda:200)
    canvas=SimpleNamespace(canvasx=lambda _:left[0],winfo_width=lambda:100,
                           winfo_ismapped=lambda:True,xview_moveto=move)
    book=SimpleNamespace(winfo_exists=lambda:True,select=lambda:'tab',buttons={'tab':(frame,)},
                         canvas=canvas,inner=SimpleNamespace(winfo_reqwidth=lambda:400))
    for _ in range(10):
        WorkspaceNotebook.ensure_visible(book)
        assert left[0]==0


def test_scaling_preview_includes_operating_scenario_without_compounding():
    from copy import deepcopy
    from power_machine.benchmarks.ieee39 import build_model
    model=build_model()
    load=next(l for l in model['loads'] if l['p_mw']>0)
    active=deepcopy(model)
    next(l for l in active['loads'] if l['id']==load['id'])['scale']=2.
    commands,before,after=scaling_preview(model,{load['id']},1.2,1.2,operational=active)
    assert before[0]==pytest.approx(load['p_mw']*2)
    assert after[0]==pytest.approx(before[0]*1.2)
    assert commands[0].data['p_mw']==pytest.approx(load['p_mw']*1.2)
