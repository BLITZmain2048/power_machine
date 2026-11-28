"""Presentación de cálculos, sin ecuaciones eléctricas adicionales."""
from power_machine.gui.i18n import tr
from pathlib import Path
import json
import math
import statistics
NAMES = {'contingency': tr('integration.n_1_contingencies'), 'voltage_stability': 'Curvas PV y QV', 'sensitivities': tr('integration.sensitivities'), 'sc_sweep': 'Cortocircuito por barra', 'dyn_metrics': tr('integration.dynamic_metrics'), 'cct': tr('integration.critical_clearing_time'), 'qss': tr('integration.quasi_dynamic_simulation'), 'qds_panel': tr('integration.quasi_dynamic_simulation'), 'rms': tr('integration.rms_simulation')}
STATES = {'completed': 'Resuelto', 'partial': tr('integration.partial'), 'failed': 'No convergente', 'not_converged': 'No convergente', 'islanded': 'Con islas', 'unsupported': tr('integration.unsupported')}

def write_figure(output, spec, index):
    """Usa siempre el tema claro del informe sin alterar la sesión interactiva."""
    import matplotlib
    from ..theme import Theme
    with matplotlib.rc_context(Theme('light').mpl_rc()):
        return _write_figure(output, spec, index)


def _write_figure(output, spec, index):
    """Exporta PDF vectorial y PNG con ejes y semántica conservados."""
    if not spec.get('series'):
        return None
    from matplotlib.figure import Figure
    panels = spec.get('panels', [spec])
    fig = Figure(figsize=spec.get('figsize', (7.0, 3.7 * len(panels))), layout='constrained')
    axes = fig.subplots(len(panels), 1, squeeze=False)
    for (ax, panel) in zip(axes[:, 0], panels):
        if not panel.get('xlabel') or not panel.get('ylabel'):
            raise ValueError('Cada figura requiere ejes con magnitud y unidad')
        for (n, line) in enumerate(panel['series']):
            (x, y) = (line['x'], line['y'])
            if panel.get('horizontal'):
                ax.barh(x, y, color='white', edgecolor='black', hatch='//' if n else None, label=line['label'])
            elif panel.get('bars'):
                positions = list(range(len(x)))
                ax.bar([v + (n - 1) * 0.25 for v in positions], y, width=0.25, color='white', edgecolor='black', hatch=['', '//', 'xx'][n % 3], label=line['label'])
                ax.set_xticks(positions, [format(v, '.4g') if isinstance(v, (int, float)) else v for v in x])
            else:
                yy = [v if isinstance(v, (int, float)) else float('nan') for v in y]
                for event in panel.get('events', []):
                    for k in range(1, len(x)):
                        if x[k - 1] <= event['time_s'] <= x[k] and x[k - 1] == x[k]:
                            yy[k] = float('nan')
                ax.plot(x, [v if isinstance(v, (int, float)) else float('nan') for v in yy], color=str(n * 0.22), linestyle=['-', '--', ':', '-.'][n % 4], marker=['o', 's', '^', 'd'][n % 4] if len(x) < 80 else None, markersize=3, linewidth=1, label=line['label'])
                if panel.get('last') and x:
                    ax.plot(x[-1], y[-1], 'kx', markersize=10, label='Último punto convergido' if n == 0 else None)
        for event in panel.get('events', []):
            ax.axvline(event['time_s'], color='black', linestyle='--', linewidth=0.8, label=event.get('label', tr('integration.event')))
        for (n, name) in enumerate(panel.get('unresolved', [])):
            ax.text(0.02, 1.13 - n * 0.035, 'No convergente: ' + name, transform=ax.transAxes, fontsize=7, va='top', bbox={'facecolor': 'white', 'edgecolor': 'none', 'alpha': 0.9})
        ax.ticklabel_format(axis='y', style='plain', useOffset=False) if not panel.get('horizontal') else None
        ax.set(xlabel=panel['xlabel'], ylabel=panel['ylabel'], title=panel.get('title', ''))
        ax.grid(True, axis='x' if panel.get('horizontal') else 'both', alpha=0.2)
        ax.legend(fontsize=7, loc='best')
        ax.tick_params(labelsize=7)
    folder = Path(output) / 'figuras'
    folder.mkdir(exist_ok=True)
    name = 'figura%03d' % index
    for ext in ('pdf', 'png'):
        fig.savefig(folder / (name + '.' + ext), dpi=150)
    (folder / (name + '.json')).write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding='utf-8')
    return 'figuras/' + name + '.pdf'

def render_calculation(item, meta, snapshot, model, index, output):
    from power_machine.gui.study_report import _envelope_payload, _table, latex_escape, _write_csv, _restriction_rows, _substation
    module = meta['module'].lower()
    p = _envelope_payload(item)
    buses = {b['id']: b for b in (model or {}).get('buses', [])}
    elements = {e['id']: e for k in ('branches', 'branches3', 'gens', 'loads') for e in (model or {}).get(k, [])}

    def identity(key):
        e = buses.get(key, elements.get(key, {}))
        ends = [buses.get(e.get(k), {}).get('name', '') for k in ('bus_from', 'bus_to')]
        name = e.get('name') or 'Rama entre ' + ' y '.join((v for v in ends if v))
        b = e if key in buses else buses.get(e.get('bus_from', e.get('bus')), {})
        return {'elemento': name, 'subestacion': _substation(e.get('path')), 'kv': b.get('un_kv')}
    legend = 'Corrida %d. Vigente. Sin validar contra la referencia externa.' % index
    heading = ('Curva QV' if p.get('curve_type') == 'QV' else 'Curva PV') if module == 'voltage_stability' else NAMES.get(module, tr('integration.load_flow_limits_and_controls_verification') if module == 'lf_controls' else tr('integration.calculation'))
    out = ['\\section{' + heading + '}']
    figs = []

    def paragraph(text):
        out.append(latex_escape(text))

    def table(rows, columns, caption):
        columns = [c for c in columns if any((r.get(c[0]) is not None and r.get(c[0]) != '' for r in rows))]
        if not columns or not rows:
            return
        columns = [(k, t, 0.94 / len(columns), 'left' if k in ('elemento', 'subestacion', 'estado', 'indicador', 'ventana', 'unidad') else 'right') for (k, t) in columns]
        for start in range(0, len(rows), 20):
            out.append(_table(columns, rows[start:start + 20], caption, 'tab:calculo%dparte%d' % (index, len(out)), legend))

    def figure(spec, caption):
        path = write_figure(output, spec, index * 10 + len(figs))
        if path:
            figs.append(path)
            out.extend(['\\begin{figure}[htbp]\\centering', '\\includegraphics[width=0.98\\linewidth,height=0.78\\textheight,keepaspectratio]{' + path + '}', '\\caption{' + latex_escape(caption) + '}\\end{figure}', '\\clearpage'])
    points = p.get('points', [])
    if module == 'contingency':
        cases = p.get('cases', [])
        failed = [c for c in cases if c.get('status') in ('failed', 'not_converged', 'error')]
        paragraph(tr('integration.each_of_d_selected_elements_was_removed') % (len(cases), sum((c.get('status') == 'islanded' for c in cases)), sum((c.get('metrics', {}).get('newly_deenergized_p_mw') or 0 for c in cases))))
        rows = []
        for c in cases:
            ids = [v.get('object_id') for v in c.get('outages', [])]
            r = identity(ids[0]) if ids else {}
            m = c.get('metrics', {})
            rows.append({**r, 'estado': STATES.get(c.get('status'), tr('integration.not_evaluable')), 'u': m.get('min_u_pu') if c not in failed else None, 'mw': m.get('newly_deenergized_p_mw')})
        table([r for r in rows if r['estado'] != 'Resuelto' or (r.get('mw') or 0) > 0], [('elemento', 'Contingencia'), ('subestacion', tr('integration.substation')), ('kv', 'kV'), ('estado', tr('integration.status')), ('u', tr('integration.minimum_u_p_u')), ('mw', 'Sin suministro MW')], 'Contingencias con incidencias')
        valid = sorted([r for r in rows if r.get('u') is not None and r['u'] > 0], key=lambda r: r['u'])
        figure({'series': [{}], 'figsize': (8, 10), 'horizontal': True, 'title': tr('integration.minimum_voltage_per_n_1_contingency'), 'xlabel': tr('integration.minimum_energized_voltage_p_u'), 'ylabel': tr('integration.outaged_line_name'), 'series': [{'label': 'Casos resueltos', 'x': [r['elemento'] for r in valid], 'y': [r['u'] for r in valid]}], 'unresolved': [r['elemento'] for r in rows if r['estado'] == 'No convergente']}, tr('integration.n_1_cases_ordered_by_minimum_voltage'))
        paragraph(tr('integration.contingency_classification_requires_checking_diagnostics_and_coverage'))
    elif module == 'voltage_stability':
        qv = p.get('curve_type') == 'QV'
        if qv:
            target = p.get('qv_bus_id', item.get('critical_bus_id'))
            ident = identity(target)
            paragraph('Barra estudiada: %s, %s, %s kV.' % (ident['elemento'], ident['subestacion'], ident['kv']))
            rows = [{'u': v.get('voltage_set_pu'), 'q': v.get('q_source_mvar', v.get('source_q_mvar'))} for v in points]
            paragraph(tr('integration.the_critical_busbar_was_regulated_by_a') % (len(rows), min((r['u'] for r in rows)), max((r['u'] for r in rows)), min((r['q'] for r in rows if r['q'] is not None)), max((r['q'] for r in rows if r['q'] is not None))))
            table(rows, [('u', 'Consigna U p.u.'), ('q', 'Q ficticia Mvar')], tr('integration.fictitious_source_at_the_critical_busbar'))
            figure({'title': tr('integration.critical_busbar_qv_curve'), 'xlabel': tr('integration.imposed_voltage_p_u'), 'ylabel': tr('integration.fictitious_reactive_injection_mvar'), 'series': [{'label': 'Fuente ficticia', 'x': [r['u'] for r in rows], 'y': [r['q'] for r in rows]}]}, tr('integration.qv_positive_q_injection_into_the_network'))
        else:
            last = points[-1] if points else {}
            paragraph(tr('integration.only_positive_internal_physical_demand_was_increased') % last.get('increase_mw', 0))
            rows = [{'p': v.get('load_breakdown', {}).get('selected_p_mw'), 'inc': v.get('increase_mw'), 'u': v.get('minimum_voltage', {}).get('u_pu', min([b['u_pu'] for b in v.get('monitored_buses', []) if b.get('u_pu') and b.get('status') == 'resuelto'], default=None))} for v in points]
            selected = [0, len(rows) // 2, len(rows) - 1] if len(rows) > 3 else range(len(rows))
            table([rows[i] for i in selected], [('p', 'Demanda interna MW'), ('inc', 'Incremento MW'), ('u', tr('integration.minimum_u_p_u'))], 'Puntos representativos PV')
            series = []
            for (n, b) in enumerate(last.get('monitored_buses', [])[:3]):
                series.append({'label': b.get('name', tr('integration.busbar')) + ' (%.3g kV)' % b.get('un_kv', 0), 'x': [v.get('load_breakdown', {}).get('selected_p_mw') for v in points], 'y': [next((r.get('u_pu') for r in v.get('monitored_buses', []) if r.get('bus_id') == b.get('bus_id')), None) for v in points]})
            if not series:
                series = [{'label': tr('integration.minimum_voltage'), 'x': [r.get('p') or r.get('inc') for r in rows], 'y': [r['u'] for r in rows]}]
            figure({'title': 'Curva PV de la red interna', 'xlabel': 'Demanda interna (MW)', 'ylabel': tr('integration.voltage_p_u'), 'last': True, 'series': series}, tr('integration.pv_three_internal_busbars_and_the_last'))
    elif module == 'sensitivities':
        allrows = p.get('buses', [])
        _write_csv(output / 'sensibilidades_barras_completas.csv', allrows)
        _write_csv(output / 'sensibilidades_ramas_completas.csv', p.get('branches', []))
        chosen = item.get('critical_bus_id')
        rows = []
        for r in allrows:
            if chosen and r.get('object_id', r.get('id')) != chosen:
                continue
            rows.append({**identity(r.get('object_id', r.get('id'))), 'elemento': r.get('name') or identity(r.get('object_id'))['elemento'], 'dp': r.get('du_dp_pu_per_mw'), 'dq': r.get('du_dq_pu_per_mvar')})
        paragraph(tr('integration.local_ac_derivatives_of_critical_voltage_are'))
        table(rows[:10], [('elemento', tr('integration.busbar')), ('subestacion', tr('integration.substation')), ('kv', 'kV'), ('dp', 'dU/dP p.u./MW'), ('dq', 'dU/dQ p.u./Mvar')], tr('integration.local_voltage_sensitivity'))
    elif module == 'sc_sweep':
        raw = p.get('rows', [])
        _write_csv(output / 'cortocircuito_barras_completo.csv', raw)
        levels = {}
        for r in raw:
            v = r.get('ik_initial_sym_ka')
            if isinstance(v, (int, float)) and v > 0:
                levels.setdefault(r.get('un_kv'), []).append(v)
        rows = [{'kv': k, 'n': len(v), 'min': min(v), 'med': statistics.median(v), 'max': max(v)} for (k, v) in sorted(levels.items())]
        paragraph(tr('integration.a_th_venin_boundary_uses_synthetic_sk') % sum((r['n'] for r in rows)))
        table(rows, [('kv', 'Nivel kV'), ('n', tr('integration.busbars_6ebb1c')), ('min', tr('integration.minimum_ik_ka')), ('med', "Ik'' mediana kA"), ('max', tr('integration.maximum_ik_ka'))], tr('integration.synthetic_sk_test_data_energized_busbars'))
        figure({'bars': True, 'title': tr('integration.ik_by_level_synthetic_sk_test_data'), 'xlabel': tr('integration.nominal_voltage_kv'), 'ylabel': tr('integration.initial_symmetrical_current_ik_ka'), 'series': [{'label': label, 'x': [r['kv'] for r in rows], 'y': [r[key] for r in rows]} for (key, label) in [('min', tr('integration.minimum')), ('med', 'Mediana'), ('max', tr('integration.maximum'))]]}, tr('integration.synthetic_sk_test_data_minimum_median_and'))
    elif module == 'lf_controls':
        paragraph(tr('integration.the_official_load_flow_was_checked_without'))
        rows = [{'indicador': 'Movimientos propuestos', 'valor': len(p.get('movements', []))}, {'indicador': 'Violaciones detectadas con datos disponibles', 'valor': len(p.get('violations', []))}, {'indicador': 'Verificaciones no evaluables', 'valor': len(p.get('not_evaluable', []))}, {'indicador': 'Islas energizadas resueltas', 'valor': p.get('energized_status', {}).get('n_solved')}, {'indicador': tr('integration.de_energized_islands'), 'valor': p.get('energized_status', {}).get('n_deenergized')}]
        table(rows, [('indicador', tr('integration.verification')), ('valor', tr('integration.count'))], tr('integration.verification_without_regulation_setpoints'))
        details = []
        for r in p.get('violations', []):
            details.append({**identity(r['object_id']), 'magnitud': r['quantity'], 'valor': r['value'], 'limite': r.get('limit')})
        table(details, [('elemento', tr('integration.element_49fa9f')), ('subestacion', tr('integration.substation')), ('kv', 'kV'), ('magnitud', tr('integration.quantity')), ('valor', tr('integration.value')), ('limite', tr('integration.limit'))], tr('integration.available_limits_exceeded_or_active'))
        counts = {}
        for row in p.get('not_evaluable', []):
            key = row.get('quantity', tr('integration.verification'))
            counts[key] = counts.get(key, 0) + 1
        table([{'indicador': k, 'valor': v} for (k, v) in counts.items()], [('indicador', 'Magnitud no evaluable'), ('valor', tr('integration.count'))], tr('integration.coverage_awaiting_limits'))
    elif module == 'cct':
        paragraph(tr('integration.no_evaluable_cct_interval_has_stable_and'))
    elif module == 'dyn_metrics':
        labels = {'nadir_hz': tr('integration.minimum_frequency'), 'zenith_hz': tr('integration.maximum_frequency'), 'power_machinef_abs_max_hz_per_s': tr('integration.maximum_absolute_frequency_derivative'), 'min_pu': tr('integration.minimum_voltage'), 'max_pu': tr('integration.maximum_voltage'), 'recovery_time_s': tr('integration.recovery_time')}
        rows = []
        for r in p.get('indicators', []):
            key = r.get('indicator_id', '').rsplit(':', 1)[-1]
            rows.append({'indicador': labels.get(key, 'Indicador no evaluable'), 'valor': r.get('value'), 'unidad': r.get('unit'), 'ventana': '0 a 2,2 s' if 'power_machinef' not in key else '100 ms desde 0,2 s', 'estado': r.get('state')})
        paragraph(tr('integration.metrics_are_computed_on_the_selected_rms'))
        table(rows, [('indicador', tr('integration.indicator')), ('valor', tr('integration.value')), ('unidad', tr('integration.unit')), ('ventana', tr('integration.window')), ('estado', tr('integration.status'))], tr('integration.interface_voltage_and_frequency_metrics'))
    elif module in ('rms', 'qss', 'qds_panel'):
        paragraph(item.get('engineering_text', tr('integration.engine_state_series')))
        if item.get('summary_rows'):
            table(item['summary_rows'], [('indicador', tr('integration.quantity')), ('valor', tr('integration.value')), ('unidad', tr('integration.unit'))], tr('integration.series_summary'))
        for spec in item.get('figure_specs', []):
            figure(spec, spec['caption'])
    else:
        paragraph(tr('integration.no_interpretable_results_for_this_calculation'))
    restrictions = _restriction_rows(item, snapshot, module)
    if restrictions:
        table(restrictions, [('restriccion', 'Restricciones vigentes')], tr('integration.scope_and_restrictions'))
    return (out, figs)
