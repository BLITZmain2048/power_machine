"""Presentation loading adapter for explicitly declared MATPOWER MVA ratings."""
import math
from power_machine.loading import branch_loading


def diagram_loading(model, result):
    summaries = branch_loading(model, result)
    index = {b['id']: b for b in model.get('branches', [])}
    for key, summary in summaries.items():
        branch = index.get(key, {})
        rating = branch.get('rate_a_mva')
        if (branch.get('outserv') or isinstance(rating, bool) or
                not isinstance(rating, (float, int)) or not math.isfinite(rating) or rating <= 0):
            continue
        # The shared adapter already masks S for unenergized or unsolved islands.
        values = []
        for end in summary['ends'].values():
            apparent = end.get('s_mva')
            end['loading_pct'] = 100 * apparent / rating if apparent is not None else None
            if end['loading_pct'] is not None:
                end.update(rating_mva=rating, rating_source='rate_a_mva')
                values.append(end['loading_pct'])
        if values:
            summary.update(loading_pct=max(values), cause=None,
                rating=dict(kind='power', rating_mva=rating, source='rate_a_mva', source_unit='MVA'),
                loading_metric=dict(quantity='apparent_power_ratio', numerator='S [MVA]',
                    denominator='rateA [MVA]', thermal_factors_included=False,
                    overload_factors_included=False, per_winding=False))
    return summaries
