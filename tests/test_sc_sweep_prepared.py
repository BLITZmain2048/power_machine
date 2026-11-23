"""Sweep rows preserve all public fault quantities and deenergized semantics."""
from dataclasses import asdict
import pytest
from test_shortcircuit_analytical import fixture
from power_machine.shortcircuit import FaultSpec, ShortCircuitOptions, run_shortcircuit
from power_machine.shortcircuit.results import json_value
from power_machine.sc_sweep.runner import run_sweep, _row


@pytest.mark.parametrize('source_active',[True,False])
def test_sweep_row_matches_full_fault_response(source_active):
    model,data=fixture(two_buses=True)
    model['gens'][0]['outserv']=0 if source_active else 1
    options=ShortCircuitOptions('iec60909','maximum')
    full=run_shortcircuit(model,FaultSpec('B2','3ph'),options,data)
    expected=_row(model['buses'][1],{'status':full.status,'payload':json_value(full.quantities),
                                  'diagnostics':json_value(full.diagnostics)},False)
    sweep=run_sweep(model,{}, {**asdict(options),'fault_kind':'3ph'},asdict(data),selected_bus_ids=['B2'])
    assert sweep['rows']==[expected]
