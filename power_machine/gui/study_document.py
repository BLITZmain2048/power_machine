"""Contenedor de estudios v2, materialización y commit de un solo archivo."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from uuid import uuid4
from dataclasses import replace

from power_machine.netedit.schema import (value_sha256, model_sha256, new_variant, VariantCommand)
from power_machine.netedit import materialize_variant
from power_machine.loadflow import _scenario
from .workspace_links import confined_path, relative_path, file_sha256
from .i18n import tr

SCHEMA = 'power_machine-study-v2'


def uid():
    return str(uuid4())


def now():
    return datetime.now(timezone.utc).isoformat()


def find(items, key, identity):
    for item in items:
        if item[key] == identity:
            return item
    raise ValueError(tr('integration.unknown_reference') + str(identity))


def new_document(study):
    case_id, scenario_id = uid(), uid()
    config = deepcopy(study)
    operations = config.pop('scenario', {})
    config['scenario'] = {}
    return dict(schema=SCHEMA, document_id=uid(), revision=0, name=study['name'],
        base=dict(model_path=None, model_file_sha256=None, overrides_path=None,
                  overrides_sha256=None, electrical_sha256=None, source_package=None),
        active_case_id=case_id,
        cases=[dict(case_id=case_id, name=study['name'], variant_id=None, stage_id=None,
                    scenario_id=scenario_id, study=config, calculation_presets={}, workspace_ids=[], result_ids=[])],
        operating_scenarios=[dict(scenario_id=scenario_id, name=tr('study.default_operation'),
                                  default_name_key='study.default_operation', revision=0, operations=operations)],
        network_variants=[], workspaces=[], result_links=[], presentation={}, trash=[],
        provenance=dict(created_at_utc=now()), extensions={})


def validate(data):
    value_sha256(data)
    if data.get('schema') != SCHEMA:
        raise ValueError(tr('integration.incompatible_study_schema'))
    if type(data.get('revision')) is not int or data['revision'] < 0:
        raise ValueError(tr('integration.invalid_revision'))
    for key in ('document_id', 'name', 'base', 'presentation', 'provenance', 'extensions'):
        if key not in data:
            raise ValueError(tr('integration.required_field_missing') + key)
    if not isinstance(data.get('trash', []), list):
        raise ValueError(tr('integration.invalid_trash'))
    for entry in data.get('trash', []):
        if not isinstance(entry, dict) or entry.get('kind') not in ('case', 'scenario', 'variant'):
            raise ValueError(tr('integration.invalid_trash_entry'))
        if not isinstance(entry.get('deleted_at_utc'), str) or not isinstance(entry.get('item'), dict):
            raise ValueError(tr('integration.incomplete_trash_entry'))
    all_ids = set()
    for collection, key in (('cases', 'case_id'), ('operating_scenarios', 'scenario_id'),
                            ('network_variants', 'variant_id'), ('workspaces', 'workspace_id'),
                            ('result_links', 'run_id')):
        for item in data[collection]:
            identity = item[key]
            uuid4_type = __import__('uuid').UUID
            uuid4_type(identity)
            if identity in all_ids:
                raise ValueError(tr('integration.duplicate_identity') + identity)
            all_ids.add(identity)
    find(data['cases'], 'case_id', data['active_case_id'])
    import math
    for scenario in data['operating_scenarios']:
        if type(scenario['revision']) is not int or scenario['revision'] < 0:
            raise ValueError(tr('integration.invalid_scenario_revision'))
        for field, changes in scenario['operations'].items():
            if field not in ('outserv', 'closed', 'load_scale', 'gen_p_mw', 'v_set_pu') or not isinstance(changes, dict):
                raise ValueError(tr('integration.unsupported_operation') + field)
            for identity, value in changes.items():
                if not isinstance(identity, str) or not identity:
                    raise ValueError(tr('integration.operation_identity_missing'))
                if field in ('outserv', 'closed'):
                    if type(value) not in (int, bool) or value not in (0, 1):
                        raise ValueError(tr('integration.invalid_boolean_state'))
                elif type(value) not in (int, float) or not math.isfinite(value):
                    raise ValueError(tr('integration.setpoint_is_nonnumeric_or_missing'))
                elif field == 'load_scale' and value < 0 or field == 'v_set_pu' and value <= 0:
                    raise ValueError(tr('integration.setpoint_outside_its_domain'))
    for variant in data['network_variants']:
        ids = set()
        sequences = set()
        for stage in variant['stages']:
            __import__('uuid').UUID(stage['stage_id'])
            if stage['stage_id'] in ids:
                raise ValueError(tr('integration.duplicate_stage'))
            ids.add(stage['stage_id'])
            if stage['stage_id'] in all_ids or stage['sequence'] in sequences:
                raise ValueError(tr('integration.duplicate_stage_identity_or_sequence'))
            all_ids.add(stage['stage_id'])
            sequences.add(stage['sequence'])
            date = datetime.fromisoformat(stage['effective_at'])
            if date.tzinfo is None or type(stage['sequence']) is not int:
                raise ValueError(tr('integration.stage_requires_sequence_and_time_zone_aware'))
            if stage['parent_stage_id']:
                parent = find(variant['stages'], 'stage_id', stage['parent_stage_id'])
                if parent['sequence'] >= stage['sequence'] or datetime.fromisoformat(parent['effective_at']) > date:
                    raise ValueError(tr('integration.invalid_stage_order'))
    for case in data['cases']:
        find(data['operating_scenarios'], 'scenario_id', case['scenario_id'])
        if case['study'].get('scenario'):
            raise ValueError(tr('integration.scenario_is_stored_by_reference'))
        if bool(case['variant_id']) != bool(case['stage_id']):
            raise ValueError(tr('integration.alternative_and_stage_must_be_selected_together'))
        if case['variant_id']:
            variant = find(data['network_variants'], 'variant_id', case['variant_id'])
            find(variant['stages'], 'stage_id', case['stage_id'])
        for identity in case['workspace_ids']:
            find(data['workspaces'], 'workspace_id', identity)
        for identity in case['result_ids']:
            link = find(data['result_links'], 'run_id', identity)
            if link['case_id'] != case['case_id']:
                raise ValueError(tr('integration.run_associated_with_another_case'))
    for w in data['workspaces']:
        if w['kind'] not in ('protection', 'dsl', 'types') or type(w['revision']) is not int or w['revision'] < 0:
            raise ValueError(tr('integration.incompatible_workspace'))
        if value_sha256(w['content']) != w['content_sha256']:
            raise ValueError(tr('integration.workspace_modified') + w['workspace_id'])
    for link in data['result_links']:
        find(data['cases'], 'case_id', link['case_id'])
    # Las rutas conocidas se verifican también dentro de extensiones de insumos.
    def paths(value):
        if isinstance(value, dict):
            if 'id' in value and ('class' in value or 'provenance' in value):
                # path y relative_path aquí identifican objetos PF, no archivos.
                return
            for k, v in value.items():
                if (k.endswith('_path') or k in ('path', 'gui_model_path')) and isinstance(v, str) and v:
                    relative = relative_path(v)
                    if v != relative:
                        raise ValueError(tr('integration.the_persisted_path_must_be_relative') + k)
                else:
                    paths(v)
        elif isinstance(value, list):
            for v in value:
                paths(v)
    paths(data)


def validate_models(data, base, study_class):
    """Comprueba también etapas inactivas y escenarios sin caso asociado."""
    for variant in data['network_variants']:
        for stage in variant['stages']:
            structural_model(data, base, dict(variant_id=variant['variant_id'], stage_id=stage['stage_id']))
    for case in data['cases']:
        materialize(data, base, case['case_id'], study_class)


def structural_model(data, base, case):
    model = deepcopy(base)
    if not case['variant_id']:
        return model
    variant = find(data['network_variants'], 'variant_id', case['variant_id'])
    if variant['base_electrical_sha256'] != model_sha256(base):
        raise ValueError(tr('integration.alternative_base_mismatch'))
    chain, seen = [], set()
    stage_id = case['stage_id']
    while stage_id:
        if stage_id in seen:
            raise ValueError(tr('integration.cycle_between_stages'))
        seen.add(stage_id)
        stage = find(variant['stages'], 'stage_id', stage_id)
        chain.append(stage)
        stage_id = stage['parent_stage_id']
    commands = []
    for stage in reversed(chain):
        if stage['input_electrical_sha256'] != model_sha256(model):
            raise ValueError(tr('integration.stage_input_fingerprint_mismatch') + stage['stage_id'])
        commands.extend(VariantCommand(**deepcopy(c)) for c in stage['commands'])
        v = replace(new_variant(base, variant_id=variant['variant_id']), commands=tuple(commands))
        model = materialize_variant(base, v)
        if stage['output_electrical_sha256'] != model_sha256(model):
            raise ValueError(tr('integration.stage_output_fingerprint_mismatch') + stage['stage_id'])
    return model


def materialize(data, base, case_id, study_class):
    case = find(data['cases'], 'case_id', case_id)
    model = structural_model(data, base, case)
    ops = find(data['operating_scenarios'], 'scenario_id', case['scenario_id'])['operations']
    from .document import operation_changes
    from power_machine.netedit.commands import index_model
    entries = index_model(model)
    for field, mapping in ops.items():
        for identity, value in mapping.items():
            if identity not in entries:
                raise ValueError(tr('integration.scenario_target_missing') + identity)
            group, item = entries[identity]
            if item.get('outserv') and field != 'outserv':
                raise ValueError(tr('integration.scenario_target_removed') + identity)
            operation_changes(dict(item, group=group), {field: value})
    model = _scenario(model, ops)
    study = study_class.from_json(deepcopy(case['study']))
    study.scenario = {}
    dependencies = dict(base_electrical_sha256=model_sha256(base), materialized_electrical_sha256=model_sha256(model),
        scenario_sha256=value_sha256(ops), options_sha256=value_sha256(dict(options=case['study'].get('options'),
            ldf_options=case['study'].get('ldf_options'), presets=case['calculation_presets'])),
        events_sha256=value_sha256(case['study'].get('events', [])),
        workspace_hashes=sorted([dict(workspace_id=w['workspace_id'], sha256=value_sha256(w))
            for w in data['workspaces'] if w['workspace_id'] in case['workspace_ids']], key=lambda w: w['workspace_id']))
    return model, study, dependencies


def atomic_save(target, data, expected_hash):
    """Serializa escritores cooperativos mediante un archivo exclusivo."""
    target = confined_path(target)
    lock = target.with_name(target.name + '.lock')
    try:
        stream = lock.open('xb')
    except FileExistsError:
        raise ValueError(tr('integration.another_writer_has_reserved_the_destination')) from None
    try:
        with stream:
            stream.write(str(os.getpid()).encode('ascii'))
        return _atomic_save_locked(target, data, expected_hash)
    finally:
        lock.unlink()


def _atomic_save_locked(target, data, expected_hash):
    """El único punto de publicación es os.replace del temporal completo."""
    target = confined_path(target)
    validate(data)
    encoded = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    def check():
        actual = file_sha256(target) if target.exists() else None
        if actual != expected_hash:
            raise ValueError(tr('integration.destination_changed_by_another_writer'))
    check()
    temporary = target.with_name(target.name + '.' + uid() + '.tmp')
    with temporary.open('xb') as stream:
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())
    if target.exists():
        old = target.read_bytes()
        previous = target.with_name(target.name + '.revision-' + uid() + '.bak')
        with previous.open('xb') as stream:
            stream.write(old)
            stream.flush()
            os.fsync(stream.fileno())
    check()
    os.replace(temporary, target)
    return file_sha256(target)
