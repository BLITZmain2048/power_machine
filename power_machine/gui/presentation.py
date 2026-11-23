"""Identidad visible de elementos y selección inicial de señales del motor."""
import re
from .i18n import tr


def station(item):
    """Return an explicit substation name, or an empty string when absent."""
    explicit = item.get('substation') or item.get('substation_name')
    if explicit and str(explicit).strip().casefold() not in ('sin subestación','sin subestacion','no substation'):
        return str(explicit)
    parts = str(item.get('path', item.get('relative_path',''))).replace('\\','/').split('/')
    return next((v[:-len('.ElmSubstat')] for v in reversed(parts) if v.endswith('.ElmSubstat')), '')


def _label_index(network):
    if isinstance(network.get('buses'), list):
        return {row.get('id',row.get('object_id')):row for rows in network.values() if isinstance(rows,list)
                for row in rows if isinstance(row,dict)}
    return network


def _label_bus(item, index):
    ident = next((item[k] for k in ('bus','bus_from','bus_hv','bus1') if item.get(k)), None)
    return index.get(ident,item)


def voltage(item, index):
    index = _label_index(index)
    value = item.get('un_kv', _label_bus(item,index).get('un_kv'))
    return f'{value:g} kV' if isinstance(value,(int,float)) else ''


def element_label(item, index):
    """One display label shared by panels, event targets and data views."""
    index = _label_index(index)
    ident = str(item.get('id',item.get('object_id','')))
    name = item.get('name') if ident.startswith('pf_') else ident
    name = name or item.get('name') or tr('integration.unnamed_element')
    substation = station(item) or station(_label_bus(item,index))
    return ' · '.join(str(v) for v in (name,substation,voltage(item,index)) if v)


def target_labels(index, classes):
    labels, seen = {}, {}
    for ident, item in index.items():
        if item.get('class', 'ElmTerm' if item.get('group') == 'buses' else '') not in classes:
            continue
        label = element_label(item, index)
        seen[label] = seen.get(label, 0) + 1
        labels[ident] = label if seen[label] == 1 else tr('integration.element', value0=label, value1=seen[label])
    return labels


def human_message(text, index):
    def replace(match):
        item = index.get(match[0])
        return f"{item.get('name', tr('integration.element_49fa9f'))} ({station(item)})" if item else tr('integration.unlisted_element')
    return re.sub(r'pf_[a-zA-Z0-9]+', replace, str(text)).replace(';', ',')


def plot_events(events):
    """Convierte eventos del estudio a marcadores seguros para una página RMS."""
    markers = []
    for event in events or []:
        if isinstance(event, dict):
            time_s = event.get('time_s')
            kind = event.get('kind', tr('integration.event'))
            target = event.get('target_id', '')
        else:
            time_s = getattr(event, 'time_s', None)
            kind = getattr(event, 'kind', 'Evento')
            target = getattr(event, 'target_id', '')
        if isinstance(time_s, (int, float)):
            label = str(kind or tr('integration.event'))
            if target:
                label += ' · ' + str(target)
            markers.append({'time_s': float(time_s), 'label': label})
    return markers


def default_plot(study, catalog, index):
    """Una página con U, frecuencia, P y Q sin inventar señales de referencia."""
    available = set(study.monitored) if study.reference_case else {r['key'] for r in catalog}
    rows = [r for r in catalog if r['key'] in available and not index.get(r['element_id'], {}).get('outserv')]
    selected = []
    for variable, axis in [('m:u', 0), ('m:fehz', 1), ('m:P:bus1', 2)]:
        candidates = [r for r in rows if r['variable'] == variable]
        selected.extend((r, axis) for r in candidates[:2])
    curves = []
    for row, axis in selected:
        curves.append(dict(key=row['key'], axis=axis, label=element_label(index[row['element_id']], index)))
        if row['key'] not in study.monitored:
            study.monitored.append(row['key'])
    return dict(name=tr('integration.main_signals'), axes=4 if study.reference_case else 3, curves=curves,
                show_pf=True, frequency_mode='absolute', frequency_base_hz=50.0,
                events=plot_events(getattr(study, 'events', [])))
