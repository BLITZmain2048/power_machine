"""Documento editable. El modelo de origen nunca se modifica."""
from power_machine.gui.i18n import tr
from copy import deepcopy
import math
from power_machine.gui import study_document as v2
from .workspace_links import (confined_path, relative_path, file_sha256, read_json,
                              check_link, calculation_sha256, workspace)
from .history import History, SnapshotCommand, NetworkSession


def operation_changes(item, values):
    allowed = {'outserv'}
    if item.get('group') == 'controllers':
        allowed.clear()
    if item.get('class') in ('StaSwitch', 'ElmCoup'):
        allowed.add('closed')
    if item.get('class') == 'ElmLod':
        allowed.add('load_scale')
    if item.get('class') in ('ElmSym', 'ElmGenstat', 'ElmPvsys'):
        allowed.update(('gen_p_mw', 'v_set_pu'))
    result = {}
    for key, value in values.items():
        if key not in allowed:
            raise ValueError(tr('integration.operation_unavailable_for_this_element'))
        if key in ('outserv', 'closed'):
            if type(value) not in (bool, int) or value not in (0, 1):
                raise ValueError(tr('integration.invalid_boolean_state'))
            result[key] = bool(value)
        else:
            if value is None or isinstance(value, bool) or isinstance(value, str) and not value.strip():
                raise ValueError(tr('integration.setpoint_is_empty_or_invalid'))
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(tr('integration.the_setpoint_must_be_finite'))
            if key == 'load_scale' and number < 0 or key == 'v_set_pu' and number <= 0:
                raise ValueError(tr('integration.load_scale_must_be_nonnegative_and_voltage'))
            result[key] = number
    return result


class Document:
    """Adaptador StudyCase v1 y dueño del documento de estudio v2."""
    def __init__(self, study, path=None):
        self.study = study
        self.path = confined_path(path) if path else None
        self.history = History()
        self.network = NetworkSession(self.history)
        self.data = v2.new_document(study.to_json())
        self._saved = deepcopy(study.to_json())
        self._saved_data = deepcopy(self.data)
        self._saved_network = None
        self._pending_network = None
        self._base = None
        self._disk_hash = file_sha256(self.path) if self.path and self.path.exists() else None
        self._v1_path = None
        self.network.subscribe(self._network_changed)

    def _network_changed(self, event):
        self.data['revision'] += 1

    @property
    def revision(self):
        return self.data['revision']

    @property
    def active_case_id(self):
        return self.data['active_case_id']

    @property
    def modified(self):
        network = self.network.to_data()
        if network and self._saved_network is None:
            network_dirty = bool(network['variant']['commands'] or network['drafts'])
        else:
            network_dirty = network != self._saved_network
        def content(data):
            result = deepcopy(data)
            result.pop('revision', None)
            for scenario in result['operating_scenarios']:
                scenario.pop('revision', None)
            return result
        return self.snapshot() != self._saved or content(self.data) != content(self._saved_data) or network_dirty

    def snapshot(self):
        return deepcopy(self.study.to_json())

    def _sync_study(self):
        case = v2.find(self.data['cases'], 'case_id', self.active_case_id)
        config = self.snapshot()
        operations = config.pop('scenario', {})
        config['scenario'] = {}
        case['study'], case['name'] = config, config['name']
        scenario = v2.find(self.data['operating_scenarios'], 'scenario_id', case['scenario_id'])
        if scenario['operations'] != operations:
            scenario['revision'] += 1
            scenario['operations'] = operations

    def _restore_study(self, state):
        self.study = type(self.study).from_json(state)
        self._sync_study()
        self.data['revision'] += 1

    def edit(self, callback):
        before = self.snapshot()
        try:
            callback(self.study)
            self.snapshot()
        except Exception:
            self.study = type(self.study).from_json(before)
            raise
        if before != self.snapshot():
            self._sync_study()
            self.data['revision'] += 1
            self.history.record('document', SnapshotCommand(self._restore_study, before, self.snapshot()))

    def operation(self, item, values):
        changes = operation_changes(item, values)
        def apply(study):
            keys = dict(outserv='outserv', closed='closed', load_scale='scale', gen_p_mw='p_mw', v_set_pu='v_set_pu')
            defaults = dict(outserv=False, closed=True, load_scale=1, v_set_pu=1)
            for field, value in changes.items():
                base = item.get(keys[field], defaults.get(field))
                if value == base:
                    study.scenario.get(field, {}).pop(item['id'], None)
                    if not study.scenario.get(field):
                        study.scenario.pop(field, None)
                else:
                    study.scenario.setdefault(field, {})[item['id']] = value
        self.edit(apply)

    def undo(self):
        return self.history.step('document')

    def redo(self):
        return self.history.step('document', redo=True)

    def _ensure_base(self):
        info = self.data['base']
        for path, key in ((info['model_path'], 'model_file_sha256'), (info['overrides_path'], 'overrides_sha256')):
            if path and file_sha256(path) != info[key]:
                raise ValueError(tr('integration.base_file_modified') + path)
        if self._base is None:
            if self.network.base is not None:
                self._base = deepcopy(self.network.base)
            else:
                from power_machine.study import load_model
                info = self.data['base']
                for path, key in ((info['model_path'], 'model_file_sha256'), (info['overrides_path'], 'overrides_sha256')):
                    if path and file_sha256(path) != info[key]:
                        raise ValueError(tr('integration.base_file_modified') + path)
                requested = info['model_path'] or getattr(self.study, 'ldf_options', {}).get('gui_model_path')
                self._base = load_model(v2.confined_path('.'), requested)
                if info['overrides_path']:
                    from power_machine.overrides import apply_overrides, read_overrides
                    self._base, _ = apply_overrides(self._base, read_overrides(confined_path(info['overrides_path'])))
                if not info['electrical_sha256']:
                    self.set_base(self._base, requested or 'power_machine/cases/ieee39/model.json',
                                  None)
        expected = self.data['base']['electrical_sha256']
        if expected and v2.model_sha256(self._base) != expected:
            raise ValueError(tr('integration.base_model_mismatch'))
        return deepcopy(self._base)

    def set_base(self, model, model_path=None, overrides_path=None):
        """Declara la base y su procedencia antes de crear alternativas."""
        info = self.data['base']
        candidate = dict(model_path=relative_path(model_path) if model_path else None,
            model_file_sha256=file_sha256(model_path) if model_path else None,
            overrides_path=relative_path(overrides_path) if overrides_path else None,
            overrides_sha256=file_sha256(overrides_path) if overrides_path else None,
            electrical_sha256=v2.model_sha256(model), source_package=model.get('meta', {}).get('source_package'))
        if self.data['network_variants'] and candidate['electrical_sha256'] != info['electrical_sha256']:
            raise ValueError(tr('integration.the_base_of_existing_alternatives_cannot_change'))
        self._base = deepcopy(model)
        self.data['base'] = candidate
        if model_path and not overrides_path and hasattr(self.study, 'ldf_options'):
            self.study.ldf_options['gui_model_path'] = relative_path(model_path)

    def materialize_case(self, case_id):
        self._sync_study()
        data = deepcopy(self.data)
        self._export_network(data)
        return v2.materialize(data, self._ensure_base(), case_id, type(self.study))

    def _export_network(self, data):
        """Integra las órdenes del editor antiguo al caso sin tocar su controlador."""
        network = self.network.to_data()
        if not network:
            return
        from uuid import UUID, uuid5, NAMESPACE_URL
        from dataclasses import replace, asdict
        from power_machine.netedit.schema import new_variant, VariantCommand, object_sha256
        from power_machine.netedit.commands import index_model, replay_commands
        from power_machine.netedit import materialize_variant
        source = network['variant']
        identity = str(uuid5(NAMESPACE_URL, source['variant_id']))
        stage_id = str(uuid5(UUID(identity), 'legacy-stage'))
        case = v2.find(data['cases'], 'case_id', self.active_case_id)
        if not source['commands']:
            # Deshacer hasta la base retira la etapa exportada por esta sesión.
            if case['variant_id'] == identity:
                case.update(variant_id=None, stage_id=None)
            return
        prior_variant_id, prior_stage_id = case['variant_id'], case['stage_id']
        if prior_variant_id == identity:
            prior_variant_id, prior_stage_id = None, None
        # Una sesión antigua sobre una etapa materializada agrega una etapa hija.
        parent_commands = []
        if prior_variant_id:
            identity = prior_variant_id
            stage_id = str(uuid5(UUID(identity), source['variant_id']))
            variant = v2.find(data['network_variants'], 'variant_id', identity)
            chain, sid = [], prior_stage_id
            while sid:
                parent = v2.find(variant['stages'], 'stage_id', sid)
                chain.insert(0, parent)
                sid = parent['parent_stage_id']
            parent_commands = [VariantCommand(**c) for s in chain for c in s['commands']]
        base = self._ensure_base()
        v = replace(new_variant(base, variant_id=identity), commands=tuple(parent_commands))
        current, _ = replay_commands(base, v)
        input_hash = v2.model_sha256(current)
        commands = []
        for raw in source['commands']:
            raw = deepcopy(raw)
            if raw['op'] == 'create':
                raw['data']['path'] = f"power_machine://variants/{identity}/{raw['object_id']}"
            elif raw.get('expected_sha256') is not None:
                raw['expected_sha256'] = object_sha256(index_model(current)[raw['object_id']][1])
            command = VariantCommand(**raw)
            commands.append(command)
            v = replace(v, commands=v.commands + (command,))
            current, _ = replay_commands(base, v)
        materialize_variant(base, v)
        stage = dict(stage_id=stage_id, name=tr('integration.network_editing'), effective_at='1970-01-01T00:00:00+00:00',
            sequence=0, parent_stage_id=prior_stage_id, input_electrical_sha256=input_hash,
            commands=[asdict(c) for c in commands], output_electrical_sha256=v2.model_sha256(current))
        if prior_variant_id:
            variant = v2.find(data['network_variants'], 'variant_id', identity)
            parent = v2.find(variant['stages'], 'stage_id', prior_stage_id)
            stage.update(sequence=parent['sequence'] + 1, effective_at=parent['effective_at'])
            variant['stages'] = [s for s in variant['stages'] if s['stage_id'] != stage_id] + [stage]
        else:
            data['network_variants'] = [v for v in data['network_variants'] if v['variant_id'] != identity]
            data['network_variants'].append(dict(variant_id=identity, name=source['name'],
                base_electrical_sha256=v2.model_sha256(base), stages=[stage]))
        case.update(variant_id=identity, stage_id=stage_id)

    def _restore_data(self, state):
        revision = self.revision + 1
        self.data = deepcopy(state)
        self.data['revision'] = revision
        case = v2.find(self.data['cases'], 'case_id', self.active_case_id)
        config = deepcopy(case['study'])
        config['scenario'] = deepcopy(v2.find(self.data['operating_scenarios'], 'scenario_id', case['scenario_id'])['operations'])
        self.study = type(self.study).from_json(config)
        if self._base is not None:
            self._load_case_network()

    def _transaction(self, mutate, expected_revision, label):
        if expected_revision != self.revision:
            raise ValueError(tr('integration.stale_document_revision'))
        self._sync_study()
        before, after = deepcopy(self.data), deepcopy(self.data)
        self._export_network(after)
        value = mutate(after)
        after['revision'] += 1
        v2.validate(after)
        if self._base is not None:
            v2.materialize(after, self._base, after['active_case_id'], type(self.study))
        self._restore_data(after)
        self.history.record('document', SnapshotCommand(self._restore_data, before, deepcopy(self.data)))
        return value

    def apply_operations(self, edits, expected_revision, label=tr('integration.batch_operation')):
        if edits and all(e['field'] in ('p_mw','q_mvar') and e['object_id'] in {l['id'] for l in (self.network.model or self._base)['loads']} for e in edits):
            try:
                if expected_revision != self.revision:raise ValueError(tr('integration.stale_network_revision'))
                from power_machine.netedit.commands import edit_command
                from power_machine.netedit.schema import variant_to_dict
                from power_machine.netedit import new_variant
                if self.network.state is None:
                    self.network.load_data(self._base,dict(schema='power_machine-gui-network-v1',variant=variant_to_dict(new_variant(self._base)),drafts={}))
                model=self.network.model
                loads={l['id']:l for l in model['loads']}
                updates={}
                for e in edits:
                    item=loads[e['object_id']]
                    field=e['field']
                    if e['unit'] != ('MW' if field=='p_mw' else 'Mvar'):raise ValueError(tr('data.error.unsupported_field'))
                    value=float(e['value'])
                    if not math.isfinite(value):raise ValueError(tr('data.error.finite'))
                    if item[field] != e.get('expected_value',item[field]):raise ValueError(tr('integration.stale_network_revision'))
                    param='plini' if field=='p_mw' else 'qlini'
                    data=updates.setdefault(e['object_id'],{'params':{}})
                    data[field]=value
                    data['params'][param]=value
                commands=[edit_command(model,'update','loads',identity,data) for identity,data in updates.items()]
                return self.network.commit(commands,self.network.revision,label)
            except (ValueError,KeyError,TypeError) as exc:
                return dict(accepted=False,diagnostics=[str(exc)])
        try:
            model, _, _ = self.materialize_case(self.active_case_id)
            from power_machine.netedit.commands import index_model
            index = index_model(model)
            aliases = dict(scale='load_scale', p_mw='gen_p_mw')
            units = dict(outserv='1', closed='1', load_scale='1', gen_p_mw='MW', v_set_pu='p.u.')
            changes = []
            for edit in edits:
                identity = edit['object_id']
                if identity not in index:
                    raise ValueError(tr('integration.unknown_id') + identity)
                group, element = index[identity]
                field = aliases.get(edit['field'], edit['field'])
                value, unit = edit['value'], edit['unit']
                if field not in units:
                    raise ValueError(tr('integration.unsupported_operating_field') + field)
                if unit == 'kW' and field == 'gen_p_mw':
                    if isinstance(value, bool):
                        raise ValueError(tr('integration.a_numeric_setpoint_cannot_be_boolean'))
                    value = float(value) / 1000 if value not in ('', None) else value
                elif unit not in (units[field], 'pu' if field == 'v_set_pu' else units[field]):
                    raise ValueError(tr('integration.incompatible_unit') + str(unit))
                current_field = dict(load_scale='scale', gen_p_mw='p_mw').get(field, field)
                if 'expected_value' in edit and edit['expected_value'] != element.get(current_field):
                    raise ValueError(tr('integration.previous_value_mismatch') + identity)
                value = operation_changes(dict(element, group=group), {field: value})[field]
                changes.append((identity, field, value))
            def mutate(data):
                case = v2.find(data['cases'], 'case_id', self.active_case_id)
                scenario = v2.find(data['operating_scenarios'], 'scenario_id', case['scenario_id'])
                for identity, field, value in changes:
                    scenario['operations'].setdefault(field, {})[identity] = value
                scenario['revision'] += 1
            self._transaction(mutate, expected_revision, label)
            return dict(accepted=True, revision=self.revision, changed_ids=sorted({x[0] for x in changes}),
                        electrical_changed=bool(changes), presentation_changed=False, diagnostics=[])
        except (ValueError, KeyError, TypeError) as exc:
            return dict(accepted=False, revision=self.revision, changed_ids=[], electrical_changed=False,
                        presentation_changed=False, diagnostics=[str(exc)])

    def activate_case(self, case_id, expected_revision):
        def mutate(data):
            v2.find(data['cases'], 'case_id', case_id)
            data['active_case_id'] = case_id
        self._ensure_base()
        self._transaction(mutate, expected_revision, tr('integration.activate_case'))
        return self.materialize_case(case_id)

    def duplicate_case(self, case_id, name, expected_revision):
        identity, scenario_id = v2.uid(), v2.uid()
        def mutate(data):
            case = deepcopy(v2.find(data['cases'], 'case_id', case_id))
            scenario = deepcopy(v2.find(data['operating_scenarios'], 'scenario_id', case['scenario_id']))
            scenario['scenario_id'] = scenario_id
            case.update(case_id=identity, name=name, scenario_id=scenario_id, result_ids=[])
            case['study']['name'] = name
            # Los contenidos editables se duplican con identidades independientes.
            copies = []
            for wid in case['workspace_ids']:
                w = deepcopy(v2.find(data['workspaces'], 'workspace_id', wid))
                w['workspace_id'] = v2.uid()
                copies.append(w)
            case['workspace_ids'] = [w['workspace_id'] for w in copies]
            data['workspaces'].extend(copies)
            data['cases'].append(case)
            data['operating_scenarios'].append(scenario)
        self._transaction(mutate, expected_revision, tr('integration.duplicate_case'))
        return identity

    def case_summary(self, case_id=None):
        """Resumen estable para el administrador sin exponer el JSON interno."""
        case = v2.find(self.data['cases'], 'case_id', case_id or self.active_case_id)
        scenario = v2.find(self.data['operating_scenarios'], 'scenario_id', case['scenario_id'])
        return dict(case_id=case['case_id'], name=case['name'], scenario_id=scenario['scenario_id'],
                    scenario_name=scenario['name'], variant_id=case['variant_id'],
                    stage_id=case['stage_id'], result_count=len(case['result_ids']),
                    active=case['case_id'] == self.active_case_id)

    def move_to_trash(self, kind, identity, expected_revision):
        """Retira un objeto del documento conservando su contenido para restaurarlo."""
        collections = {'case': ('cases', 'case_id'), 'scenario': ('operating_scenarios', 'scenario_id'),
                       'variant': ('network_variants', 'variant_id')}
        if kind not in collections:
            raise ValueError(tr('integration.unsupported_trash_entry_class'))
        collection, key = collections[kind]

        def mutate(data):
            item = deepcopy(v2.find(data[collection], key, identity))
            if kind == 'case':
                if len(data['cases']) == 1:
                    raise ValueError(tr('integration.the_document_must_retain_at_least_one'))
                links = [deepcopy(link) for link in data['result_links'] if link['case_id'] == identity]
                data['result_links'] = [link for link in data['result_links'] if link['case_id'] != identity]
                data['cases'] = [row for row in data['cases'] if row[key] != identity]
                if data['active_case_id'] == identity:
                    data['active_case_id'] = data['cases'][0]['case_id']
                payload = dict(item=item, result_links=links)
            else:
                reference = 'scenario_id' if kind == 'scenario' else 'variant_id'
                if any(case.get(reference) == identity for case in data['cases']):
                    raise ValueError(tr('integration.the_object_is_in_use_by_a'))
                data[collection] = [row for row in data[collection] if row[key] != identity]
                payload = dict(item=item)
            data.setdefault('trash', []).append(dict(kind=kind, deleted_at_utc=v2.now(), **payload))

        self._transaction(mutate, expected_revision, tr('integration.move_to_trash'))

    def restore_from_trash(self, index, expected_revision):
        """Restaura una entrada de papelera si su identidad aún está disponible."""
        def mutate(data):
            try:
                entry = data.get('trash', [])[index]
            except (IndexError, TypeError):
                raise ValueError(tr('integration.unknown_trash_entry')) from None
            kind = entry['kind']
            collection, key = {'case': ('cases', 'case_id'), 'scenario': ('operating_scenarios', 'scenario_id'),
                               'variant': ('network_variants', 'variant_id')}[kind]
            if any(row[key] == entry['item'][key] for row in data[collection]):
                raise ValueError(tr('integration.the_identity_already_exists_in_the_document'))
            data[collection].append(deepcopy(entry['item']))
            if kind == 'case':
                known = {link['run_id'] for link in data['result_links']}
                data['result_links'].extend(link for link in deepcopy(entry.get('result_links', []))
                                            if link['run_id'] not in known)
            data['trash'].pop(index)
        self._transaction(mutate, expected_revision, tr('integration.restore_from_trash'))

    def runs_for_case(self, case_id=None):
        """Devuelve corridas con vigencia calculada para el historial de la interfaz."""
        case_id = case_id or self.active_case_id
        rows = []
        for link in self.data['result_links']:
            if link['case_id'] == case_id:
                row = deepcopy(link)
                row['freshness'] = self.result_freshness(link['run_id'])
                rows.append(row)
        return sorted(rows, key=lambda row: row['created_at_utc'], reverse=True)

    def create_scenario(self, name, operations, expected_revision):
        identity = v2.uid()
        self._transaction(lambda d: d['operating_scenarios'].append(dict(scenario_id=identity,
            name=name, revision=0, operations=deepcopy(operations))), expected_revision, tr('integration.create_scenario'))
        return identity

    def configure_case(self, case_id, expected_revision, **changes):
        allowed = {'scenario_id', 'variant_id', 'stage_id', 'calculation_presets', 'workspace_ids', 'name'}
        if set(changes) - allowed:
            raise ValueError(tr('integration.case_field_cannot_be_edited'))
        def mutate(data):
            case = v2.find(data['cases'], 'case_id', case_id)
            case.update(deepcopy(changes))
            if 'name' in changes:
                case['study']['name'] = changes['name']
            v2.materialize(data, self._ensure_base(), case_id, type(self.study))
        self._transaction(mutate, expected_revision, tr('integration.configure_case'))

    def create_variant(self, name, expected_revision):
        identity = v2.uid()
        base = self._ensure_base()
        self._transaction(lambda d: d['network_variants'].append(dict(variant_id=identity, name=name,
            base_electrical_sha256=v2.model_sha256(base), stages=[])), expected_revision, tr('integration.create_alternative'))
        return identity

    def add_stage(self, variant_id, name, effective_at, commands, expected_revision, parent_stage_id=None):
        identity = v2.uid()
        def mutate(data):
            variant = v2.find(data['network_variants'], 'variant_id', variant_id)
            case = dict(variant_id=variant_id if parent_stage_id else None, stage_id=parent_stage_id)
            input_model = v2.structural_model(data, self._ensure_base(), case)
            stage = dict(stage_id=identity, name=name, effective_at=effective_at,
                sequence=max([s['sequence'] for s in variant['stages']], default=-1) + 1,
                parent_stage_id=parent_stage_id, input_electrical_sha256=v2.model_sha256(input_model),
                commands=deepcopy(commands), output_electrical_sha256=None)
            # Reutiliza toda la cadena para preservar identidades sintéticas de la alternativa.
            chain, sid = [], parent_stage_id
            while sid:
                parent = v2.find(variant['stages'], 'stage_id', sid)
                chain.insert(0, parent)
                sid = parent['parent_stage_id']
            from dataclasses import replace
            from power_machine.netedit import materialize_variant
            from power_machine.netedit.schema import VariantCommand, new_variant
            combined = [VariantCommand(**c) for s in chain + [stage] for c in s['commands']]
            output = materialize_variant(self._ensure_base(), replace(new_variant(self._ensure_base(),
                variant_id=variant_id), commands=tuple(combined)))
            stage['output_electrical_sha256'] = v2.model_sha256(output)
            variant['stages'].append(stage)
        self._transaction(mutate, expected_revision, tr('integration.add_stage'))
        return identity

    def put_workspace(self, kind, content, bindings, expected_revision, workspace_id=None):
        identity = workspace_id or v2.uid()
        def mutate(data):
            old = next((w for w in data['workspaces'] if w['workspace_id'] == identity), None)
            new = workspace(kind, content, bindings, identity, old['revision'] + 1 if old else 0)
            if old:
                data['workspaces'].remove(old)
            data['workspaces'].append(new)
        self._transaction(mutate, expected_revision, tr('integration.edit_workspace'))
        return identity

    def attach_result(self, link):
        link = deepcopy(link)
        required = ('run_id', 'case_id', 'module', 'panel_id', 'created_at_utc', 'result_path',
                    'result_file_sha256', 'schema', 'status', 'raw_status', 'cancelled', 'dependencies',
                    'calculation_sha256', 'validation', 'coverage')
        if any(k not in link for k in required):
            raise ValueError(tr('integration.incomplete_result_link'))
        if link['status'] not in ('completed', 'partial', 'unsupported', 'invalid_input', 'error'):
            raise ValueError(tr('integration.invalid_public_state'))
        if link['cancelled'] and link['status'] == 'completed':
            raise ValueError(tr('integration.a_cancelled_run_is_incomplete'))
        if file_sha256(link['result_path']) != link['result_file_sha256'] or calculation_sha256(link['dependencies']) != link['calculation_sha256']:
            raise ValueError(tr('integration.result_fingerprint_mismatch'))
        link['result_path'] = relative_path(link['result_path'])
        def mutate(data):
            case = v2.find(data['cases'], 'case_id', link['case_id'])
            if any(r['run_id'] == link['run_id'] for r in data['result_links']):
                raise ValueError(tr('integration.duplicate_run_identity'))
            data['result_links'].append(link)
            case['result_ids'].append(link['run_id'])
        self._transaction(mutate, self.revision, tr('integration.link_run'))
        return link['run_id']

    def result_freshness(self, run_id):
        link = v2.find(self.data['result_links'], 'run_id', run_id)
        try:
            _, _, dependencies = self.materialize_case(link['case_id'])
        except (ValueError, OSError):
            return 'unknown'
        return check_link(link, dependencies, self.active_case_id)

    def _load_case_network(self):
        case = v2.find(self.data['cases'], 'case_id', self.active_case_id)
        model = v2.structural_model(self.data, self._ensure_base(), case)
        # El adaptador antiguo aplica el escenario desde study, nunca aquí.
        from power_machine.netedit.schema import new_variant, variant_to_dict
        from dataclasses import replace
        saved = self.data['presentation'].get('network_sessions', {}).get(self.active_case_id, {})
        if (saved.get('session') and saved.get('variant_id') == case['variant_id']
                and saved.get('stage_id') == case['stage_id']):
            self.network.detach()
            self.network.load_data(saved['base_model'], saved['session'])
            return
        variant = new_variant(model)
        if saved.get('layout'):
            variant = replace(variant, layout=deepcopy(saved['layout']))
        self.network.detach()
        self.network.load_data(model, {'schema': 'power_machine-gui-network-v1',
            'variant': variant_to_dict(variant), 'drafts': deepcopy(saved.get('drafts', {}))})

    def restore_network(self, base):
        self._base = deepcopy(base)
        self._ensure_base()
        if self._pending_network is not None:
            self.network.load_data(base, self._pending_network)
            self._saved_network = self.network.to_data()
            self._pending_network = None
            if self._v1_path:
                self._export_network(self.data)
                self._saved_data = deepcopy(self.data)

    def to_data(self):
        self._sync_study()
        data = deepcopy(self.data)
        self._export_network(data)
        return data

    def save(self, path=None):
        target = confined_path(path) if path else self.path
        if target is None:
            raise ValueError(tr('integration.select_a_study_file'))
        if self._v1_path == target:
            raise ValueError(tr('integration.save_the_v2_migration_to_another_file'))
        self._sync_study()
        generic = self.snapshot().get('schema') is None
        base = {} if generic else self._ensure_base()
        candidate = deepcopy(self.data)
        # Sin ruta fuente, incluir una copia pequeña permite reabrir bases analíticas.
        if not candidate['base']['model_path']:
            requested = getattr(self.study, 'ldf_options', {}).get('gui_model_path')
            if requested:
                self.set_base(base, requested)
                candidate['base'] = deepcopy(self.data['base'])
            else:
                candidate['extensions']['power_machine:embedded_base'] = deepcopy(base)
                candidate['base']['electrical_sha256'] = v2.model_sha256(base)
        network = self.network.to_data()
        if not generic:
            self._export_network(candidate)
        candidate['extensions']['power_machine:legacy_network'] = deepcopy(network) if self.network.base == base else None
        if network:
            saved_session = dict(
                drafts=deepcopy(network['drafts']), layout=deepcopy(self.network.state['layout']))
            if self.network.base != base:
                case = v2.find(candidate['cases'], 'case_id', self.active_case_id)
                saved_session.update(session=deepcopy(network), base_model=deepcopy(self.network.base),
                                     variant_id=case['variant_id'], stage_id=case['stage_id'])
            candidate['presentation'].setdefault('network_sessions', {})[self.active_case_id] = saved_session
        if not generic:
            v2.validate_models(candidate, base, type(self.study))
        candidate['revision'] += 1
        expected = self._disk_hash if target == self.path else None
        disk_hash = v2.atomic_save(target, candidate, expected)
        self.data, self.path, self._disk_hash = candidate, target, disk_hash
        self._saved, self._saved_data = self.snapshot(), deepcopy(self.data)
        self._saved_network = deepcopy(network)

    @classmethod
    def open(cls, path, study_class):
        path = confined_path(path)
        data = read_json(path)
        if data.get('schema') == v2.SCHEMA:
            v2.validate(data)
            case = v2.find(data['cases'], 'case_id', data['active_case_id'])
            config = deepcopy(case['study'])
            config['scenario'] = deepcopy(v2.find(data['operating_scenarios'], 'scenario_id', case['scenario_id'])['operations'])
            document = cls(study_class.from_json(config), path)
            document.data = deepcopy(data)
            document._base = deepcopy(data['extensions'].get('power_machine:embedded_base'))
            document._ensure_base()
            if config.get('schema'):
                v2.validate_models(document.data, document._base, study_class)
            network = data['extensions'].get('power_machine:legacy_network')
            if network:
                document.network.load_data(document._base, network)
            elif case['variant_id']:
                document._load_case_network()
            document._saved_network = document.network.to_data()
            document._saved_data = deepcopy(document.data)
            return document
        if data.get('schema') != 'power_machine-study-v1':
            raise ValueError(tr('integration.unknown_study_schema'))
        document = cls(study_class.from_json(data), path)
        document._v1_path = path
        document.data['provenance']['migration'] = dict(source_schema='power_machine-study-v1', source_path=relative_path(path), source_sha256=file_sha256(path))
        lateral = confined_path(str(path) + '.network.json')
        if lateral.exists():
            payload = read_json(lateral)
            if payload.get('schema') != 'power_machine-gui-study-network-v1' or payload.get('study_sha256') != v2.value_sha256(data):
                raise ValueError(tr('integration.network_sidecar_does_not_match_the_saved'))
            document._pending_network = deepcopy(payload['network'])
        document._saved_data = deepcopy(document.data)
        return document


