"""Contratos visibles del producto, sin alterar las magnitudes del motor."""
from copy import deepcopy
import math
import pytest
from power_machine.gui.display import public_text, elapsed_text
from power_machine.gui.panels.shortcircuit import presentation_quantities
from power_machine.gui.batch_panel import batch_scenarios


def test_public_messages_hide_personal_paths():
    text=public_text(r"Resultado D:\Profiles\sample_account\salidas\corrida\signals.csv")
    assert 'sample_account' not in text and 'Profiles' not in text
    assert 'signals.csv' in text
    assert elapsed_text(.04699999999986)=='47 ms'
    assert elapsed_text(12.345)=='12.35 s'


def test_shortcircuit_units_derived_without_mutation():
    result={'payload':{'fault':{'object_id':'B1'},'Ik_initial_sym_ka':10.,'z_th_ohm':{'1':{'real':1.,'imag':5.,'unit':'ohm'}}}}
    before=deepcopy(result)
    shown=presentation_quantities(result,{'buses':[{'id':'B1','un_kv':110.}]})['payload']
    assert shown['Sk_initial_mva']==pytest.approx(math.sqrt(3)*110*10)
    assert shown['r_x_ratio']==pytest.approx(.2)
    assert result==before


def test_batch_sweep_is_explicit_and_does_not_change_network():
    model={'loads':[{'id':'L1'},{'id':'L2','outserv':1}]}
    before=deepcopy(model)
    rows=batch_scenarios(model,3,.9,1.1)
    assert [r.load_multipliers for r in rows]==[{'L1':.9},{'L1':1.},{'L1':1.1}]
    assert len({r.id for r in rows})==3 and model==before
    with pytest.raises(ValueError):batch_scenarios(model,0,.9,1.1)


def test_drawn_toolbar_icons_are_distinct(tk_root):
    from power_machine.gui.toolbar_icons import icon
    images=[icon(tk_root,i) for i in range(13)]
    pixels=[tuple(im.get(x,y) for y in range(im.height()) for x in range(im.width())) for im in images]
    assert len(set(pixels))==13


def test_element_selector_tracks_programmatic_fault_bus(tk_root):
    import tkinter as tk
    from power_machine.gui.panels.panel_widgets import ElementSelector
    buses=[{'id':'B01','name':'Barra uno','un_kv':110},{'id':'B39','name':'Barra final','un_kv':345}]
    variable=tk.StringVar(tk_root,value='B01')
    selector=ElementSelector(tk_root,buses,{'buses':buses},variable)
    variable.set('B39')
    tk_root.update()
    assert selector.get_id()=='B39' and 'B39' in selector.get()
    selector.destroy()
