"""Preflight conservador capabilities, sin cálculo numérico, E/S ni validación PF.

capability_inputs es un diccionario opcional del estudio con una entrada por
motor: input, options y evidencias consumibles. No modifica StudyCase v1.
Una capacidad condicional no acredita convergencia ni presencia de datos
externos que el llamador no haya entregado en memoria.
"""
from copy import deepcopy
import hashlib
import json


MODULES = ('voltage_stability', 'sensitivities', 'sc_sweep', 'cct', 'dyn_metrics', 'qds_panel', 'lf_controls')


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def capabilities(model, study):
    """Devuelve cobertura por cálculo para el caso, nunca ejecuta LF/RMS.

Los diagnósticos incluyen object_id y relative_path cuando hay elemento.
evaluable=True solo habilita el preflight numérico del alcance declarado.
Un LF calculado por el consumidor debe pasar energized_status, no converged.
"""
    if hasattr(study, 'to_json'):
        study = study.to_json()
    if not isinstance(model, dict) or not isinstance(study, dict):
        raise TypeError('model y study deben ser diccionarios o StudyCase serializable')
    model, study = deepcopy(model), deepcopy(study)
    supplies = study.get('capability_inputs', {})
    if not isinstance(supplies, dict):
        raise ValueError('capability_inputs debe ser un diccionario')
    rows = {}
    for module in MODULES:
        rows[module] = dict(evaluable=False, status='conditional', coverage=[],
                            missing_data=[], blockers=[], restrictions=[],
                            validation='sin validar contra PF')
    def block(module, code, message, item=None):
        item = item or {}
        rows[module]['blockers'].append(dict(code=code, message=message,
            object_id=item.get('object_id', item.get('id')),
            relative_path=item.get('relative_path', item.get('path'))))
    def missing(module, text):
        rows[module]['missing_data'].append(text)
    groups = ('buses', 'branches', 'branches3', 'switches', 'loads', 'gens', 'shunts')
    index = {v['id']: v for g in groups for v in model.get(g, []) if 'id' in v}
    scenario = study.get('scenario') or {}
    effective = deepcopy(model)
    try:
        from power_machine.loadflow import _scenario
        for field, changes in scenario.items():
            if not isinstance(changes, dict):
                raise ValueError('Cada operación debe mapear identidad a valor')
            if any(key not in index for key in changes):
                raise ValueError('Escenario refiere un objeto inexistente')
        effective = _scenario(effective, scenario)
    except (ValueError, TypeError, KeyError) as exc:
        for module in MODULES:
            block(module, 'invalid_scenario', str(exc))
    from power_machine.topology import build_topology
    try:
        topology = build_topology(effective)
        active_gens = [g for g in effective.get('gens', []) if g['id'] in topology['active']['gens']]
        refs = [g for g in active_gens if g.get('ref')]
    except (ValueError, KeyError, TypeError) as exc:
        topology, refs = None, []
        for module in MODULES:
            block(module, 'invalid_topology', str(exc))
    for module in ('voltage_stability', 'sensitivities', 'qds_panel'):
        if not refs:
            block(module, 'no_reference', 'No hay referencia LF activa conectada')
        rows[module]['restrictions'].append('LF previo por energized_status, sin prometer convergencia')
    pv = rows['voltage_stability']
    pv['coverage'] = ['PV de cargas en islas resueltas', 'QV de fuente ficticia en barra no referencia']
    pv['restrictions'] += ['No es continuación ni colapso físico confirmado',
                           'QV resuelve sin límites Q de generadores',
                           'No acreditar cambio de factor de potencia sin revisar el punto base']
    if not any(not x.get('outserv') for x in effective.get('loads', [])):
        missing('voltage_stability', 'Carga activa para PV')
    pv_input = supplies.get('voltage_stability', {}).get('input', {})
    pv['restrictions'] = ['PV es exploración numérica, no margen físico',
        'QV idealizada o con límites reales según reactive_limit_mode',
        'No sustituye límites térmicos, de importación ni capacidad de generación']
    if effective.get('interface') and pv_input.get('curve_type', 'PV') == 'PV':
        from power_machine.voltage_stability.runner import _select_loads, _dispatch_participants
        try:
            _select_loads(effective, pv_input)
        except (ValueError, TypeError, KeyError) as exc:
            block('voltage_stability', 'physical_load_selection', str(exc))
        dispatch = supplies.get('voltage_stability', {}).get('options', {}).get('generator_dispatch', 'reference')
        if dispatch == 'proportional':
            try:
                _dispatch_participants(effective, pv_input)
            except (ValueError, TypeError, KeyError) as exc:
                block('voltage_stability', 'dispatch_data', str(exc))
        pv['restrictions'].append('Referencia externa solo como importación idealizada, no external grid capacity')
    if pv_input.get('curve_type') == 'QV':
        target = pv_input.get('qv_bus_id')
        if target not in {b['id'] for b in effective.get('buses', []) if not b.get('outserv')}:
            missing('voltage_stability', 'Barra QV activa')
        if any(g.get('bus') == target for g in refs):
            block('voltage_stability', 'qv_reference', 'La barra QV no puede ser referencia angular')
        if topology is not None and target in topology['bus_to_node']:
            target_island = topology['node_to_island'][topology['bus_to_node'][target]]
            ref_islands = {topology['node_to_island'][topology['bus_to_node'][g['bus']]] for g in refs}
            if target_island not in ref_islands:
                block('voltage_stability', 'qv_deenergized', 'Barra QV sin referencia en su isla')
    rows['sensitivities']['coverage'] = ['Derivadas locales AC con balance en referencia']
    rows['sensitivities']['restrictions'] = ['Sujeto al dictamen sensitivities y a controles activos constantes']
    sensitivity_options = supplies.get('sensitivities', {}).get('options', {})
    if not sensitivity_options.get('injection_bus_id'):
        missing('sensitivities', 'Barra de inyección y rama de contingencia explícitas')
    target = sensitivity_options.get('injection_bus_id')
    if topology is not None and target:
        ref_islands = {topology['node_to_island'][topology['bus_to_node'][g['bus']]] for g in refs}
        if target not in topology['bus_to_node'] or topology['node_to_island'][topology['bus_to_node'][target]] not in ref_islands:
            block('sensitivities', 'injection_without_reference', 'Inyección fuera de una isla con referencia')
    rows['sensitivities']['restrictions'].append('Bloquear conclusiones ante cambio de régimen, LODF local sujeto a verificación N-1')
    rows['dyn_metrics']['coverage'] = ['Posproceso de señales RMS identificadas por unidad y variable']
    if not supplies.get('dyn_metrics', {}).get('signals_verified'):
        missing('dyn_metrics', 'CSV, unidades, eventos, huecos y hashes de la corrida verificados')
    rows['dyn_metrics']['restrictions'] = ['Ventanas completas, huecos y eventos limitan cada indicador',
        'Sin evento o cobertura suficiente, conservar no evaluable', 'No usar conclusiones normativas del perfil referencial']
    rows['cct']['coverage'] = ['IEEE 39 clásico B16 a 1 s, despeje simple o L16-17', 'power_machine condicionado a señales RMS']
    if not supplies.get('cct', {}).get('input', {}).get('fault_bus_id'):
        missing('cct', 'Falla, despeje, horizonte, umbrales y paso del estudio')
    cct_input = supplies.get('cct', {}).get('input', {})
    if cct_input.get('engine') == 'power_machine_rms':
        if cct_input.get('fault_bus_id') not in index or any(cct_input.get(k) is None for k in ('fault_r_ohm', 'fault_x_ohm')):
            block('cct', 'power_machine_fault_data', 'Barra de falla y Rf/Xf explícitos requeridos')
    elif cct_input.get('engine') != 'ieee39_classical':
        missing('cct', 'Motor CCT explícito')
    elif 'B16' not in index or cct_input.get('fault_bus_id') != 'B16' or cct_input.get('fault_inception_s', 1.) != 1. or any(cct_input.get(k) not in (None, 0.) for k in ('fault_r_ohm', 'fault_x_ohm')):
        block('cct', 'classical_domain', 'Dominio clásico acreditado: B16 a 1 s, falla sólida')
    rows['cct']['restrictions'] = ['Requiere fronteras opuestas y consistencia h/2',
        'Horizonte y umbrales explícitos, sin validación PF',
        'power_machine exploratorio: campañas moderadas estables no acreditan CCT ni duración segura de protección',
        'Fallo numérico es no evaluable, nunca inestable']
    rows['qds_panel']['coverage'] = ['Serie de LF, sin dinámica entre instantes']
    rows['qds_panel']['restrictions'] += ['q_limits=False impuesto por el adaptador',
        'Despacho heurístico sin límites completos', 'Ratings por unidad y circuitos pendientes de corrección']
    if scenario:
        block('qds_panel', 'scenario_unsupported', 'QDS no acepta escenario previo')
    if len(refs) != 1:
        block('qds_panel', 'reference_count', 'QDS requiere exactamente una referencia activa')
    if not supplies.get('qds_panel', {}).get('profile_verified'):
        missing('qds_panel', 'Perfil, manifiesto y hash compatibles verificados por el lector QSS')
    rows['qds_panel']['restrictions'] = ['Serie de LF con q_limits=False',
        'Despacho heurístico sin límites completos', 'Cargabilidad indicativa del adaptador QDS, no de loading.py',
        'Demanda sin suministro separada y duración desconocida conservada',
        'Perfil y modelo serializado deben corresponder al caso oficial con overrides, el crudo es solo comparación']
    sc = rows['sc_sweep']
    sc['coverage'] = ['Perfil SC por barra, fuente, tipo de falla y magnitud']
    sc['restrictions'] = ['No deducir boundary equivalent desde potencias LF', 'No usar barras sin fuente como completed',
                          'No hay aporte implementado de current_limited', 'Sin conformidad IEC acreditada']
    supplied_sc = supplies.get('sc_sweep', {})
    data = supplied_sc.get('supplemental')
    sc['by_bus'] = []
    if data is None:
        missing('sc_sweep', 'Suplemento SC explícito en capability_inputs.sc_sweep.supplemental')
    elif topology is not None:
        try:
            from power_machine.shortcircuit.data import from_dict
            from power_machine.shortcircuit.schema import ShortCircuitOptions
            from power_machine.shortcircuit.sequences import prepare_network
            from power_machine.shortcircuit import _apply_scenario
            opts = deepcopy(supplied_sc.get('options', {}))
            kind = opts.pop('fault_kind', '3ph')
            opts.pop('r_fault_ohm', None)
            opts.pop('x_fault_ohm', None)
            options = ShortCircuitOptions(**opts)
            sc_model = _apply_scenario(model, scenario)
            parsed = from_dict(data)
            selected = supplied_sc.get('selected_bus_ids')
            selected = selected if selected is not None else [b['id'] for b in effective.get('buses', []) if not b.get('outserv')]
            for ident in selected:
                if ident not in topology['bus_to_node']:
                    sc['by_bus'].append({'object_id': ident, 'evaluable': False, 'reason': 'Barra ausente o inactiva'})
                    continue
                prepared = prepare_network(sc_model, parsed, options, kind, ident)
                # La estructura del motor se consume explícitamente, sin resolver Y.
                diagnostics = prepared.get('diagnostics', [])
                node = topology['bus_to_node'][ident]
                island = topology['node_to_island'][node]
                sources = [s for s in prepared.get('sources', []) if not s.get('excluded') and
                           topology['node_to_island'][topology['bus_to_node'][s['bus_id']]] == island]
                ok = bool(sources) and not any(d.get('level') == 'error' for d in diagnostics)
                sc['by_bus'].append({'object_id': ident, 'relative_path': index[ident].get('path'),
                                     'evaluable': ok, 'diagnostics': diagnostics})
            if not sc['by_bus'] or not all(b['evaluable'] for b in sc['by_bus']):
                block('sc_sweep', 'incomplete_sc_coverage', 'La selección contiene barras sin cobertura SC')
        except Exception as exc:
            block('sc_sweep', 'sc_preflight', f'{type(exc).__name__}: {exc}')
    # La GUI puede entregar el modelo ya contraído. Detectar también sus ramas derivadas.
    bus_kv = {b['id']: b.get('un_kv') for b in effective.get('buses', [])}
    # SC-C01 y SC-E01 cerrados. La cobertura permanece condicionada por barra.
    source = next((s for s in (data or {}).get('sources', {}).values() if s.get('kind') == 'external'), None)
    if source:
        sc['coverage'] = ['Barrido power_machine trifásico de secuencia positiva con fuente Thévenin suministrada',
                          'ElmTr2 con relación nominal y grupo, estrella ElmTr3 en el dominio ensayado']
        sc['restrictions'] += ['ElmTr2 en posición principal SC, toma operativa LF solo trazada',
            'Cierre de lazos e invariancia de fase acreditados en el caso oficial, revisar variantes',
            'Sk, X/R y c deben tener procedencia para interpretar niveles reales',
            'Sin validación PF del barrido power_machine ni acreditación para seleccionar protecciones']
    for branch in effective.get('branches', []):
        if branch.get('class') == 'ElmZpu' and branch.get('derived_from') and bus_kv.get(branch.get('bus_from')) != bus_kv.get(branch.get('bus_to')):
            block('sc_sweep', 'sc_derived_voltage_bases', 'Estrella SC con bases distintas no admitida por el solver', branch)
    sc['restrictions'] += ['power_machine: solo secuencia positiva y fuente de frontera suministrada',
        'Contracción ideal no acredita corrientes individuales de acopladores',
        'No usar Sk sintético como nivel real de boundary', 'Tap operativo neutro no autoriza omitir relación nominal ni desfase de grupo', 'Aportes de convertidores y correcciones térmicas requieren cobertura propia',
        'Ceros en islas sin fuente no son barras con nivel SC evaluable',
        'Overrides ajustados al flujo base no acreditan impedancias físicas para SC ni contingencias']
    control = rows['lf_controls']
    control['coverage'] = ['Verificación de límites explícitos', 'Propuesta de tomas y bancos capacitivos con consignas explícitas']
    control['restrictions'] = ['Heurística secuencial local, sin garantía de óptimo ni factibilidad global',
        'Sin controles solicitados solo verifica, nunca infiere regulación desde usetp',
        'Bandas y umbrales son entradas del estudio, no criterios normativos',
        'Límites o ratings ausentes permanecen no evaluables',
        'La propuesta no modifica el modelo base ni aplica maniobras',
        'Ciclo, falta de mejora o límite alcanzado no acreditan bandas satisfechas',
        'Cargabilidad indicativa, sin factores térmicos operativos acreditados']
    from power_machine.lf_controls import preflight
    supplied_control = supplies.get('lf_controls', {})
    try:
        preflight(effective, supplied_control.get('input', {}), supplied_control.get('options', {}))
    except (ValueError, KeyError, TypeError) as exc:
        block('lf_controls', 'control_data', str(exc))
    for row in rows.values():
        row['evaluable'] = not row['blockers'] and not row['missing_data']
        row['status'] = 'conditional' if row['evaluable'] else 'blocked'
    return dict(schema='power_machine-study-capabilities-v1', qualification_revision='capabilities-v1', case_id=study.get('case_id'),
                model_sha256=_hash(model), study_sha256=_hash(study), calculations=rows,
                note='Preflight estático, no acredita convergencia, validación PF ni cumplimiento normativo')
