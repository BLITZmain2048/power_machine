"""Prepared N-1 results are independent JSON records without repeated full copies."""
import copy
import importlib
import json
from power_machine.benchmarks.ieee39 import build_model
from power_machine.contingency import run_contingencies, ContingencyOptions


def test_prepared_study_has_one_snapshot_and_independent_records(monkeypatch):
    runner=importlib.import_module('power_machine.contingency.runner')
    model=build_model();original=copy.deepcopy(model)
    prepare=runner.compile_network;seen=[]
    def compile_once(*args,**kwargs):
        result=prepare(*args,**kwargs);seen.append(result);return result
    monkeypatch.setattr(runner,'compile_network',compile_once)
    result=run_contingencies(model,{},ContingencyOptions(q_limits=False),selected_ids=['L01-02','L02-03'],filter_reason='test')
    assert result['payload']['completed_count']==2
    assert len(seen)==1
    json.dumps(result,allow_nan=False)
    result['payload']['base']['metrics']['islands'][0]['bus_ids'].clear()
    result['payload']['cases'][0]['loadflow']['islands'][0]['nodes'].clear()
    assert seen[0].run_loadflow()['islands'][0]['nodes']
    assert seen[0].state(seen[0].model,{}, {})['topology']['islands'][0]['buses']
    assert model==original


def test_parameter_delta_is_accepted_by_contingency_materialization():
    runner=importlib.import_module('power_machine.contingency.runner')
    model=build_model();ident=model['branches'][0]['id']
    result=runner.materialize_scenario(model,{'params':{ident:{'X1':2.0}}})
    assert result['branches'][0]['params']['X1']==2.0
    assert model['branches'][0]['params']['X1']!=2.0


def test_scalar_finite_check_covers_each_electrical_output_table():
    runner=importlib.import_module('power_machine.contingency.runner')
    for group in ('islands','buses','branches','gens','loads','diagnostics'):
        assert runner._engine_nonfinite({group:[{'quantity':float('nan'),'id':'test'}]})
    assert runner._engine_nonfinite({'totals':{'generation_p_mw':float('inf')}})
    assert not runner._engine_nonfinite({'buses':[{'u_pu':1.,'angle_deg':0.,'id':'B'}]})


def test_public_outage_members_are_lists_in_cases_catalog_and_exclusions():
    result=run_contingencies(build_model(),{},ContingencyOptions(q_limits=False),selected_ids=['L01-02'],filter_reason='JSON contract')
    payload=result['payload']
    records=[*payload['catalog'],*payload['excluded'],*(o for c in payload['cases'] for o in c['outages'])]
    assert records and all(isinstance(record['member_ids'],list) for record in records)
    assert json.loads(json.dumps(result,allow_nan=False))==result
