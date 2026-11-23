"""Dominio de creación con datos explícitos e invariantes adicionales."""
from copy import deepcopy
from dataclasses import replace
import pytest
pytestmark = pytest.mark.gui
from gui_support import analytic_history
from power_machine.netedit import create_command, edit_command, VariantError, execute_request, check_variant
from power_machine.gui.netedit.forms import creation_template, number
from power_machine.loadflow import run_loadflow
from gui_support import net_request as request_for

def test_creation_transformer_shunt_zpu_and_switch_have_lf():
    (_, history, ids) = analytic_history()
    tr = creation_template('branches', 'ElmTr2')
    tr.update(name='Transformador', bus_from=ids['b1'], bus_to=ids['b2'])
    tr['type_params'].update(strn=100, utrn_h=10, utrn_l=10, uktr=10, pcutr=0)
    history.apply(create_command(history.variant.variant_id, 'branches', tr))
    shunt = creation_template('shunts')
    shunt.update(name='Condensador', bus=ids['b2'])
    shunt['params'].update(qcapn=1, ushnm=10)
    history.apply(create_command(history.variant.variant_id, 'shunts', shunt))
    zpu = creation_template('branches', 'ElmZpu')
    zpu.update(name='Impedancia común', bus_from=ids['b1'], bus_to=ids['b2'])
    zpu['params'].update(Sn=100, r_pu=0.01, x_pu=0.1, r_pu_ji=0.02, x_pu_ji=0.2)
    history.apply(create_command(history.variant.variant_id, 'branches', zpu))
    switch = creation_template('switches', 'StaSwitch')
    switch.update(name='Cubículo', bus1=ids['b2'], element_id=ids['l1'], port='bus1')
    sw = create_command(history.variant.variant_id, 'switches', switch)
    history.apply(sw)
    assert not any((c.code == 'disconnected_port' and c.object_id == sw.object_id for c in check_variant(history._base, history.variant)))
    assert run_loadflow(history.model)['converged']
    history.apply(edit_command(history.model, 'update', 'switches', sw.object_id, {'closed': False}))
    assert run_loadflow(history.model)['totals']['load_p_mw'] == 0

def test_absent_native_values_and_invalid_existing_units_are_rejected():
    (_, history, ids) = analytic_history()
    assert number('', 'MW') is None
    assert number('0', 'MW') == 0
    with pytest.raises(VariantError, match='incompatible_unit'):
        history.apply(edit_command(history.model, 'update', 'branches', ids['z1'], {'units': {'R1': 'MW'}}))
    line = creation_template('branches')
    line.update(name='Sin impedancia', bus_from=ids['b1'], bus_to=ids['b2'])
    line['params'].update(R1=0, X1=0)
    with pytest.raises(VariantError, match='invalid_electrical_data'):
        history.apply(create_command(history.variant.variant_id, 'branches', line))

def test_spawn_worker_computes_actual_envelope():
    import multiprocessing
    import gc
    gc.collect()
    (base, history, _) = analytic_history()
    request = request_for(base, history.variant)
    with multiprocessing.get_context('spawn').Pool(1) as pool:
        result = pool.apply_async(execute_request, (request,)).get(timeout=60)
    assert result['status'] == 'completed'
    assert result['payload']['loadflow']['converged']
