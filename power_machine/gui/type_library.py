"""Tipos fuente de solo lectura y copias propias aplicadas mediante comandos."""
from copy import deepcopy
from uuid import uuid4
from power_machine.netedit.commands import index_model, edit_command
from power_machine.netedit.schema import value_sha256
from .i18n import tr


def source_types(model):
    result = {}
    for _, item in index_model(model).values():
        identity = item.get('type_id')
        if identity:
            candidate = dict(type_id=identity, type_class=item.get('type_class'),
                instance_class=item['class'], params=deepcopy(item.get('type_params', {})),
                units=deepcopy(item.get('type_units', {})), origin='external_reference')
            if identity in result and candidate != result[identity]:
                raise ValueError(tr('types.error.conflicting_source', ident=identity))
            result[identity] = candidate
    return list(result.values())


def copy_type(source, name, params=None, units=None):
    result = deepcopy(source)
    result.update(type_id=str(uuid4()), name=name, source_type_id=source['type_id'],
                  revision=0, origin='power_machine_created')
    if params is not None:
        result['params'] = deepcopy(params)
    if units is not None:
        result['units'] = deepcopy(units)
    result['content_sha256'] = value_sha256({k: v for k, v in result.items() if k != 'content_sha256'})
    return result


def application_commands(model, private_type, object_ids):
    if private_type.get('origin') != 'power_machine_created':
        raise ValueError(tr('types.error.copy_first'))
    if private_type['content_sha256'] != value_sha256({k: v for k, v in private_type.items() if k != 'content_sha256'}):
        raise ValueError(tr('types.error.changed_content'))
    if set(private_type['params']) - set(private_type['units']):
        raise ValueError(tr('types.error.units_required'))
    index = index_model(model)
    result = []
    for identity in object_ids:
        group, element = index[identity]
        if element['class'] != private_type['instance_class']:
            raise ValueError(tr('types.error.incompatible_class', ident=identity))
        result.append(edit_command(model, 'update', group, identity, dict(type_id=private_type['type_id'],
            type_class=private_type['type_class'], type_params=deepcopy(private_type['params']),
            type_units=deepcopy(private_type['units']))))
    return result
