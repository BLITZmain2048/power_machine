"""Alcance de evidencia externa y lectura de curvas sin interpolación."""
import csv,json
from copy import deepcopy
from dataclasses import asdict
import pytest
from power_machine.resources import readonly_path
from power_machine.shortcircuit import ShortCircuitOptions,FaultSpec
from power_machine.pfcompare.cc import build_supplement
from power_machine.gui.evidence import sc_reference_label,sc_result_matches_reference
from power_machine.gui.reference_plots import native_comparison
from power_machine.gui.panels.panel_widgets import element_label


@pytest.fixture
def included_sc():
    root=readonly_path('cases','ieee39_pf')
    model=json.loads((root/'model.json').read_text('utf-8-sig'))
    data=asdict(build_supplement(model,root))
    options=ShortCircuitOptions('iec60909','maximum',frequency_hz=60,min_break_time_s=.1,profile='iec_pf2016_c1_v1')
    bus=next(b for b in model['buses'] if b['name']=='Bus 39')
    return model,data,options,FaultSpec(bus['id'],'3ph')


def test_sc_badge_requires_identical_model_options_and_sources(included_sc):
    model,data,options,fault=included_sc
    before=deepcopy((model,data))
    assert sc_reference_label(model,options,data,fault)=='cotejado con PowerFactory (IEEE 39)'
    changed=deepcopy(model)
    changed['branches'][0]['params']['R1']+=.01
    assert sc_reference_label(changed,options,data,fault) is None
    assert sc_reference_label(model,ShortCircuitOptions('iec60909','maximum'),data,fault) is None
    changed_data=deepcopy(data)
    changed_data['assumptions']=[]
    assert sc_reference_label(model,options,changed_data,fault) is None
    assert (model,data)==before


def test_sc_badge_checks_actual_three_quantities(included_sc):
    model,_,_,fault=included_sc
    with readonly_path('cases','ieee39_pf','E39-CC-01','fault_summary.csv').open(encoding='utf-8-sig',newline='') as f:
        row=next(r for r in csv.DictReader(f) if r['fault_bus']=='39')
    result={'payload':{'fault':{'object_id':fault.bus_id},'Ik_initial_sym_ka':float(row['Ikss_kA']),
                       'ip_peak_ka':float(row['ip_kA']),'Ib_sym_ka':float(row['Ib_kA'])}}
    assert sc_result_matches_reference(result,model)
    result['payload']['ip_peak_ka']*=1.1
    assert not sc_result_matches_reference(result,model)


def test_selector_without_station_uses_id_and_voltage():
    assert element_label({'id':'B39','name':'Nombre largo','un_kv':345},{'buses':[]})=='B39 · 345 kV'


def test_native_reader_preserves_samples_and_external_time_grid(tmp_path):
    channels=[{'signal':'B4_U_pu','variable':'m:u','unit':'p.u.'},{'signal':'G32_speed_pu','variable':'s:speed','unit':'p.u.'}]
    with (tmp_path/'channel_units.csv').open('w',newline='',encoding='utf8') as f:
        w=csv.DictWriter(f,fieldnames=list(channels[0]));w.writeheader();w.writerows(channels)
    (tmp_path/'signals_native.csv').write_text('time;B4;G32\nb:tnow in s;m:u in p.u.;s:speed in p.u.\n0;1.01;1\n1.2;.97;1.001\n',encoding='utf8')
    spec,own,reference=native_comparison(tmp_path,readonly_path('cases','ieee39_pf','E39-DIN-01'))
    assert own.rows==[[0.,1.01,1.],[1.2,.97,1.001]]
    assert len(reference.rows)==121 and reference.rows[-1][0]==pytest.approx(1.2)
    assert spec['source_legend'] and len(spec['curves'])==2


def test_automatic_coordinates_are_plain_json_scalars():
    from gui_support import ieee_model
    from power_machine.gui.diagram.layout import build_scene,diagram_groups
    from power_machine.gui.presentation_specs import _json
    model=ieee_model()
    model['meta'].pop('benchmark',None)
    scene=build_scene(model,diagram_groups(model)[0]['id'])
    positions={key:[symbol.x,symbol.y] for key,symbol in scene.symbols.items()}
    _json(positions)
    assert all(type(value) in (float,int) for point in positions.values() for value in point)


def test_n1_uses_engine_priority_and_published_generation():
    from power_machine.gui.panels.contingency import severity_order,engineering_row
    cases=[{'case_id':'generation','status':'generation_disconnected','metrics':{'alert_priority':5,'severity':0,'newly_disconnected_generation_p_mw':1000}},
           {'case_id':'voltage','status':'limit_violation','metrics':{'alert_priority':4,'severity':9}},
           {'case_id':'thermal','status':'limit_violation','metrics':{'alert_priority':3,'severity':.1}}]
    from power_machine.api import ContingencyResult, StudyTable
    for case, loading in zip(cases, (80, 90, 120)):
        case['metrics'].update(unsupplied_p_mw=0, max_loading_pct=loading)
    expected = ContingencyResult(status='completed', cases=StudyTable('cases', tuple(
        dict(case, unserved_mw=case['metrics']['unsupplied_p_mw'],
             max_loading_pct=case['metrics']['max_loading_pct']) for case in cases))).rank().to_records()
    assert [c['case_id'] for c in sorted(cases,key=severity_order)] == [c['case_id'] for c in expected]
    model={'buses':[{'id':'B1','un_kv':110}], 'branches':[{'id':'L1','bus_from':'B1','name':'L1'}]}
    case={'status':'generation_disconnected','outages':[{'object_id':'L1'}],
          'metrics':{'max_loading_pct':121.2345,'newly_disconnected_generation_p_mw':42.,'severity':.2,
                     'energized_converged':True,'branches':[{'model_id':'L1','loading_pct':121.2345}]}}
    row=engineering_row(case,model)
    assert row['loading']==121.2345 and row['generation']==42.
    from power_machine.gui.panels.contingency import STATUS_TEXT
    assert row['state'] == STATUS_TEXT['generation_disconnected'] and 'L1' in row['branch']
