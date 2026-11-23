"""Especificaciones JSON de presentación v1, independientes del estado eléctrico."""
from power_machine.gui.i18n import tr
from copy import deepcopy
import json
import math

SCHEMA = 'power_machine-presentation-v1'


def _require(ok, message):
    if not ok:
        raise ValueError(message)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value)


def _text(value):
    return isinstance(value, str) and bool(value)


def _json(value):
    if isinstance(value, dict):
        _require(all(isinstance(k, str) for k in value), tr('integration.json_keys_must_be_strings'))
        for v in value.values():
            _json(v)
    elif isinstance(value, list):
        for v in value:
            _json(v)
    else:
        _require(value is None or type(value) in (str, bool, int) or
                 type(value) is float and math.isfinite(value), tr('integration.value_cannot_be_serialized_to_json'))


def validate_template(template):
    """Valida sin eliminar campos de extensión ni ejecutar expresiones."""
    _json(template)
    _require(isinstance(template, dict), tr('integration.template_must_be_an_object'))
    _require(template.get('version') == 1 and type(template.get('version')) is int, tr('integration.unknown_template_version'))
    _require(_text(template.get('name')), tr('integration.template_name_required'))
    count = template.get('axes')
    _require(type(count) is int and 1 <= count <= 6, tr('integration.between_1_and_6_axes_are_required'))
    axes = template.get('axis_specs')
    _require(isinstance(axes, list) and len(axes) == count, tr('integration.axis_specifications_missing'))
    for axis in axes:
        _require(isinstance(axis, dict) and _text(axis.get('unit')), tr('integration.axis_unit_required'))
        _require(axis.get('reference', 'absolute') in ('absolute', 'deviation'), tr('integration.invalid_reference'))
        if axis.get('reference') == 'deviation':
            _require(_number(axis.get('base')), tr('integration.deviation_base_required_in_axis_units'))
        _require(axis.get('scale', 'linear') in ('linear', 'log'), tr('integration.invalid_scale'))
        for name in ('xlim_s', 'ylim'):
            if name in axis:
                pair = axis[name]
                _require(isinstance(pair, list) and len(pair) == 2 and all(_number(x) for x in pair)
                         and pair[0] < pair[1], tr('integration.invalid_limits'))
                if name == 'ylim' and axis.get('scale') == 'log':
                    _require(pair[0] > 0, tr('integration.logarithmic_scale_requires_positive_limits'))
    _require(isinstance(template.get('signals'), list), tr('integration.selectors_required'))
    for signal in template['signals']:
        _require(isinstance(signal, dict), tr('integration.invalid_selector'))
        axis = signal.get('axis', 0)
        _require(type(axis) is int and 0 <= axis < count, tr('integration.invalid_signal_axis'))
        _require(_text(signal.get('variable')), tr('integration.variable_required'))
        _require(signal.get('unit') == axes[axis]['unit'], tr('integration.unit_incompatible_with_axis'))
        selector = signal.get('objects', {})
        _require(isinstance(selector, dict), tr('integration.invalid_object_selector'))
        _require(set(selector) <= {'ids', 'class', 'name_contains', 'un_kv'}, tr('integration.unknown_object_filter'))
        if 'ids' in selector:
            _require(isinstance(selector['ids'], list) and all(_text(x) for x in selector['ids']), tr('integration.invalid_ids'))
        for key in ('class', 'name_contains'):
            if key in selector:
                _require(_text(selector[key]), tr('integration.invalid_text_filter'))
        if 'un_kv' in selector:
            _require(_number(selector['un_kv']) and selector['un_kv'] > 0, tr('integration.invalid_nominal_voltage'))
        _require(signal.get('line_style', '-') in ('-', '--', '-.', ':'), tr('integration.invalid_line_style'))
        _require(signal.get('marker', '') in ('', 'o', 's', '^', '.', 'x', '+'), tr('integration.invalid_marker'))
        _require(_number(signal.get('line_width', 1.15)) and signal.get('line_width', 1.15) > 0, tr('integration.invalid_width'))
    _require(isinstance(template.get('events', []), list), tr('integration.invalid_events'))
    for event in template.get('events', []):
        _require(isinstance(event, dict) and _number(event.get('time_s')) and
                 isinstance(event.get('label', ''), str), tr('integration.invalid_event'))
    return deepcopy(template)


def default_presentation():
    return dict(schema=SCHEMA, version=1, diagram=dict(color_mode='voltage', voltage_limits=[.95, 1.05],
                loading_bands=[80, 100], boxes={}, digits={}, show_results=True, historical=False),
                plot_templates=[], plot_pages=[], table_pages=[], extensions={})


def validate_presentation(data):
    """Rechaza campos conocidos inválidos y conserva extensiones JSON desconocidas."""
    _json(data)
    _require(isinstance(data, dict) and data.get('schema') == SCHEMA and
             type(data.get('version')) is int and data['version'] == 1, tr('integration.unknown_presentation_schema'))
    diagram = data.get('diagram')
    _require(isinstance(diagram, dict), tr('integration.diagram_required'))
    _require(diagram.get('color_mode') in ('voltage', 'voltage_band', 'loading', 'state'), tr('integration.invalid_colour_mode'))
    for name in ('voltage_limits', 'loading_bands'):
        pair = diagram.get(name)
        _require(isinstance(pair, list) and len(pair) == 2 and all(_number(x) for x in pair)
                 and 0 < pair[0] < pair[1], tr('integration.invalid_bands'))
    for name in ('show_results', 'historical'):
        _require(type(diagram.get(name)) is bool, tr('integration.invalid_flag'))
    if 'legacy_boxes' in diagram:
        _require(type(diagram['legacy_boxes']) is bool, tr('integration.invalid_legacy_format_flag'))
    digits = diagram.get('digits', {})
    _require(isinstance(digits, dict) and all(type(v) is int and 0 <= v <= 6 for v in digits.values()), tr('integration.invalid_precision'))
    boxes = diagram.get('boxes')
    if 'viewport' in diagram:
        view = diagram['viewport']
        _require(isinstance(view, dict) and _number(view.get('scale')) and view['scale'] > 0
                 and _number(view.get('x_factor')) and view['x_factor'] > 0, tr('integration.invalid_view_scale'))
        offset = view.get('offset')
        _require(isinstance(offset, list) and len(offset) == 2 and all(_number(x) for x in offset), tr('integration.invalid_offset'))
    positions = diagram.get('positions', {})
    _require(isinstance(positions, dict), tr('integration.invalid_positions'))
    for pair in positions.values():
        _require(isinstance(pair, list) and len(pair) == 2 and all(_number(x) for x in pair), tr('integration.invalid_position'))
    _require(isinstance(boxes, dict), tr('integration.formats_required_per_class'))
    for cls, fields in boxes.items():
        _require(_text(cls) and isinstance(fields, list), tr('integration.invalid_class_or_format'))
        for field in fields:
            _require(isinstance(field, dict) and _text(field.get('variable')) and _text(field.get('unit')), tr('integration.variable_and_unit_required'))
            _require(field.get('end') in ('none', 'local', 'from', 'to', 'hv', 'mv', 'lv'), tr('integration.invalid_terminal'))
            _require(type(field.get('precision')) is int and 0 <= field['precision'] <= 6, tr('integration.invalid_precision'))
            _require(isinstance(field.get('label', ''), str), tr('integration.invalid_label'))
            expected = {'u_kv': 'kV', 'u_pu': 'p.u.', 'angle_deg': '°', 'p': 'MW', 'q': 'Mvar', 'loading': '%', 'p_mw': 'MW', 'q_mvar': 'Mvar'}
            _require(field['variable'] in expected and field['unit'] == expected[field['variable']], tr('integration.incompatible_result_box_variable_or_unit'))
            _require(field['variable'] in ('p', 'q', 'loading') or field['end'] == 'none', tr('integration.variable_without_a_terminal'))
            _require(field['variable'] not in ('p', 'q') or field['end'] != 'none', tr('integration.branch_power_requires_a_terminal'))
    for name in ('plot_templates', 'table_pages'):
        _require(isinstance(data.get(name), list), tr('integration.pages_must_be_lists'))
    for template in data['plot_templates']:
        validate_template(template)
    pages = data.get('plot_pages', [])
    _require(isinstance(pages, list), tr('integration.invalid_plot_pages'))
    for page in pages:
        from .plots import validate_plot_specs
        _require(isinstance(page, dict) and isinstance(page.get('curves'), list), tr('integration.invalid_plot_page'))
        validate_plot_specs([page], {c.get('key') for c in page['curves'] if isinstance(c, dict)})
    for page in data['table_pages']:
        _require(isinstance(page, dict) and type(page.get('version')) is int and page['version'] == 1
                 and _text(page.get('name')) and _text(page.get('class')), tr('integration.invalid_table_page'))
        _require(isinstance(page.get('columns'), list) and all(_text(c) for c in page['columns']), tr('integration.invalid_columns'))
        filters = page.get('filters', [])
        _require(isinstance(filters, list), tr('integration.invalid_filters'))
        for f in filters:
            _require(isinstance(f, dict) and _text(f.get('field')) and f.get('op') in
                     ('eq', 'contains', 'lt', 'gt') and 'value' in f, tr('integration.invalid_table_filter'))
    _require(isinstance(data.get('extensions', {}), dict), tr('integration.invalid_extensions'))
    return deepcopy(data)


def dumps(data):
    return json.dumps(validate_presentation(data), ensure_ascii=False, allow_nan=False, sort_keys=True)


def loads(text):
    def pairs(items):
        result = {}
        for key, value in items:
            _require(key not in result, tr('integration.duplicate_json_key'))
            result[key] = value
        return result
    return validate_presentation(json.loads(text, object_pairs_hook=pairs))


def box_presets():
    def field(variable, unit, precision, end='none', label=''):
        return dict(variable=variable, end=end, unit=unit, precision=precision, label=label or variable)
    voltage = {'ElmTerm': [field('u_kv', 'kV', 2, label='U'), field('u_pu', 'p.u.', 4, label='u')]}
    flow = deepcopy(voltage)
    loading = {}
    for cls in ('ElmLne', 'ElmTr2', 'ElmTr3', 'ElmZpu', 'ElmCoup', 'StaSwitch'):
        flow[cls] = [field('p', 'MW', 1, 'local', 'P'), field('q', 'Mvar', 1, 'local', 'Q')]
        loading[cls] = [field('loading', '%', 1, 'local', tr('integration.load'))]
    for cls in ('ElmSym', 'ElmGenstat', 'ElmPvsys', 'ElmLod'):
        flow[cls] = [field('p_mw', 'MW', 1, label='P'), field('q_mvar', 'Mvar', 1, label='Q')]
    return {'flujo_potencia': flow, 'tensiones': voltage, 'cargabilidad': loading}


def plot_presets():
    def template(name, axes, signals):
        return dict(version=1, name=name, axes=len(axes), axis_specs=[dict(unit=u, scale='linear', reference='absolute') for u in axes], signals=signals, events=[])
    def signal(var, unit, objects, axis=0):
        return dict(variable=var, unit=unit, objects=objects, axis=axis, line_style='-')
    buses = signal('m:u', 'p.u.', {'class': 'ElmTerm', 'un_kv': 220})
    freq = signal('m:fehz', 'Hz', {})
    power = signal('m:P:bus1', 'MW', {'class': 'ElmSym'})
    return {'tensiones_220kv': template(tr('integration.220_kv_busbar_voltages'), ['p.u.'], [buses]),
            'frecuencia_interface': template(tr('integration.busbar_frequency'), ['Hz'], [freq]),
            'potencia_maquinas': template(tr('integration.machine_power'), ['MW'], [power]),
            'respuesta_falla': template(tr('integration.fault_response'), ['p.u.', 'Hz', 'MW'],
                [buses, dict(freq, axis=1), dict(power, axis=2)])}


def resolve_template(template, catalog, index=None):
    """Resuelve contra catálogo de la corrida, nunca agrega señales no adquiridas."""
    template = validate_template(template)
    index = index or {}
    curves, missing = [], []
    for selector in template['signals']:
        objects = selector.get('objects', {})
        found = []
        for row in catalog:
            item = index.get(row['element_id'], {})
            if row['variable'] != selector['variable'] or row['unit'] != selector['unit']:
                continue
            if 'ids' in objects and row['element_id'] not in objects['ids']:
                continue
            if 'class' in objects and row.get('element_class', item.get('class')) != objects['class']:
                continue
            if 'name_contains' in objects and objects['name_contains'].casefold() not in row.get('element_name', item.get('name', '')).casefold():
                continue
            if 'un_kv' in objects and item.get('un_kv', row.get('un_kv')) != objects['un_kv']:
                continue
            found.append(row)
        if not found:
            missing.append(dict(selector=deepcopy(selector), reason=tr('integration.no_compatible_acquired_signals')))
        if objects.get('ids'):
            for ident in objects['ids']:
                if not any(r['element_id'] == ident for r in found):
                    missing.append(dict(selector=deepcopy(selector), object_id=ident, reason=tr('integration.signal_missing_from_the_run')))
        for row in found:
            curves.append(dict(key=row['key'], axis=selector.get('axis', 0), label=row.get('element_name', row['element_id']),
                               line_style=selector.get('line_style', '-'), marker=selector.get('marker', ''),
                               line_width=selector.get('line_width', 1.15)))
    return dict(schema='power_machine-plot-page-v1', version=1, name=template['name'], axes=template['axes'], curves=curves, events=deepcopy(template.get('events', [])),
                template=template, resolved_signals=deepcopy(curves), missing_signals=missing,
                axis_specs=deepcopy(template['axis_specs']))
