"""Native balanced reference simulator ElmXnet data. No machine rating is inferred."""
import math
from .elements import MissingParameter


def number(item, key, unit=None):
    value=item.get('params',{}).get(key)
    if value is None:raise MissingParameter(key)
    if unit and item.get('units',{}).get(key)!=unit:
        raise ValueError(f'ElmXnet {key} requires {unit}')
    value=float(value)
    if not math.isfinite(value):raise ValueError(f'Nonfinite ElmXnet {key}')
    return value


def powers(item):
    mode=item.get('params',{}).get('bustp')
    if mode not in ('SL','PV','PQ'):raise ValueError('ElmXnet bustp requires SL, PV or PQ')
    # SL has no specified injection. PV has no specified Q.
    return (0.0 if mode=='SL' else number(item,'pgini','MW'),
            number(item,'qgini','Mvar') if mode=='PQ' else 0.0)


def q_limits(item):
    low,high=item.get('q_min_mvar'),item.get('q_max_mvar')
    if low is None or high is None:raise MissingParameter('q_min_mvar/q_max_mvar')
    return float(low),float(high)


def normalize_external(item):
    p=item['params']
    mode=p.get('bustp')
    item.update(ref=mode=='SL',mode='constq' if mode=='PQ' else 'constv',
                v_set_pu=p.get('usetp'),p_mw=p.get('pgini',0),q_mvar=p.get('qgini',0))
    return item


def impedance(item, un_kv, *, case='maximum', voltage_factor=1.0):
    """Z1 [ohm] = c Un²/Sk, or c Un/(sqrt(3) Ik). R/X is native.

    xntrn/xntrnmin (X/R) are accepted only when R/X is absent.
    No minimum data are substituted by maximum data.
    """
    if case not in ('maximum','minimum'):raise ValueError('Invalid ElmXnet case')
    suffix='min' if case=='minimum' else ''
    p=item.get('params',{})
    sk='snss'+suffix
    if p.get(sk) is not None:
        s=number(item,sk,'MVA')
    else:
        ik=number(item,'ikss'+suffix,'kA')
        s=math.sqrt(3)*float(un_kv)*ik
    if s<=0 or un_kv<=0 or voltage_factor<=0:raise ValueError('ElmXnet Sk, Un and c must be positive')
    if p.get('rntxn'+suffix) is not None:rx=number(item,'rntxn'+suffix)
    else:
        xr=number(item,'xntrn'+suffix)
        if xr<=0:raise ValueError('ElmXnet X/R must be positive')
        rx=1/xr
    if rx<0:raise ValueError('ElmXnet R/X must be nonnegative')
    x=voltage_factor*un_kv**2/s/math.hypot(1,rx)
    return complex(rx*x,x)
