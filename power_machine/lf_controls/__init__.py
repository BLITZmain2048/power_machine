"""Regulación discreta externa del LF. Sin validar contra la referencia externa.

Las consignas están en p.u., las posiciones son enteras. No escribe el modelo.
"""
from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path

from power_machine.loadflow import run_loadflow
from power_machine.lf_status import energized_status
from power_machine.loading import branch_loading
from power_machine.elements import gen_q_limits_mvar


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError('Se requiere un número finito explícito')
    return float(value)


def inventory(model):
    """Inventario por canal, con localizador y unidad de cada campo exportado.

    path del modelo es el relative_path REF. No se infiere una barra regulada
    desde el puerto conectado. El mínimo físico de ncapa es cero.
    """
    rows = []
    for group in ('branches', 'branches3', 'shunts'):
        for item in model.get(group, []):
            kind = item.get('class')
            if kind not in ('ElmTr2', 'ElmTr3', 'ElmShnt'):
                continue
            channels = ('', '2') if kind == 'ElmTr2' else ('h', 'm', 'l') if kind == 'ElmTr3' else ('',)
            for channel in channels:
                if kind == 'ElmShnt':
                    keys = dict(position='params.ncapa', maximum='params.ncapx', step='params.qcapn',
                                side='params.bus1', automatic='params.iswitch')
                    field = 'ncapa'
                elif kind == 'ElmTr3':
                    keys = dict(position='params.n3tap_' + channel, minimum='type_params.n3tmn_' + channel,
                                maximum='type_params.n3tmx_' + channel, step='type_params.du3tp_' + channel,
                                neutral='type_params.n3tp0_' + channel, phase='type_params.ph3tr_' + channel,
                                automatic='params.ntrcn')
                    field = 'n3tap_' + channel
                else:
                    keys = dict(position='params.nntap' + channel, minimum='type_params.ntpmn' + channel,
                                maximum='type_params.ntpmx' + channel, step='type_params.dutap' + channel,
                                side='type_params.tap_side' + channel, neutral='type_params.nntap0' + channel,
                                phase='type_params.phitr' + channel, automatic='params.ntrcn')
                    field = 'nntap' + channel
                keys.update(target_pu='params.usetp', controlled_bus='controlled_bus_id')
                fields = {}
                for name, locator in keys.items():
                    parts = locator.split('.')
                    source = item.get(parts[0], {}) if len(parts) == 2 else item
                    key = parts[-1]
                    units = item.get('type_units' if parts[0] == 'type_params' else 'units', {})
                    fields[name] = dict(value=source.get(key), locator=locator, unit=units.get(key),
                                        present=key in source)
                values = {k: v['value'] for k, v in fields.items()}
                if kind == 'ElmShnt':
                    values['minimum'] = 0
                    fields['minimum'] = dict(value=0, locator='contrato físico ncapa >= 0', unit='paso', present=True)
                missing = [k for k in ('position', 'minimum', 'maximum', 'step') if values.get(k) is None]
                reason = 'Faltan ' + ', '.join(missing) if missing else ''
                if not reason:
                    try:
                        lo, hi, pos, step = [_number(values[k]) for k in ('minimum', 'maximum', 'position', 'step')]
                        if not all(v.is_integer() for v in (lo, hi, pos)) or not lo <= pos <= hi:
                            reason = 'Posiciones o límites discretos inválidos'
                        elif lo == hi or step == 0:
                            reason = 'Sin recorrido de regulación'
                    except ValueError as exc:
                        reason = str(exc)
                if kind == 'ElmShnt' and (item.get('params', {}).get('shtype') != 2 or item.get('params', {}).get('qrean') != 0):
                    reason = 'Solo banco capacitivo sin reactor soportado por LF'
                if item.get('outserv'):
                    reason = 'Fuera de servicio'
                if kind == 'ElmTr2' and channel and not item.get('type_params', {}).get('itapch2'):
                    reason = 'Segundo cambiador deshabilitado'
                rows.append(dict(key=item['id'] + ':' + field, object_id=item['id'],
                                 relative_path=item.get('relative_path', item.get('path')), name=item.get('name'),
                                 class_name=kind, group=group, field=field, channel=channel,
                                 fields=fields, **values, available=not reason, reason=reason))
    return rows


def preflight(model, inputs, options):
    """Comprueba datos de controles y bandas sin ejecutar ecuaciones."""
    rows = {r['key']: r for r in inventory(model)}
    buses = {b['id'] for b in model.get('buses', []) if not b.get('outserv')}
    controls, seen = [], set()
    for supplied in inputs.get('controls', []):
        row = rows[supplied['key']]
        if row['key'] in seen:
            raise ValueError('Control duplicado')
        seen.add(row['key'])
        if not row['available']:
            raise ValueError(row['reason'])
        bus = supplied.get('bus_id', row.get('controlled_bus'))
        target = supplied.get('target_pu', row.get('target_pu'))
        if bus not in buses:
            raise ValueError('Falta barra controlada explícita en servicio')
        target = _number(target)
        deadband = _number(supplied.get('deadband_pu'))
        if target <= 0 or deadband < 0:
            raise ValueError('Consigna positiva y semibanda no negativa requeridas')
        controls.append(dict(row, bus_id=bus, target_pu=target, deadband_pu=deadband))
    count = options.get('max_iterations', 20)
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 1000:
        raise ValueError('Máximo de iteraciones entero entre 1 y 1000')
    for band in options.get('voltage_bands', []):
        if not 0 < _number(band['min_pu']) < _number(band['max_pu']) or _number(band['level_kv']) <= 0:
            raise ValueError('Banda de tensión inválida')
    levels = [b['level_kv'] for b in options.get('voltage_bands', [])]
    if len(set(levels)) != len(levels):
        raise ValueError('Nivel de tensión duplicado')
    if options.get('loading_limit_pct') is not None and _number(options['loading_limit_pct']) <= 0:
        raise ValueError('Límite de cargabilidad positivo requerido')
    return controls


def _voltage(result, bus_id):
    row = next((b for b in result.get('buses', []) if b['id'] == bus_id), {})
    if row.get('status') not in (None, 'resuelto', 'solved') or row.get('energized') is False:
        return None
    value = row.get('u_pu')
    return value if value is not None and math.isfinite(value) else None


def verify_limits(model, result, options):
    """Violaciones ordenadas por excedencia relativa del umbral configurado."""
    violations, unknown = [], []
    buses = {b['id']: b for b in model.get('buses', [])}
    def add(item, quantity, value, limit, severity):
        violations.append(dict(object_id=item['id'], relative_path=item.get('relative_path', item.get('path')),
                               name=item.get('name'), quantity=quantity, value=value, limit=limit, severity=severity))
    for b in result.get('buses', []):
        item = buses[b['id']]
        band = next((v for v in options.get('voltage_bands', []) if math.isclose(v['level_kv'], item['un_kv'], rel_tol=1e-6)), None)
        u = _voltage(result, b['id'])
        if band is None or u is None:
            unknown.append(dict(object_id=b['id'], quantity='U [p.u.]', reason='Sin banda configurada o sin solución'))
            continue
        if u < band['min_pu']:
            add(item, 'U [p.u.]', u, band['min_pu'], (band['min_pu'] - u) / band['min_pu'])
        if u > band['max_pu']:
            add(item, 'U [p.u.]', u, band['max_pu'], (u - band['max_pu']) / band['max_pu'])
    loading = branch_loading(model, result)
    items = {r['id']: r for g in ('branches', 'branches3', 'gens') for r in model.get(g, [])}
    limit = options.get('loading_limit_pct')
    for key, row in loading.items():
        value = row['loading_pct']
        if value is None or limit is None:
            unknown.append(dict(object_id=key, quantity='Carga [%]', reason=row.get('cause') or 'Sin umbral configurado'))
        elif value > limit:
            add(items[key], 'Carga [%]', value, limit, (value - limit) / limit)
    for gen in result.get('gens', []):
        if _voltage(result, items[gen['id']].get('bus')) is None or gen.get('q_mvar') is None:
            unknown.append(dict(object_id=gen['id'], quantity='Q [Mvar]', reason='Sin solución'))
        elif gen.get('q_limit'):
            add(items[gen['id']], 'Q [Mvar], límite activo LF', gen.get('q_mvar'), None, 0.0)
        else:
            try:
                low, high = gen_q_limits_mvar(items[gen['id']])
            except (ValueError, KeyError, TypeError) as exc:
                unknown.append(dict(object_id=gen['id'], quantity='Q [Mvar]', reason=str(exc)))
                continue
            q = gen['q_mvar']
            if math.isfinite(low) and q <= low + 1e-6:
                add(items[gen['id']], 'Q [Mvar]', q, low, max(0, low-q)/max(1, abs(low)))
            if math.isfinite(high) and q >= high - 1e-6:
                add(items[gen['id']], 'Q [Mvar]', q, high, max(0, q-high)/max(1, abs(high)))
    return sorted(violations, key=lambda v: v['severity'], reverse=True), unknown, loading


def execute_request(request, *, cancel=None, progress=None):
    """Lazo por coordenadas, prueba ambos vecinos mediante el LF público.

    Solo acepta movimientos que reducen el desvío local. El orden es el del
    usuario, no es OPF. Detecta retorno de la configuración global, incluido
    ciclo de dos posiciones. No extrapola el signo de respuesta de una barra.
    """
    output = dict(schema='power_machine-lf-controls-result-v1', module='lf_controls', status='invalid_input',
                  validation_label='sin validar contra la referencia externa', payload={}, diagnostics=[])
    try:
        request = deepcopy(request)
        json.dumps(request, allow_nan=False)
        if request.get('module') != 'lf_controls':
            raise ValueError('Módulo solicitado inválido')
        snapshot = request['snapshot']
        model = deepcopy(snapshot['model'])
        hashes = snapshot.get('fingerprints', {})
        if hashes.get('model_sha256') and hashes['model_sha256'] != fingerprint(model):
            raise ValueError('Huella del modelo incoherente')
        study = snapshot.get('study', {})
        if hashes.get('study_sha256') and hashes['study_sha256'] != fingerprint(study):
            raise ValueError('Huella del estudio incoherente')
        output.update(model_sha256=fingerprint(model), study_sha256=fingerprint(study),
                      variant_sha256=hashes.get('variant_sha256'))
        if study.get('scenario'):
            from power_machine.contingency.runner import materialize_scenario
            model = materialize_scenario(model, study['scenario'])
        options, inputs = request.get('options', {}), request.get('input', {})
        controls = preflight(model, inputs, options)
        output['provenance'] = dict(model_sha256=fingerprint(model), request_sha256=fingerprint(request))
        lookup = {r['id']: r for g in ('branches', 'branches3', 'shunts') for r in model.get(g, [])}
        def positions():
            return tuple(lookup[c['object_id']]['params'][c['field']] for c in controls)
        initial = positions()
        history, movements, blocked = [initial], [], []
        lf_options = {**study.get('ldf_options', {}), **options.get('lf_options', {})}
        if lf_options.get('overrides'):
            raise ValueError('Materialice los overrides antes de regular para conservar trazabilidad de posiciones')
        lf = run_loadflow(model, options=lf_options)
        reason, loop_ok, iterations = 'máximo de iteraciones', False, 0
        for iteration in range(options.get('max_iterations', 20)):
            iterations = iteration + 1
            if cancel is not None and (cancel.is_set() if hasattr(cancel, 'is_set') else cancel()):
                reason = 'cancelado'
                break
            if not energized_status(lf)['ok']:
                reason = 'load flow did not converge in energized islands'
                break
            moved, pending, blocked = False, False, []
            for ctl in controls:
                voltage = _voltage(lf, ctl['bus_id'])
                if voltage is None:
                    pending = True
                    blocked.append(dict(key=ctl['key'], reason='Barra sin solución'))
                    continue
                error = abs(voltage - ctl['target_pu'])
                if error <= ctl['deadband_pu']:
                    continue
                pending = True
                item = lookup[ctl['object_id']]
                current = item['params'][ctl['field']]
                best = None
                for new in (current - 1, current + 1):
                    if not ctl['minimum'] <= new <= ctl['maximum']:
                        continue
                    item['params'][ctl['field']] = new
                    try:
                        trial = run_loadflow(model, options=lf_options)
                    finally:
                        item['params'][ctl['field']] = current
                    u = _voltage(trial, ctl['bus_id'])
                    if energized_status(trial)['ok'] and u is not None:
                        candidate = abs(u - ctl['target_pu'])
                        if candidate < error - 1e-12 and (best is None or candidate < best[0]):
                            best = candidate, new, trial, u
                if best:
                    item['params'][ctl['field']] = best[1]
                    lf = best[2]
                    movements.append(dict(iteration=iterations, key=ctl['key'], object_id=ctl['object_id'],
                                          relative_path=ctl['relative_path'], name=ctl['name'],
                                          initial=current, final=best[1], u_before_pu=voltage, u_after_pu=best[3]))
                    moved = True
                else:
                    blocked.append(dict(key=ctl['key'], reason='En límite o sin vecino discreto que reduzca el desvío'))
            if progress:
                progress(dict(phase='lazo LF', completed=iterations, total=options.get('max_iterations', 20)))
            state = positions()
            if not moved:
                reason = ('bandas satisfechas' if controls else 'sin controles habilitados, solo verificación') if not pending else 'sin movimiento admisible'
                loop_ok = not pending
                break
            if state in history:
                reason = 'oscilación de dos posiciones' if len(history) >= 2 and state == history[-2] else 'ciclo de posiciones'
                break
            history.append(state)
        if energized_status(lf)['ok'] and controls and all(
                _voltage(lf, c['bus_id']) is not None and abs(_voltage(lf, c['bus_id']) - c['target_pu']) <= c['deadband_pu'] for c in controls):
            loop_ok, reason = True, 'bandas satisfechas'
        violations, unknown, loading = verify_limits(model, lf, options)
        edits = [dict(object_id=c['object_id'], relative_path=c['relative_path'], field='params.' + c['field'],
                      expected_value=a, value=b, unit='posición') for c, a, b in zip(controls, initial, positions()) if a != b]
        output['status'] = 'completed' if loop_ok and not unknown and not energized_status(lf)['n_deenergized'] else 'partial'
        output['payload'] = dict(inventory=inventory(snapshot['model']), controls=[dict(key=c['key'], initial=a, final=b,
                at_limit=b in (c['minimum'], c['maximum'])) for c, a, b in zip(controls, initial, positions())],
                movements=movements, blocked=blocked, loop_converged=loop_ok, stop_reason=reason,
                iterations=iterations, energized_status=energized_status(lf), lf_result=lf, violations=violations,
                not_evaluable=unknown, loading=loading, proposed_scenario=dict(edits=edits, base_model_sha256=fingerprint(snapshot['model'])))
        output['diagnostics'] = [dict(level='info', message=reason), dict(level='warning',
            message='Propuesta discreta sin validar contra la referencia externa. Bandas configuradas sin carácter normativo. Carga I/Inom o S/Sn sin factores térmicos.')]
    except (ValueError, KeyError, TypeError) as exc:
        output['diagnostics'] = [dict(level='error', message=str(exc))]
    except Exception as exc:
        output['status'] = 'error'
        output['diagnostics'] = [dict(level='error', message=str(exc))]
    return output


def save_proposal(result, path, root):
    """Guarda un resultado nuevo dentro del proyecto, nunca sobrescribe."""
    workspace = Path(__file__).resolve().parents[2]
    root = Path(root).resolve()
    target = (root / path).resolve()
    if not root.is_relative_to(workspace) or not target.is_relative_to(root):
        raise ValueError('La propuesta debe permanecer dentro de la carpeta de trabajo')
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
