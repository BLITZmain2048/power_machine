"""Loaded-island references and generator-only island severity."""
import copy
import pytest
from power_machine.benchmarks.ieee39 import build_model
from power_machine.contingency import ContingencyOptions, run_contingencies
from power_machine.contingency.references import choose_references
from power_machine.compiled import scenario_view
from power_machine.topology import build_topology
from power_machine.contingency.catalog import index_model


def run(model, ids, policy='largest_generator'):
    return run_contingencies(model,{},ContingencyOptions(q_limits=False,island_reference=policy),
                             selected_ids=ids,filter_reason='Reference-policy contract')


def test_policy_default_and_validation():
    assert ContingencyOptions().island_reference=='largest_generator'
    with pytest.raises(ValueError,match='invalid_island_reference'):
        ContingencyOptions(island_reference='guess')


def test_generator_only_island_reports_solved_base_p_and_signed_q():
    model=build_model();original=copy.deepcopy(model)
    result=run(model,['T02-30'])
    case=result['payload']['cases'][0];metrics=case['metrics']
    base={g['id']:g for g in result['payload']['base']['loadflow']['gens']}['G30']
    assert metrics['disconnected_generator_ids']==['G30']
    assert metrics['disconnected_generation_p_mw']==pytest.approx(base['p_mw'])
    assert metrics['disconnected_generation_q_mvar']==pytest.approx(base['q_mvar'])
    assert metrics['disconnected_generation'][0]['basis']=='base_loadflow'
    assert metrics['unresolved_bus_count']==0
    assert metrics['energized_converged'] and case['loadflow']['converged']
    assert case['status']!='missing_reference'
    assert any(i['status']=='generation_disconnected' for i in metrics['islands'])
    assert model==original


def test_reference_is_reassigned_after_reference_transformer_outage():
    model=build_model();result=run(model,['T06-31'])
    case=result['payload']['cases'][0];metrics=case['metrics']
    assert [r['generator_id'] for r in metrics['reference_assignments']]==['G39']
    assert metrics['reference_assignments'][0]['capacity_is_rating'] is False
    assert metrics['energized_converged']
    assert all(i['status']=='solved' for i in metrics['islands'])
    totals=case['loadflow']['totals']
    assert abs(totals['generation_p_mw']-totals['load_p_mw']-totals['branch_loss_p_mw'])<1e-5
    assert next(g for g in model['gens'] if g['id']=='G39')['ref'] is False


def test_none_policy_keeps_legacy_missing_reference_result():
    result=run(build_model(),['T02-30'],'none')
    case=result['payload']['cases'][0]
    assert case['status']=='missing_reference'
    assert 'reference_assignments' not in case['metrics']
    assert 'disconnected_generation' not in case['metrics']


def test_declared_capacity_wins_over_scheduled_power():
    model=build_model();next(g for g in model['gens'] if g['id']=='G38')['s_rated_mva']=1200.
    view=scenario_view(model,{'outserv':{'T06-31':1}})
    changes,records,diagnostics=choose_references(build_topology(view),index_model(view))
    assert changes=={'G38':True}
    assert records[0]['capacity_mva']==1200.
    assert records[0]['capacity_basis']=='s_rated_mva'


def test_unknown_capacity_is_not_invented():
    model=build_model()
    for g in model['gens']:g['type_params'].pop('sgn',None)
    view=scenario_view(model,{'outserv':{'T06-31':1}})
    changes,records,diagnostics=choose_references(build_topology(view),index_model(view))
    assert not changes and not records
    assert any(d['code']=='island_reference_unavailable' for d in diagnostics)


def test_real_overloads_rank_before_generator_only_events_without_overloads():
    result=run(build_model(),[b['id'] for b in build_model()['branches']])
    cases=result['payload']['cases'];position={ident:i for i,ident in enumerate(result['payload']['alert_order'])}
    thermal=[c for c in cases if any(v['kind']=='loading' for v in c['violations'])]
    isolated=[c for c in cases if c['metrics']['disconnected_generator_ids'] and not any(v['kind']=='loading' for v in c['violations'])]
    assert thermal and isolated
    assert all(position[a['case_id']]<position[b['case_id']] for a in thermal for b in isolated)


def test_unreferenced_base_uses_the_selected_reference():
    model=build_model()
    for generator in model['gens']:
        generator['ref']=False
        generator['params']['ip_ctrl']=0
    result=run(model,['L01-02'])
    base=result['payload']['base']
    assert base['metrics']['energized_converged']
    assert base['metrics']['reference_assignments'][0]['generator_id']=='G39'
    assert result['payload']['cases'][0]['metrics']['energized_converged']
    assert not any(g['ref'] for g in model['gens'])
