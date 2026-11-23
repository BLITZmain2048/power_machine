"""Explicit island-reference selection and disconnected-generation accounting."""
from __future__ import annotations
import math
from ..elements import gen_pq_mw, load_pq_mw, MissingParameter
from .results import diagnostic


def has_demand(island, index):
    """Keep nonzero or unknown P/Q demand in the operating-island calculation."""
    for ident in island['elements'].get('loads', ()):
        try:
            p, q = load_pq_mw(index[ident])
            if not math.isfinite(p) or not math.isfinite(q) or p != 0 or q != 0:
                return True
        except (MissingParameter, ValueError, TypeError):
            return True
    return False


def generation_only(island, index):
    """Identify generator islands without demand, including zero-valued load records."""
    return bool(island['elements'].get('gens')) and not has_demand(island, index)


def capacity_mva(generator):
    """Return declared total MVA and its source. Never infer a missing rating from MW."""
    value = generator.get('s_rated_mva')
    if value is not None:
        try:value = float(value)
        except (ValueError,TypeError):value = float('nan')
        if math.isfinite(value) and value > 0:
            return value, 's_rated_mva'
    for field, units in (('params','units'), ('type_params','type_units')):
        value = generator.get(field, {}).get('sgn')
        unit = generator.get(units, {}).get('sgn')
        count = generator.get('params', {}).get('ngnum', generator.get('n_units'))
        if value is not None and count is not None and unit in ('MVA', 'kVA'):
            try:total = float(value) * float(count) / (1000 if unit == 'kVA' else 1)
            except (ValueError,TypeError):continue
            if math.isfinite(total) and total > 0:
                return total, field + '.sgn*ngnum[' + unit + ']'
    return None, None


def choose_references(topology, index):
    """Choose the largest eligible in-service generator in each loaded unreferenced island.

    Equal declared MVA uses scheduled MW, then element ID, as deterministic ties.
    MATPOWER machine bases remain explicitly identified rather than invented ratings.
    """
    changes, assignments, diagnostics = {}, [], []
    for island in topology['islands']:
        if island['reference_gen'] is not None or not has_demand(island, index):
            continue
        candidates = []
        for ident in island['elements'].get('gens', ()):
            generator = index[ident]
            capacity, basis = capacity_mva(generator)
            voltage = generator.get('v_set_pu', generator.get('params', {}).get('usetp'))
            try:
                p, _ = gen_pq_mw(generator)
                voltage = float(voltage)
            except (MissingParameter, ValueError, TypeError):
                continue
            if capacity is not None and math.isfinite(p) and p >= 0 and math.isfinite(voltage) and voltage > 0:
                candidates.append((-capacity, -p, ident, basis, voltage))
        if not candidates:
            diagnostics.append(diagnostic('island_reference_unavailable',
                'No in-service generator with declared MVA and voltage setpoint can reference loaded island ' + str(island['id']) + '.'))
            continue
        negative_capacity, _, ident, basis, voltage = min(candidates)
        changes[ident] = True
        assignments.append({'island_id':island['id'], 'generator_id':ident,
                            'capacity_mva':-negative_capacity, 'capacity_basis':basis,
                            'capacity_is_rating':index[ident].get('origin') != 'matpower' or basis == 's_rated_mva',
                            'v_set_pu':voltage})
        diagnostics.append(diagnostic('island_reference_assigned',
            'Generator ' + ident + ' supplies the angular reference for a loaded island.',
            level='info', object_id=ident))
    return changes, assignments, diagnostics


def disconnected_generation(model, islands, index, base_result):
    """Report lost MW and signed Mvar from the solved base, or explicit scheduled inputs."""
    ids = {ident for island in islands if generation_only(island, index)
           for ident in island['elements'].get('gens', ())}
    base_gens = {row['id']:row for row in (base_result or {}).get('gens', ())}
    ids.update(g['id'] for g in model.get('gens', ()) if g.get('outserv') == 1 and g['id'] in base_gens)
    records, unknown = [], []
    for ident in sorted(ids):
        source = base_gens.get(ident)
        p = source.get('p_mw') if source is not None else None
        q = source.get('q_mvar') if source is not None else None
        basis = 'base_loadflow'
        if p is None or q is None:
            basis = 'scheduled_input'
            try:p, q = gen_pq_mw(index[ident])
            except (MissingParameter, ValueError, TypeError):p = q = None
        if p is None or q is None or not math.isfinite(p) or not math.isfinite(q):
            p = q = None
            unknown.append(ident)
        records.append({'generator_id':ident, 'bus_id':index[ident]['bus'],
                        'p_mw':p, 'q_mvar':q, 'basis':basis})
    return records, unknown
