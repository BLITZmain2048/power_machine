"""Cargabilidad de ramas a partir de resultados LF y ratings trazables del modelo.

Los valores se calculan para presentación y no alteran el resultado del motor LF.
No constituyen validación contra la referencia externa.
"""

from __future__ import annotations

import math
from typing import Any

from .lf_status import SOLVED, energized_status


SQRT3 = math.sqrt(3.0)


def _loading_metric(class_name: str | None) -> dict:
    """Declara la magnitud publicada, sin factores térmicos ni de sobrecarga."""
    common = dict(thermal_factors_included=False, overload_factors_included=False)
    if class_name == 'ElmLne':
        return dict(common, quantity='current_ratio', numerator='I extremo [kA]',
                    denominator='Inom × nlnum [kA]', per_winding=False)
    if class_name == 'ElmTr2':
        return dict(common, quantity='apparent_power_ratio', numerator='S extremo [MVA]',
                    denominator='strn × ntnum [MVA]', per_winding=False)
    if class_name == 'ElmTr3':
        return dict(common, quantity='apparent_power_ratio', numerator='S de devanado [MVA]',
                    denominator='strn3_h, strn3_mv o strn3_lv [MVA]', per_winding=True)
    return dict(common, quantity=None, numerator=None, denominator=None, per_winding=False)


def _island_membership(lf_result: dict) -> tuple[dict, dict, bool]:
    """Indexa los estados de islas por rama y barra sin alterar el resultado LF."""
    branch_statuses, bus_statuses = {}, {}
    islands = lf_result.get('islands') or []
    for island in islands:
        status = island.get('status')
        elements = island.get('elements') or {}
        for group in ('branches', 'branches3'):
            for element_id in elements.get(group) or []:
                branch_statuses[element_id] = status
        for bus_id in island.get('buses') or island.get('nodes') or []:
            bus_statuses[bus_id] = status
    return branch_statuses, bus_statuses, bool(islands)


def _state_cause(status: Any, subject: str) -> str | None:
    """Traduce solo estados resueltos a permiso de publicación."""
    if status in (None, SOLVED, 'solved'):
        return None
    normalized = str(status).strip().lower()
    if normalized in ('sin_referencia', 'desenergizada', 'desenergizado',
                      'sin_energia_pasiva', 'ambos_puertos_abiertos',
                      'extremo_abierto', 'fuera_de_servicio'):
        return f'{subject} está desenergizada ({status})'
    return f'{subject} pertenece a una isla no convergente ({status})'


def _solution_cause(row: dict, ends: tuple[str, ...], bus_results: dict,
                    branch_statuses: dict, bus_statuses: dict, islands_present: bool,
                    lf_result: dict, status_summary: dict) -> str | None:
    """Bloquea iterados de islas sin solución antes de calcular cargabilidad."""
    cause = _state_cause(row.get('status'), 'la rama')
    if cause:
        return cause
    cause = _state_cause(branch_statuses.get(row.get('id')), 'la rama')
    if cause:
        return cause
    if row.get('energized') is False:
        return 'la rama está desenergizada'
    for end in ends:
        bus_id = row.get('bus_' + end)
        bus = bus_results.get(bus_id, {})
        cause = _state_cause(bus.get('status'), f'la barra {bus_id}')
        if cause:
            return cause
        cause = _state_cause(bus_statuses.get(bus_id), f'la barra {bus_id}')
        if cause:
            return cause
        if bus.get('energized') is False:
            return f'la barra {bus_id} está desenergizada'
    if islands_present:
        missing_branch = row.get('id') not in branch_statuses
        missing_bus = any(row.get('bus_' + end) not in bus_statuses for end in ends)
        if missing_branch and missing_bus:
            return 'isla de la rama no identificada en el resultado LF'
    if not islands_present and lf_result.get('converged') is False and not status_summary['ok']:
        return 'load flow did not converge without island details'
    return None


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0.0 else None


def _current_ka(value: Any, unit: Any) -> float | None:
    """Convierte un rating declarado solamente desde A o kA hacia kA."""
    current = _finite_positive(value)
    if current is None:
        return None
    normalized = str(unit or '').strip().lower().replace(' ', '')
    if normalized == 'ka':
        return current
    if normalized == 'a':
        return current / 1000.0
    return None


def _rating(branch: dict) -> dict:
    """Devuelve el rating real aplicable, sin completar campos ausentes."""
    kind = branch.get('class')
    params = branch.get('params') or {}
    units = branch.get('units') or {}
    type_params = branch.get('type_params') or {}
    type_units = branch.get('type_units') or {}
    if kind == 'ElmLne':
        raw_current = params.get('Inom', type_params.get('Inom'))
        raw_unit = units.get('Inom', type_units.get('Inom'))
        current_ka = _current_ka(raw_current, raw_unit)
        circuits = _finite_positive(params.get('nlnum'))
        if current_ka is None:
            cause = 'corriente nominal Inom ausente o con unidad no soportada'
            return dict(kind='none', cause=cause)
        if circuits is None:
            return dict(kind='none', cause='número de circuitos nlnum ausente o inválido')
        return dict(kind='current', rating_ka=current_ka * circuits,
                    current_per_circuit_ka=current_ka, circuits=circuits,
                    source='params.Inom' if 'Inom' in params else 'type_params.Inom',
                    source_unit=raw_unit)
    if kind == 'ElmTr2':
        raw_power = params.get('strn', type_params.get('strn'))
        raw_unit = units.get('strn', type_units.get('strn'))
        power_mva = _finite_positive(raw_power)
        units_count = _finite_positive(params.get('ntnum', 1))
        if power_mva is None or str(raw_unit or '').strip().lower() != 'mva':
            return dict(kind='none', cause='potencia nominal strn ausente o con unidad no MVA')
        if units_count is None:
            return dict(kind='none', cause='número de unidades ntnum ausente o inválido')
        return dict(kind='power', rating_mva=power_mva * units_count,
                    power_per_unit_mva=power_mva, units_count=units_count,
                    source='params.strn' if 'strn' in params else 'type_params.strn',
                    source_unit=raw_unit)
    if kind == 'ElmTr3':
        ratings = {}
        rating_keys = {'hv': 'strn3_h', 'mv': 'strn3_mv', 'lv': 'strn3_lv'}
        for end, key in rating_keys.items():
            power_mva = _finite_positive(type_params.get(key))
            unit = type_units.get(key)
            if power_mva is None or str(unit or '').strip().lower() != 'mva':
                ratings[end] = dict(kind='none', cause=f'potencia nominal {key} ausente o con unidad no MVA')
            else:
                ratings[end] = dict(kind='power', rating_mva=power_mva,
                                    source='type_params.' + key, source_unit=unit)
        return dict(kind='three_winding', ends=ratings)
    if kind == 'ElmZpu':
        return dict(kind='none', cause='ElmZpu del equivalente boundary equivalent no tiene rating nominal')
    return dict(kind='none', cause=f'{kind or "clase desconocida"} no tiene rating aplicable')


def _end_values(row: dict, end: str, bus_results: dict) -> dict:
    """Obtiene S e I desde P, Q y tensión de barra, con unidades explícitas."""
    p_mw = row.get('p_' + end + '_mw')
    q_mvar = row.get('q_' + end + '_mvar')
    try:
        p_value, q_value = float(p_mw), float(q_mvar)
    except (TypeError, ValueError):
        return dict(s_mva=None, i_ka=None, cause='P o Q del extremo ausente')
    if not math.isfinite(p_value) or not math.isfinite(q_value):
        return dict(s_mva=None, i_ka=None, cause='P o Q del extremo no finito')
    s_mva = math.hypot(p_value, q_value)
    bus = bus_results.get(row.get('bus_' + end), {})
    voltage_kv = _finite_positive(bus.get('u_kv'))
    if voltage_kv is None:
        return dict(s_mva=s_mva, i_ka=None, cause='tensión de barra ausente o inválida')
    return dict(s_mva=s_mva, i_ka=s_mva / (SQRT3 * voltage_kv),
                voltage_kv=voltage_kv)


def _apply_rating(values: dict, rating: dict) -> dict:
    """Añade porcentaje respecto de un rating de corriente o potencia."""
    result = dict(values)
    if values.get('s_mva') is None or values.get('i_ka') is None:
        result['loading_pct'] = None
        return result
    if rating.get('kind') == 'current':
        result['rating_ka'] = rating['rating_ka']
        result['loading_pct'] = 100.0 * values['i_ka'] / rating['rating_ka']
        return result
    if rating.get('kind') == 'power':
        result['rating_mva'] = rating['rating_mva']
        result['loading_pct'] = 100.0 * values['s_mva'] / rating['rating_mva']
        return result
    result['loading_pct'] = None
    result['cause'] = rating.get('cause', 'rating no disponible')
    return result


def branch_loading(model: dict, lf_result: dict) -> dict[str, dict]:
    """Calcula I [kA], S [MVA] y carga [%] por rama y extremo.

    El diccionario se indexa por `object_id` de rama. Para líneas, el porcentaje
    es corriente de extremo respecto de `Inom` multiplicado por `nlnum`. Para
    transformadores 2W es potencia aparente respecto de `strn` por `ntnum`, y
    para 3W es potencia aparente por devanado respecto de `strn3_h`, `strn3_mv`
    o `strn3_lv`. No incluye factores térmicos ni de sobrecarga. Una rama de
    isla no convergente o desenergizada conserva `loading_pct=None` antes de
    calcular I o S y expone una causa explícita.
    """
    branches = {row.get('id'): row for row in (model.get('branches') or [])}
    branches.update({row.get('id'): row for row in (model.get('branches3') or [])})
    bus_results = {row.get('id'): row for row in (lf_result.get('buses') or [])}
    branch_statuses, bus_statuses, islands_present = _island_membership(lf_result)
    status_summary = energized_status(lf_result)
    result = {}
    for row in (lf_result.get('branches') or []):
        branch = branches.get(row.get('id'))
        if branch is None:
            continue
        rating = _rating(branch)
        end_names = ('hv', 'mv', 'lv') if branch.get('class') == 'ElmTr3' else ('from', 'to')
        solution_cause = _solution_cause(
            row, end_names, bus_results, branch_statuses, bus_statuses,
            islands_present, lf_result, status_summary,
        )
        ends = {}
        if solution_cause:
            for end in end_names:
                ends[end] = dict(s_mva=None, i_ka=None, loading_pct=None,
                                 cause=solution_cause)
        elif branch.get('class') == 'ElmTr3':
            for end in end_names:
                ends[end] = _apply_rating(_end_values(row, end, bus_results), rating['ends'][end])
        else:
            for end in end_names:
                ends[end] = _apply_rating(_end_values(row, end, bus_results), rating)
        loading_values = [values['loading_pct'] for values in ends.values()
                          if values.get('loading_pct') is not None]
        result[row['id']] = dict(id=row['id'], class_name=branch.get('class'),
                                 name=branch.get('name'), rating=rating, ends=ends,
                                 loading_pct=max(loading_values) if loading_values else None,
                                 cause=solution_cause or rating.get('cause'),
                                 loading_metric=_loading_metric(branch.get('class')))
    return result
