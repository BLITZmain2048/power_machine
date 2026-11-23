"""Adaptador de eventos del editor sobre la fachada pública NetworkSession."""
from power_machine.gui.i18n import tr
from copy import deepcopy
from power_machine.gui.network_session import NetworkSession
from power_machine.netedit import create_command, edit_command, model_sha256, value_sha256
from power_machine.netedit.schema import VariantCommand, variant_from_dict, variant_sha256, PORTS
from power_machine.netedit.commands import IDENTITY, native_port, index_model, port_field, merge_fields
from power_machine.netedit.storage import atomic_json, read_json
from power_machine.lf_status import energized_status


class EditorController:
    def __init__(self, context, session=None):
        self.context = context
        self.session = session or getattr(context, 'network_session', None)
        if self.session is None:
            self.session = NetworkSession(base=context.get_snapshot()['model'])
        self.selected_id = None
        self.last_result = None
        self.flow_status = ''
        self.changed = lambda: None
        self.clipboard = None
        self.token = self.session.subscribe(self.on_change)

    def close(self):
        self.session.unsubscribe(self.token)

    @property
    def base(self):
        return self.session.snapshot()['base_model']

    @property
    def model(self):
        return self.session.snapshot()['model']

    @property
    def layout(self):
        return self.session.snapshot()['layout']

    @property
    def variant(self):
        return variant_from_dict(self.session.snapshot()['variant'])

    @property
    def drafts(self):
        return self.session.snapshot()['drafts']

    def on_change(self, event):
        if event['electrical_changed'] or event['reason'] == 'Abrir variante':
            self.last_result = None
            self.flow_status = (tr('integration.result_stale_after_variant_change') if event['reason'] == 'Abrir variante'
                                else tr('integration.result_stale_after_electrical_editing'))
        self.changed()

    def select(self, object_id):
        self.selected_id = object_id
        self.context.select_element(object_id)

    def checked(self, result):
        if not result['accepted']:
            raise ValueError(', '.join(result.get('diagnostics', [])) or tr('integration.no_history_changes'))
        return result

    def apply(self, commands):
        commands = list(commands) if isinstance(commands, (list, tuple)) else [commands]
        revision = self.session.snapshot()['revision']
        self.checked(self.session.preview(commands, revision))
        return self.checked(self.session.commit(commands, revision, tr('integration.graphical_editing')))

    def create(self, group, data, *, draft=False):
        command = create_command(self.variant.variant_id, group, data)
        if draft:
            self.checked(self.session.set_draft(command.object_id, dict(command.data, group=group),
                                               self.session.snapshot()['revision']))
        else:
            self.apply(command)
        return command.object_id

    def complete_draft(self, object_id, data):
        draft = self.drafts[object_id]
        item = deepcopy(data)
        for key in IDENTITY | {'origin'}:
            if key in draft:
                item[key] = deepcopy(draft[key])
        item.pop('editor_position', None)
        item.pop('group', None)
        command = VariantCommand('create', draft['group'], object_id, item)
        commands=[command]
        if draft.get('editor_position'):
            x,y=draft['editor_position']
            commands.append(VariantCommand('move',draft['group'],object_id,
                dict(x_canvas=x,y_canvas=y,diagram_group='__editor__')))
        revision = self.session.snapshot()['revision']
        self.checked(self.session.preview(commands, revision))
        self.checked(self.session.commit(commands, revision, tr('integration.complete_draft'),
                                         draft_updates={object_id: None}))
        return object_id

    def edit(self, object_id, changes):
        if object_id in self.drafts:
            data = deepcopy(self.drafts[object_id])
            merge_fields(data,changes)
            self.checked(self.session.set_draft(object_id, data, self.session.snapshot()['revision']))
        else:
            item = self.session.get_element(object_id)
            self.apply(edit_command(self.model, 'update', item['group'], object_id, changes))

    def ports(self, object_id):
        if object_id not in self.drafts:
            return self.session.get_ports(object_id)
        item = self.drafts[object_id]
        return [dict(name=native_port(item['group'], item, f), field=f, bus_id=item.get(f))
                for f in PORTS[item['group']]]

    def move(self, object_id, x_canvas, y_canvas, diagram_group='__editor__'):
        if object_id in self.drafts:
            return self.edit(object_id, {'editor_position': [x_canvas, y_canvas]})
        item = self.session.get_element(object_id)
        self.apply(edit_command(self.model, 'move', item['group'], object_id,
                               dict(x_canvas=x_canvas, y_canvas=y_canvas, diagram_group=diagram_group)))

    def reconnect(self, object_id, port, bus_id=None):
        if object_id in self.drafts:
            item = self.drafts[object_id]
            return self.edit(object_id, {port_field(item['group'], item, port): bus_id})
        item = self.session.get_element(object_id)
        data = {'port': port}
        if bus_id is not None:
            data['bus_id'] = bus_id
        self.apply(edit_command(self.model, 'connect' if bus_id else 'disconnect', item['group'], object_id, data))

    def disconnect(self,object_id):
        """Disconnect all ports with one undo step and canonical object hashes."""
        if object_id in self.drafts:
            return self.edit(object_id,{p['field']:None for p in self.ports(object_id)})
        from dataclasses import replace
        from power_machine.netedit.commands import replay_commands
        model=self.model
        commands=[]
        for port in self.ports(object_id):
            group,item=index_model(model)[object_id]
            commands.append(edit_command(model,'disconnect',group,object_id,{'port':port['field']}))
            model,_=replay_commands(self.base,replace(self.variant,commands=self.variant.commands+tuple(commands)))
        if commands:return self.apply(commands)

    def retire(self, object_id):
        if object_id in self.drafts:
            self.checked(self.session.set_draft(object_id, None, self.session.snapshot()['revision']))
        else:
            item = self.session.get_element(object_id)
            self.apply(edit_command(self.model, 'retire', item['group'], object_id))

    def retire_many(self, object_ids):
        """Retire a selection as one reversible transaction, including drafts."""
        commands=[]
        drafts={}
        for identity in dict.fromkeys(object_ids):
            if identity in self.drafts:drafts[identity]=None
            else:
                item=self.session.get_element(identity)
                commands.append(edit_command(self.model,'retire',item['group'],identity))
        revision=self.session.snapshot()['revision']
        self.checked(self.session.preview(commands,revision))
        return self.checked(self.session.commit(commands,revision,tr('edit.delete'),draft_updates=drafts))

    def undo(self):
        return self.session.undo(self.session.snapshot()['revision'])

    def redo(self):
        return self.session.redo(self.session.snapshot()['revision'])

    def copy(self):
        item = self.drafts.get(self.selected_id) or self.session.get_element(self.selected_id)
        if item is None:
            raise ValueError(tr('integration.select_an_element_to_copy'))
        self.clipboard = deepcopy(item)

    def paste(self):
        if not self.clipboard:
            raise ValueError(tr('integration.no_copied_element'))
        data = deepcopy(self.clipboard)
        group = data.pop('group')
        data['name'] = data.get('name', '') + tr('integration.copy')
        return self.create(group, data, draft=True)

    def differences(self):
        before, after = index_model(self.base), index_model(self.model)
        rows = []
        def flatten(item, prefix=''):
            result = {}
            for key, value in item.items():
                if isinstance(value, dict):
                    result.update(flatten(value, prefix + key + '.'))
                else:
                    result[prefix + key] = value
            return result
        for iid in sorted(set(before) | set(after)):
            old = flatten(before.get(iid, ('', {}))[1])
            new = flatten(after.get(iid, ('', {}))[1])
            for field in sorted(set(old) | set(new)):
                if old.get(field) != new.get(field):
                    rows.append((iid, after.get(iid, before.get(iid))[1].get('name', iid),
                                 field, old.get(field), new.get(field)))
        return rows

    def save(self, path):
        atomic_json(self.session.to_data(), path)

    def open(self, path):
        self.checked(self.session.replace_data(self.base, read_json(path),
                                               self.session.snapshot()['revision']))

    def submit(self, *, run_loadflow=False):
        self.study = deepcopy(self.context.get_snapshot().get('study', {}))
        request = {'schema': 'power_machine-parity-request-v1', 'module': 'netedit', 'root': str(self.context.root),
                   'snapshot': {'model': self.base, 'study': self.study,
                       'fingerprints': {'model_sha256': model_sha256(self.base),
                                        'study_sha256': value_sha256(self.study),
                                        'variant_sha256': variant_sha256(self.variant)}},
                   'options': {'run_loadflow': run_loadflow}, 'input': {'variant': self.session.snapshot()['variant']}}
        self.ticket = (self.session.snapshot()['revision'], value_sha256(self.study),
                       model_sha256(self.context.get_snapshot()['model']))
        return self.context.submit_job('netedit', request, self.on_message)

    def on_message(self, message):
        if message[0] == 'result':
            result = message[1]
            current = self.context.get_snapshot()
            if (self.ticket != (self.session.snapshot()['revision'], value_sha256(current.get('study', {})),
                                model_sha256(current['model']))
                    or result.get('variant_sha256') != variant_sha256(self.variant)
                    or result.get('model_sha256') != model_sha256(self.base)
                    or result.get('study_sha256') != value_sha256(self.study)):
                self.context.log('WARNING', tr('integration.result_from_an_earlier_revision_discarded'))
                return
            self.last_result = deepcopy(result)
            lf = result.get('payload', {}).get('loadflow')
            self.flow_status = energized_status(lf)['message'] if lf else str(result.get('diagnostics', []))
            self.context.publish_result(result)
            self.changed()
        elif message[0] == 'error':
            self.flow_status = str(message[1])
            self.context.log('ERROR', self.flow_status)
            self.changed()
