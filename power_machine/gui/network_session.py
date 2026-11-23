"""Sesión pública de red y adaptador del controlador heredado."""
from power_machine.gui.i18n import tr
from copy import deepcopy
from power_machine.netedit import VariantHistory, new_variant, materialized_layout
from power_machine.netedit.schema import (variant_to_dict, variant_from_dict, model_sha256,
                                     value_sha256, VariantCommand, PORTS)
from power_machine.netedit.commands import index_model, native_port


class _ControllerHistory:
    """Adapta solo el protocolo público de VariantHistory."""
    def __init__(self, history, session, restoring=False):
        self.inner, self.session, self.restoring = history, session, restoring

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def undo(self):
        if not self.restoring:
            self.session.undo()
        return self.inner.variant

    def redo(self):
        if not self.restoring:
            self.session.redo()
        return self.inner.variant


class NetworkSession:
    def __init__(self, history=None, base=None):
        if history is None:
            from .history import History
            history = History()
        self.history = history
        self.controller = None
        self.state = None
        self.base = deepcopy(base)
        self.revision = 0
        self.on_change = lambda: None
        self._refresh = lambda: None
        self._listeners = {}
        self._restoring = False
        if base is not None:
            self.load_data(base, {'schema': 'power_machine-gui-network-v1',
                                 'variant': variant_to_dict(new_variant(base)), 'drafts': {}})

    @staticmethod
    def capture(controller):
        model = controller.model
        return deepcopy(dict(base_model=controller.base, history=controller.history.to_dict(), model=model,
                             layout=controller.layout,
                             drafts={k: v for k, v in controller.drafts.items()
                                     if k not in index_model(model)}))

    @property
    def model(self):
        return deepcopy(self.state['model']) if self.state else deepcopy(self.base)

    def _variant(self):
        h = self.state['history']
        return variant_from_dict(h['snapshots'][h['cursor']])

    def to_data(self):
        if self.state is None:
            return None
        bases, states = {}, {}
        def encode(state):
            base = state.get('base_model', self.base)
            base_id = value_sha256(base)
            if base_id not in bases:
                bases[base_id] = deepcopy(base)
            packed = dict(base_sha256=base_id, history=deepcopy(state['history']),
                          drafts=deepcopy(state['drafts']))
            identity = value_sha256(packed)
            states[identity] = packed
            return identity
        stacks = self.history.export_domain('network', encode)
        transactions = dict(schema='power_machine-network-transactions-v1', bases=bases, states=states, **stacks)
        return {'schema': 'power_machine-gui-network-v1', 'variant': variant_to_dict(self._variant()),
                'drafts': deepcopy(self.state['drafts']), 'history': deepcopy(self.state['history']),
                'transactions': transactions}

    @staticmethod
    def _decode_state(base, data):
        if data.get('schema') != 'power_machine-gui-network-v1':
            raise ValueError(tr('integration.incompatible_network'))
        h = (VariantHistory.from_dict(base, data['history']) if data.get('history') else
             VariantHistory(base, variant_from_dict(data['variant'])))
        if variant_to_dict(h.variant) != data['variant']:
            raise ValueError(tr('integration.history_and_variant_mismatch'))
        drafts = deepcopy(data.get('drafts', {}))
        if not isinstance(drafts, dict):
            raise ValueError(tr('integration.incompatible_drafts'))
        value_sha256(drafts)
        return dict(base_model=deepcopy(base), history=h.to_dict(), model=h.model,
                    layout=materialized_layout(base, h.variant), drafts=drafts)

    def _decode_transactions(self, base, data, current):
        packed = data.get('transactions')
        if packed is None:
            # Compatibilidad con archivos que solo guardaban VariantHistory.
            engine = current['history']
            states = []
            for cursor, variant in enumerate(engine['snapshots']):
                history = dict(engine, cursor=cursor)
                state = self._decode_state(base, dict(schema='power_machine-gui-network-v1',
                    variant=variant, history=history, drafts=current['drafts']))
                states.append(state)
            orders = [dict(before=a, after=b) for a, b in zip(states, states[1:])]
            cursor = engine['cursor']
            return dict(undo=orders[:cursor], redo=list(reversed(orders[cursor:])))
        if packed.get('schema') != 'power_machine-network-transactions-v1':
            raise ValueError(tr('integration.incompatible_transaction_history'))
        decoded = {}
        for identity, state in packed['states'].items():
            if value_sha256(state) != identity:
                raise ValueError(tr('integration.history_state_modified'))
            state_base = packed['bases'][state['base_sha256']]
            if value_sha256(state_base) != state['base_sha256']:
                raise ValueError(tr('integration.history_base_modified'))
            history = state['history']
            variant = history['snapshots'][history['cursor']]
            decoded[identity] = self._decode_state(state_base, dict(schema='power_machine-gui-network-v1',
                variant=variant, history=history, drafts=state['drafts']))
        result = {name: [dict(before=deepcopy(decoded[c['before']]), after=deepcopy(decoded[c['after']]))
                         for c in packed[name]] for name in ('undo', 'redo')}
        chain = result['undo'] + list(reversed(result['redo']))
        if any(a['after'] != b['before'] for a, b in zip(chain, chain[1:])):
            raise ValueError(tr('integration.discontinuous_transaction_history'))
        if (result['undo'] and result['undo'][-1]['after'] != current
                or result['redo'] and result['redo'][-1]['before'] != current):
            raise ValueError(tr('integration.history_does_not_match_the_current_state'))
        return result

    def load_data(self, base, data):
        """Carga inicial comprobada, restaura pilas sin registrar una apertura."""
        state = self._decode_state(base, data)
        stacks = self._decode_transactions(base, data, state)
        self.restore(state, notify=False)
        self.history.load_domain('network', stacks, self.restore)

    def replace_data(self, base, data, expected_revision, label=tr('integration.open_variant')):
        """Abre sobre una sesión existente como una sola orden reversible."""
        try:
            if expected_revision != self.revision:
                raise ValueError(tr('integration.stale_network_revision'))
            if self.snapshot()['busy']:
                raise ValueError(tr('integration.network_busy'))
            state = self._decode_state(base, data)
            self._decode_transactions(base, data, state)
            before = deepcopy(self.state)
            from .history import SnapshotCommand
            self.restore(state, notify=False)
            self.history.record('network', SnapshotCommand(self.restore, before, state))
            self._notify(before, label)
            return dict(accepted=True, revision=self.revision, changed_ids=sorted(index_model(self.model)),
                electrical_changed=model_sha256(before['model']) != model_sha256(self.model),
                presentation_changed=before['layout'] != state['layout'] or before['drafts'] != state['drafts'], diagnostics=[])
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            return dict(accepted=False, revision=self.revision, diagnostics=[str(exc)])

    def snapshot(self):
        return deepcopy(dict(revision=self.revision, base_model=self.base, model=self.model,
            variant=variant_to_dict(self._variant()) if self.state else None,
            layout=self.state['layout'] if self.state else {}, drafts=self.state['drafts'] if self.state else {},
            electrical_sha256=model_sha256(self.model),
            layout_sha256=value_sha256(self.state['layout'] if self.state else {}),
            can_undo=self.history.can_step('network'), can_redo=self.history.can_step('network', redo=True),
            busy=bool(getattr(self.controller, 'busy', False))))

    def list_elements(self, group=None, diagram_group=None):
        result = []
        for g, item in index_model(self.model or {}).values():
            if group is not None and group != g:
                continue
            if diagram_group is not None:
                positions = self.state['layout'].get('groups', {}) if self.state else {}
                if diagram_group in positions:
                    if item['id'] not in positions[diagram_group]:
                        continue
                elif item.get('diagram_group', item.get('substation')) != diagram_group:
                    continue
            result.append(dict(deepcopy(item), group=g))
        return result

    def get_element(self, object_id):
        return next((x for x in self.list_elements() if x['id'] == object_id), None)

    def get_ports(self, object_id):
        item = self.get_element(object_id)
        if item is None:
            return []
        fields = ('bus1',) if item.get('class') == 'StaSwitch' else PORTS[item['group']]
        return [dict(name=native_port(item['group'], item, f), field=f, bus_id=item.get(f),
                     compatible_classes=['ElmTerm'], voltage_kv=(self.get_element(item[f]) or {}).get('un_kv')
                     if item.get(f) else None,
                     diagnostics=[] if item.get(f) else [tr('integration.disconnected_port')]) for f in fields]

    def subscribe(self, callback):
        token = object()
        self._listeners[token] = callback
        return token

    def unsubscribe(self, token):
        self._listeners.pop(token, None)

    def _notify(self, before, reason):
        self.revision += 1
        event = dict(revision=self.revision, domain='network', changed_ids=sorted(set(index_model(self.model or {}))),
            electrical_changed=model_sha256(before.get('model')) != model_sha256(self.model),
            presentation_changed=(before.get('layout') != self.state['layout']
                                  or before.get('drafts') != self.state['drafts']), reason=reason)
        self.on_change()
        self._refresh()
        for callback in list(self._listeners.values()):
            callback(deepcopy(event))

    def attach(self, controller, on_change):
        self.controller, self.on_change, self._refresh = controller, on_change, controller.changed
        if self.state is None:
            self.base, self.state = deepcopy(controller.base), self.capture(controller)
        else:
            self.restore(self.state, notify=False)
        controller.history = _ControllerHistory(controller.history, self)
        controller.changed = self.observe
        self._refresh()

    def observe(self):
        if self._restoring:
            return
        current = self.capture(self.controller)
        if not isinstance(self.controller.history, _ControllerHistory):
            self.controller.history = _ControllerHistory(self.controller.history, self)
        if current != self.state:
            from .history import SnapshotCommand
            before = deepcopy(self.state)
            self.state = current
            self.history.record('network', SnapshotCommand(self.restore, before, current))
            self._notify(before, tr('integration.network_editing'))
        else:
            self._refresh()

    def restore(self, state, *, notify=True):
        before = self.state or {}
        state_base = state.get('base_model', self.base)
        c = self.controller
        if c is not None:
            h = VariantHistory.from_dict(state_base, state['history'])
            previous = c.history
            c.history = _ControllerHistory(h, self, restoring=True)
            self._restoring = True
            try:
                # El método público verifica trabajo pendiente y actualiza sus cachés.
                c.undo()
                c.drafts = deepcopy(state['drafts'])
            except Exception:
                c.history = previous
                raise
            finally:
                self._restoring = False
            c.history = _ControllerHistory(h, self)
        self.base = deepcopy(state_base)
        self.state = deepcopy(state)
        if notify:
            self._notify(before, tr('integration.network_history'))

    def detach(self):
        if self.controller is not None:
            self.controller.changed = lambda: None
            if isinstance(self.controller.history, _ControllerHistory):
                self.controller.history = self.controller.history.inner
        self.controller = None
        self._refresh = lambda: None

    def _proposal(self, commands, expected_revision):
        if expected_revision != self.revision:
            raise ValueError(tr('integration.stale_network_revision'))
        if self.snapshot()['busy']:
            raise ValueError(tr('integration.network_busy'))
        h = VariantHistory.from_dict(self.base, self.state['history'])
        commands = [VariantCommand(**deepcopy(c)) if isinstance(c, dict) else c for c in commands]
        h.apply(commands)
        candidate = dict(base_model=deepcopy(self.base), history=h.to_dict(), model=h.model,
                         layout=materialized_layout(self.base, h.variant), drafts=deepcopy(self.state['drafts']))
        return candidate, [c.object_id for c in commands]

    def preview(self, commands, expected_revision):
        try:
            state, ids = self._proposal(commands, expected_revision)
            return dict(accepted=True, revision=self.revision, changed_ids=ids,
                electrical_changed=model_sha256(self.model) != model_sha256(state['model']),
                presentation_changed=self.state['layout'] != state['layout'], diagnostics=[])
        except (ValueError, TypeError, KeyError) as exc:
            return dict(accepted=False, revision=self.revision, changed_ids=[], electrical_changed=False,
                        presentation_changed=False, diagnostics=[str(exc)])

    def commit(self, commands, expected_revision, label=tr('integration.network_editing'), *, draft_updates=None):
        result = self.preview(commands, expected_revision)
        if not result['accepted']:
            return result
        state, _ = self._proposal(commands, expected_revision)
        try:
            for identity, data in (draft_updates or {}).items():
                if data is None:
                    state['drafts'].pop(identity, None)
                else:
                    value_sha256(data)
                    state['drafts'][identity] = deepcopy(data)
        except (ValueError, TypeError, AttributeError) as exc:
            return dict(accepted=False, revision=self.revision, diagnostics=[str(exc)])
        from .history import SnapshotCommand
        before = deepcopy(self.state)
        try:
            self.restore(state, notify=False)
        except ValueError as exc:
            return dict(accepted=False, revision=self.revision, changed_ids=[], electrical_changed=False,
                        presentation_changed=False, diagnostics=[str(exc)])
        self.history.record('network', SnapshotCommand(self.restore, before, state))
        self._notify(before, label)
        result['presentation_changed'] = (result['presentation_changed']
                                          or before['drafts'] != state['drafts'])
        result['revision'] = self.revision
        return result

    def set_draft(self, draft_id, data, expected_revision):
        if expected_revision != self.revision:
            return dict(accepted=False, revision=self.revision, diagnostics=[tr('integration.stale_revision')])
        from .history import SnapshotCommand
        before, after = deepcopy(self.state), deepcopy(self.state)
        if data is None:
            after['drafts'].pop(draft_id, None)
        else:
            value_sha256(data)
            after['drafts'][draft_id] = deepcopy(data)
        self.restore(after, notify=False)
        self.history.record('network', SnapshotCommand(self.restore, before, after))
        self._notify(before, tr('integration.draft_editing'))
        return dict(accepted=True, revision=self.revision, changed_ids=[draft_id],
                    electrical_changed=False, presentation_changed=True, diagnostics=[])

    def step(self, redo=False):
        # restore verifica también el estado pendiente del controlador heredado.
        if self.controller is not None:
            c = self.controller
            h = c.history
            c.history = _ControllerHistory(VariantHistory.from_dict(self.base, self.state['history']), self, True)
            self._restoring = True
            try:
                c.undo()
            finally:
                self._restoring = False
                c.history = h
        return self.history.step('network', redo=redo)

    def _step_result(self, expected_revision, redo):
        if expected_revision is None:
            return self.step(redo)
        if expected_revision != self.revision:
            return dict(accepted=False, revision=self.revision, diagnostics=[tr('integration.stale_revision')])
        before = self.snapshot()
        accepted = self.step(redo)
        after = self.snapshot()
        return dict(accepted=accepted, revision=self.revision, changed_ids=[], diagnostics=[],
            electrical_changed=before['electrical_sha256'] != after['electrical_sha256'],
            presentation_changed=before['layout_sha256'] != after['layout_sha256'])

    def undo(self, expected_revision=None):
        return self._step_result(expected_revision, False)

    def redo(self, expected_revision=None):
        return self._step_result(expected_revision, True)
