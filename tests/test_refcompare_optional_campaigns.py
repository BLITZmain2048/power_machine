import pytest
"""Optional REF campaigns do not require folders or create partial NET outputs."""
from pathlib import Path
from power_machine.refcompare import lf, net


@pytest.mark.refdata
def test_lf_runs_base_and_reports_missing_contingency_campaigns(monkeypatch,tmp_path):
    import power_machine.benchmarks.ieee39_ref as benchmark
    import importlib
    loadflow=importlib.import_module('power_machine.loadflow')
    monkeypatch.setattr(benchmark,'build_model',lambda folder:{})
    monkeypatch.setattr(loadflow,'run_loadflow',lambda *args,**kwargs:{})
    monkeypatch.setattr(lf,'read_reference',lambda folder:{})
    monkeypatch.setattr(lf,'compare_loadflow',lambda *args:{'pass':True,'maxima':{}})
    written=[]
    monkeypatch.setattr(lf,'write_comparison',lambda result,path:written.append(path))
    result=lf.run_comparisons(tmp_path/'export',tmp_path/'output')
    assert result['E39-LF-01']['pass']
    assert [r['status'] for key,r in result.items() if key.startswith('E39-CT')]==['not_included','not_included']
    assert written==[tmp_path/'output/E39-LF-01']


def test_net_reports_missing_reference_before_model_reads_or_outputs(monkeypatch,tmp_path):
    monkeypatch.setattr(net,'MODEL_PATH',tmp_path/'absent-model.json')
    def forbidden(*args,**kwargs):raise AssertionError('LF ran without a NET reference')
    monkeypatch.setattr(net,'run_loadflow',forbidden)
    output=tmp_path/'output'
    result=net.run_comparisons(tmp_path/'export',output)
    assert result['E39-NET-01']['status']=='reference_not_included'
    assert result['E39-NET-01']['pass'] is None
    assert not output.exists()

