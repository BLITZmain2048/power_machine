"""Hito B, request spawn, JSON, archivos exclusivos y contexto falso sin pantalla."""
import copy
import csv
import json
from pathlib import Path
import pickle
import pytest
pytestmark = pytest.mark.gui
from power_machine.contingency import ContingencyOptions, execute_request, run_contingencies
from power_machine.contingency.results import export_run, fingerprint, json_data
from power_machine.gui.panels import contingency as contingency
from gui_support import parallel_model, lines_only

class FakeContext:

    def __init__(self, model, root):
        self.root = root
        study = {'scenario': {}, 'ldf_options': {'q_limits': True}}
        self.snapshot = {'model': model, 'study': study, 'fingerprints': {'model_sha256': fingerprint(model), 'study_sha256': fingerprint(study), 'variant_sha256': None}}
        (self.jobs, self.published, self.logs, self.selections) = ([], [], [], [])

    def get_snapshot(self):
        return self.snapshot

    def submit_job(self, module, request, callback):
        self.jobs.append((module, request, callback))

    def publish_result(self, result):
        self.published.append(result)

    def log(self, level, message):
        self.logs.append((level, message))

    def select_element(self, object_id):
        self.selections.append(object_id)

def test_request_result_roundtrip_context_and_spawn_target(parallel_model, tmp_path):
    context = FakeContext(parallel_model, tmp_path)
    controller = contingency.PanelController(context)
    request = controller.submit(ContingencyOptions(), selected_ids=['L1', 'L2'], filter_reason='Ensayo')
    assert context.jobs[0][0] == 'contingency'
    assert pickle.loads(pickle.dumps(execute_request)) is execute_request
    request = json.loads(json.dumps(request, allow_nan=False))
    result = execute_request(request)
    assert result['payload']['completed_count'] == 2
    assert result['provenance']['pf_comparison'] == 'not validated against PowerFactory'
    assert result['study_sha256'] == fingerprint(context.snapshot['study'])
    controller.on_message(('result', result))
    assert context.published == [result]
    controller.select('L1')
    assert context.selections == ['L1']
    controller.cancel()
    assert context.logs[-1][0] == 'warning'
    assert json.loads(json.dumps(result, allow_nan=False)) == result
    request['snapshot']['model']['loads'][0]['params']['plini'] = 999
    assert context.snapshot['model']['loads'][0]['params']['plini'] == 50.0

@pytest.mark.parametrize('change', ['hash', 'study_hash', 'schema', 'module', 'scenario', 'options', 'output_path'])
def test_request_rejection(parallel_model, tmp_path, change):
    request = contingency.build_request(FakeContext(parallel_model, tmp_path), ContingencyOptions())
    if change == 'hash':
        request['snapshot']['fingerprints']['model_sha256'] = 'wrong'
    elif change == 'study_hash':
        request['snapshot']['fingerprints']['study_sha256'] = 'wrong'
    elif change == 'root':
        request['root'] = str(Path(__file__).resolve().parents[2])
    elif change in ('schema', 'module'):
        request[change] = 'wrong'
    elif change == 'scenario':
        request['snapshot']['study']['scenario'] = {'unknown_field': {'L1': 0}}
        request['snapshot']['fingerprints']['study_sha256'] = fingerprint(request['snapshot']['study'])
    elif change == 'options':
        request['options']['max_cases'] = 0
    else:
        request['input']['output_dir'] = str(Path(__file__).resolve().parents[2])
    result = execute_request(request)
    assert result['status'] == 'invalid_input'
    json.dumps(result, allow_nan=False)

def test_exports_manifest_csv_no_overwrite_and_confined(parallel_model, tmp_path):
    result = lines_only(parallel_model)
    (manifest, table) = export_run(result, tmp_path / 'runs')
    reloaded = json.loads(manifest.read_text(encoding='utf-8'))
    assert reloaded == result
    assert not list(manifest.parent.glob('*.part'))
    with table.open(encoding='utf-8', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2
    assert json.loads(rows[0]['outage_ids']) == ['L1']
    assert reloaded['payload']['excluded'][0]['object_id'] == 'G1'
    assert all((c['metrics']['topology_sha256'] for c in reloaded['payload']['cases']))
    with pytest.raises(FileExistsError):
        export_run(result, tmp_path / 'runs')
    with pytest.raises(ValueError, match='outside'):
        export_run(result, tmp_path.parent, root=tmp_path)
    assert json_data({'absent': None, 'bad': float('nan'), 'z': 1 + 2j}) == {'absent': None, 'bad': None, 'z': {'real': 1.0, 'imag': 2.0, 'unit': '1'}}

def test_filter_requires_reason_and_unknown_id_rejected(parallel_model):
    for inputs in ({'selected_ids': ['L1']}, {'selected_ids': ['unknown'], 'filter_reason': 'Ensayo'}):
        result = run_contingencies(parallel_model, {}, ContingencyOptions(), **inputs)
        assert result['status'] == 'invalid_input'

def test_cancel_before_base_preserves_exact_denominator(parallel_model):

    def forbidden(*args):
        pytest.fail('No resolver después de cancelación')
    progress = []
    result = run_contingencies(parallel_model, {}, ContingencyOptions(orders=(1, 2)), loadflow_service=forbidden, cancel=lambda : True, progress=progress.append)
    assert result['status'] == 'cancelled'
    assert len(result['payload']['missing_case_ids']) == 6
    assert result['payload']['base'] is None
    assert progress[0]['total'] == 6

@pytest.mark.parametrize('payload', [None, [], 'request'])
def test_invalid_request_shape(payload):
    result = execute_request(payload)
    assert result['status'] == 'invalid_input'
    assert result['diagnostics'][0]['code'] == 'invalid_request'

def test_factory_without_display(monkeypatch, parallel_model, tmp_path):

    class Variable:

        def __init__(self, master=None, value=None):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    class Widget:

        def __init__(self, parent=None, **kwargs):
            self.kwargs = kwargs

        def grid(self, **kwargs):
            pass

        def heading(self, *args, **kwargs):
            pass

        def bind(self, *args):
            pass

        def columnconfigure(self, *args, **kwargs):
            pass

        def rowconfigure(self, *args, **kwargs):
            pass
    for name in ('Frame', 'Label', 'Entry', 'Button', 'Checkbutton', 'Treeview'):
        monkeypatch.setattr(contingency.ttk, name, Widget)
    for name in ('BooleanVar', 'StringVar'):
        monkeypatch.setattr(contingency.tk, name, Variable)
    context = FakeContext(parallel_model, tmp_path)
    frame = contingency.create_panel(None, context)
    assert isinstance(frame, Widget)
    assert frame.controller.context is context
    spec = contingency.PANEL_SPEC
    assert (spec['id'],spec['api_version'],spec['factory'],spec['order']) == ('contingency',1,'create_panel',20)
    from power_machine.gui.i18n import tr
    assert tr(spec['title_key'])

def test_configuration_hash_changes_with_scenario_limits_and_groups(parallel_model):
    first = lines_only(parallel_model)['payload']['configuration_sha256']
    changed = lines_only(parallel_model, scenario={'load_scale': {'D2': 1.1}})['payload']['configuration_sha256']
    limits = lines_only(parallel_model, options=ContingencyOptions(q_limits=False))['payload']['configuration_sha256']
    assert len({first, changed, limits}) == 3
    assert first == lines_only(copy.deepcopy(parallel_model))['payload']['configuration_sha256']
