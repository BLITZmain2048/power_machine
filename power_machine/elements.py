"Positive-sequence electrical stamps on explicit bus and system bases."

from __future__ import annotations

import cmath
import math
import warnings

import numpy as np


class MissingParameter(ValueError):
    "A required electrical stamping attribute is missing. code is missing_parameter."

    def __init__(self, attribute: str):
        self.attribute = attribute
        self.code = "missing_parameter"
        super().__init__(f"[missing_parameter] Missing REF attribute: {attribute}")


def _value(entity, name, *, type_only=False):
    sources = ("type_params",) if type_only else ("params", "type_params")
    for source in sources:
        value = entity.get(source, {}).get(name)
        if value is not None:
            return value
    raise MissingParameter(name)


def _optional(entity, name, default, *, type_only=False):
    try:
        return _value(entity, name, type_only=type_only)
    except MissingParameter:
        return default


def _positive(value, name):
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError('[invalid_input] ' + f"{name} must be positive and finite")
    return value


def _base(un_kv, s_base_mva):
    return _positive(un_kv, "un_kv") ** 2 / _positive(s_base_mva, "s_base_mva")


def _pi(y_series, y_shunt, un_i_kv, un_j_kv, s_base_mva):
    """Convert physical admittances in S to pu on independent voltage bases."""
    ui = _positive(un_i_kv, "un_i_kv")
    uj = _positive(un_j_kv, "un_j_kv")
    scale = 1 / _positive(s_base_mva, "s_base_mva")
    return np.array(
        [[(y_series + y_shunt / 2) * ui * ui, -y_series * ui * uj],
         [-y_series * ui * uj, (y_series + y_shunt / 2) * uj * uj]],
        dtype=complex,
    ) * scale


def y_line(branch, un_from_kv, un_to_kv, s_base_mva=100):
    "Return a lumped-pi ElmLne stamp. R1/X1 are total ohms and G1/B1 total microsiemens.\n\nExplicit B1 takes precedence over C1. C1 requires the exported frequency.\nCurrent flows out of each terminal. Parallel units are applied exactly once."
    p = branch.get("params", {})
    r = float(_value(branch, "R1"))
    x = float(_value(branch, "X1"))
    if r == x == 0:
        raise ValueError('[invalid_input] ' + "Zero R1/X1 requires additional ElmLne electrical parameters")
    try:
        g_us = float(_value(branch, "G1"))
    except MissingParameter:
        tp = branch.get("type_params", {})
        if tp.get("gline") is None or p.get("dline") is None:
            raise MissingParameter("G1 (total uS) or TypLne.gline (uS/km) with dline (km)")
        g_us = float(tp["gline"]) * float(p["dline"])
    if p.get("B1") is not None:
        b_us = float(p["B1"])
    else:
        c_uf = float(_value(branch, "C1"))
        f_hz = float(_optional(branch, "f_nom_hz", 50.0))
        b_us = 2 * math.pi * f_hz * c_uf
    n = _positive(_value(branch, "nlnum"), "nlnum")
    if int(n) != n:
        raise ValueError('[invalid_input] ' + "nlnum must be an integer")
    return _pi(n / complex(r, x), n * complex(g_us, b_us) * 1e-6,
               un_from_kv, un_to_kv, s_base_mva)


def _magnetizing(curmg_pct, pfe_kw, rated_mva, s_base_mva, un_kv, rated_kv):
    """Inductive no-load admittance on the system pu base."""
    i0 = float(curmg_pct) / 100
    g = float(pfe_kw) / (1000 * rated_mva)
    if i0 < 0 or g < 0 or g > i0 + 1e-12:
        raise ValueError('[invalid_input] ' + "Incompatible curmg/pfe")
    b = -math.sqrt(max(i0 * i0 - g * g, 0))
    return complex(g, b) * rated_mva / s_base_mva * (un_kv / rated_kv) ** 2


def _tap_factor(branch, suffix="", tap=None):
    tp = branch.get("type_params", {})
    if suffix == "" and "dutap" not in tp:
        return 1 + 0j
    if suffix in ("2", "3") and not bool(tp.get("itapch" + suffix, 0)):
        return 1 + 0j
    delta = float(_value(branch, "dutap" + suffix, type_only=True)) / 100
    neutral = float(_value(branch, "nntap0" + suffix, type_only=True))
    position = float(tap if tap is not None else _value(branch, "nntap" + suffix))
    lower = tp.get("ntpmn" + suffix)
    upper = tp.get("ntpmx" + suffix)
    if lower is not None and upper is not None:
        if float(lower) > float(upper):
            raise ValueError('[invalid_input] ' + "Inverted tap limits")
        position = min(max(position, float(lower)), float(upper))
    phi = math.radians(float(_value(branch, "phitr" + suffix, type_only=True)))
    factor = 1 + (position - neutral) * delta * cmath.exp(1j * phi)
    if abs(factor) == 0:
        raise ValueError('[invalid_input] ' + "Tap produces a zero ratio")
    return factor


def tr2_assumptions(branch):
    "Describe assumptions caused by missing exported two-winding transformer attributes."
    tp = branch.get("type_params", {})
    assumptions = []
    for name in ("itrdr", "itrdl"):
        if tp.get(name) is None:
            assumptions.append(f"{name}=0.5: leakage split was not exported")
    if branch.get("params", {}).get("ntnum") is None:
        assumptions.append("ntnum=1: unit count was not exported")
    return assumptions


def y_tr2(branch, un_hv_kv, un_lv_kv, s_base_mva=100, tap=None):
    "Return an ElmTr2 [HV, LV] T-equivalent Kron reduction in system pu.\n\nitrdr and itrdl split leakage. Missing splits use 0.5 with a warning.\nThe nominal winding ratio, phase group and operational tap remain explicit."
    s = _positive(_value(branch, "strn", type_only=True), "strn")
    uh = _positive(_value(branch, "utrn_h", type_only=True), "utrn_h")
    ul = _positive(_value(branch, "utrn_l", type_only=True), "utrn_l")
    uk = float(_value(branch, "uktr", type_only=True)) / 100
    r = float(_value(branch, "pcutr", type_only=True)) / (1000 * s)
    if uk < abs(r):
        raise ValueError('[invalid_input] ' + "uktr is smaller than the resistive component")
    x = math.sqrt(max(uk * uk - r * r, 0))
    if not complex(r, x):
        raise ValueError('[invalid_input] ' + "ElmTr2 has zero series impedance")
    gamma_r = float(_optional(branch, "itrdr", 0.5, type_only=True))
    gamma_x = float(_optional(branch, "itrdl", 0.5, type_only=True))
    if not 0 <= gamma_r <= 1 or not 0 <= gamma_x <= 1:
        raise ValueError('[invalid_input] ' + "itrdr/itrdl must be in [0,1]")
    z_base = s_base_mva / s * (ul / _positive(un_lv_kv, "un_lv_kv")) ** 2
    z_h = complex(r * gamma_r, x * gamma_x) * z_base
    z_l = complex(r * (1 - gamma_r), x * (1 - gamma_x)) * z_base
    ym = _magnetizing(_value(branch, "curmg", type_only=True),
                      _value(branch, "pfe", type_only=True), s,
                      s_base_mva, un_lv_kv, ul)
    side = int(_value(branch, "tap_side", type_only=True)
               if "dutap" in branch.get("type_params", {}) else 0)
    if side not in (0, 1):
        raise ValueError('[invalid_input] ' + "Unsupported tap_side")
    main = _tap_factor(branch, tap=tap)
    second = _tap_factor(branch, "2")
    th, tl = (main, second) if side == 0 else (second, main)
    phase = math.radians(float(_value(branch, "nt2ag", type_only=True)) * 30)
    a_h = (uh / _positive(un_hv_kv, "un_hv_kv")) / (ul / un_lv_kv) * th * cmath.exp(1j * phase)
    a_l = tl
    # T: z_h -- (ym a tierra) -- z_l. La reducción admite z_h o z_l nulos.
    denominator = z_h + z_l + ym * z_h * z_l
    if abs(denominator) == 0:
        raise ValueError('[invalid_input] ' + "Singular ElmTr2 T equivalent")
    n = _positive(_optional(branch, "ntnum", 1), "ntnum")
    if int(n) != n:
        raise ValueError('[invalid_input] ' + "ntnum must be an integer")
    return n * np.array([
        [(1 + ym * z_l) / denominator / abs(a_h) ** 2,
         -1 / denominator / (a_h.conjugate() * a_l)],
        [-1 / denominator / (a_h * a_l.conjugate()),
         (1 + ym * z_h) / denominator / abs(a_l) ** 2],
    ], dtype=complex)


def tr3_assumptions(branch3, allow_missing_losses=False):
    "List missing loss and magnetizing-data assumptions for a permitted partial export."
    if not allow_missing_losses:
        return []
    tp = branch3.get("type_params", {})
    assumptions = [f"pcut3_{key}=0: copper losses were not exported"
                   for key in "hml" if tp.get("pcut3_" + key) is None
                   and tp.get("pcutr3_" + key) is None]
    if (tp.get("curm3") is None and tp.get("curmg") is None) or tp.get("pfe") is None:
        assumptions.append("No magnetization: incomplete curm3/pfe")
    return assumptions


def y_tr3(branch3, un_hv_kv, un_mv_kv, un_lv_kv, s_base_mva=100,
          allow_missing_losses=False):
    "Stamp ElmTr3 [hv, mv, lv] leakage-star branches and eliminate the internal node."
    rated = [_positive(_value(branch3, "strn3_" + k, type_only=True), "strn3_" + k)
             for k in "hml"]
    voltage = [_positive(_value(branch3, "utrn3_" + k, type_only=True), "utrn3_" + k)
               for k in "hml"]
    base_voltage = [un_hv_kv, un_mv_kv, un_lv_kv]
    pairs = [(0, 1, "h"), (1, 2, "m"), (2, 0, "l")]
    z_pair = []
    for i, j, key in pairs:
        pair_s = min(rated[i], rated[j])
        uk = float(_value(branch3, "uktr3_" + key, type_only=True)) / 100
        tp = branch3.get("type_params", {})
        # REF 2024 denomina pcut3_* al ensayo [kW]; uktrr3_* [%]
        # expresa la misma resistencia sobre la menor potencia del par.
        loss_name = ("pcut3_" + key if tp.get("pcut3_" + key) is not None
                     else "pcutr3_" + key if tp.get("pcutr3_" + key) is not None
                     else "pcut3_" + key)
        pcu = float(_optional(branch3, loss_name, 0, type_only=True) if allow_missing_losses
                    else _value(branch3, loss_name, type_only=True))
        r = pcu / (1000 * pair_s)
        if tp.get(loss_name) is not None and tp.get("uktrr3_" + key) is not None and not math.isclose(
                100 * r, float(tp["uktrr3_" + key]), abs_tol=1e-5):
            raise ValueError('[invalid_input] ' + f"Incompatible pcut3_{key} and uktrr3_{key}")
        if uk < abs(r):
            raise ValueError('[invalid_input] ' + "uktr3 is smaller than the resistive component")
        z_pair.append(complex(r, math.sqrt(max(uk * uk - r * r, 0)))
                      * s_base_mva / pair_s)
    z_hm, z_ml, z_lh = z_pair
    z_winding = [(z_hm + z_lh - z_ml) / 2,
                 (z_hm + z_ml - z_lh) / 2,
                 (z_ml + z_lh - z_hm) / 2]
    # Classify cancellation only within the floating-point summation error.
    # This creates an exact constraint, never a small substitute impedance.
    roundoff = np.finfo(float).eps * sum(abs(z) for z in z_pair)
    z_winding = [0j if abs(z) <= roundoff else z for z in z_winding]
    a = np.array([
        voltage[i] / base_voltage[i] * cmath.exp(1j * math.radians(
            -float(_value(branch3, "nt3ag_" + k, type_only=True)) * 30))
        for i, k in enumerate("hml")], dtype=complex)
    for suffix in ("", "2", "3"):
        if suffix == "" and "dutap" not in branch3.get("type_params", {}):
            continue
        if suffix and not branch3.get("type_params", {}).get("itapch" + suffix, 0):
            continue
        side = int(_value(branch3, "tap_side" + suffix, type_only=True))
        if side not in (0, 1, 2):
            raise ValueError('[invalid_input] ' + "Unsupported ElmTr3 tap_side")
        a[side] *= _tap_factor(branch3, suffix)
    tp = branch3.get("type_params", {})
    for i, key in enumerate("hml"):
        if tp.get("du3tp_" + key) is None:
            continue
        position = float(_value(branch3, "n3tap_" + key))
        lower = float(_value(branch3, "n3tmn_" + key, type_only=True))
        upper = float(_value(branch3, "n3tmx_" + key, type_only=True))
        if lower > upper:
            raise ValueError('[invalid_input] ' + f"Inverted n3tmn/n3tmx_{key} limits")
        position = min(max(position, lower), upper)
        neutral = float(_value(branch3, "n3tp0_" + key, type_only=True))
        step = float(_value(branch3, "du3tp_" + key, type_only=True)) / 100
        phase = math.radians(float(_optional(branch3, "ph3tr_" + key, 0,
                                             type_only=True)))
        factor = 1 + (position - neutral) * step * cmath.exp(1j * phase)
        if abs(factor) == 0:
            raise ValueError('[invalid_input] ' + f"3W tap {key} produces a zero ratio")
        a[i] *= factor
    if allow_missing_losses and ((tp.get("curm3") is None and tp.get("curmg") is None)
                                 or tp.get("pfe") is None):
        ym = 0j
    else:
        current_name = "curm3" if tp.get("curm3") is not None else "curmg"
        ym = _magnetizing(_value(branch3, current_name, type_only=True),
                          _value(branch3, "pfe", type_only=True), rated[0],
                          s_base_mva, voltage[0], voltage[0])
    # La rama de magnetización está en el nodo estrella por defecto del TechRef.
    zero = [i for i, z in enumerate(z_winding) if z == 0]
    if len(zero) > 1:
        raise ValueError('[invalid_input] Multiple ideal ElmTr3 windings require ratio-constrained topology')
    if zero:
        # v_star = v_k/a_k exactly. Each finite arm contributes
        # y_i (v_i/a_i - v_k/a_k), with conjugate current transforms.
        k = zero[0]
        matrix = np.zeros((3, 3), complex)
        matrix[k, k] = ym / abs(a[k]) ** 2
        for i, z in enumerate(z_winding):
            if i == k:
                continue
            c = np.zeros(3, complex)
            c[i], c[k] = 1/a[i], -1/a[k]
            matrix += np.outer(c.conjugate(), c) / z
        return matrix
    y = np.array([1 / z for z in z_winding], dtype=complex)
    d = np.diag(y / (a * a.conjugate()))
    left = y / a.conjugate()
    right = y / a
    return d - np.outer(left, right) / (sum(y) + ym)


def y_zpu(branch, un_i_kv, un_j_kv, s_base_mva=100):
    "Stamp directed ElmZpu impedances per the technical reference, without symmetrization."
    p = branch.get("params", {})
    sn = _positive(_value(branch, "Sn"), "Sn")
    k = float(_value(branch, "uratio")) * cmath.exp(
        -1j * math.radians(float(_value(branch, "nphshift")) * 30
                           + float(_value(branch, "ag"))))
    if abs(k) == 0:
        raise ValueError('[invalid_input] ' + "uratio must be nonzero")
    z_ij = complex(float(_value(branch, "r_pu")), float(_value(branch, "x_pu")))
    z_ji = complex(float(_value(branch, "r_pu_ji")), float(_value(branch, "x_pu_ji")))
    if not z_ij or not z_ji:
        raise ValueError('[invalid_input] ' + "Zero ElmZpu series impedance requires topology processing")
    yi = complex(float(_value(branch, "gi_pu")), float(_value(branch, "bi_pu")))
    yj = complex(float(_value(branch, "gj_pu")), float(_value(branch, "bj_pu")))
    # El TechRef define valores p.u. sobre Sn y tensiones nominales de barras.
    _base(un_i_kv, s_base_mva)
    _base(un_j_kv, s_base_mva)
    factor = sn / s_base_mva
    return factor * np.array([
        [abs(k) ** 2 * (1 / z_ij + yi), -k.conjugate() / z_ij],
        [-k / z_ji, 1 / z_ji + yj],
    ], dtype=complex)


def classify_zpu(branch):
    "Classify extreme impedances for diagnostics. The numerical stamp is unchanged."
    zi = abs(complex(float(_value(branch, "r_pu")), float(_value(branch, "x_pu"))))
    zj = abs(complex(float(_value(branch, "r_pu_ji")), float(_value(branch, "x_pu_ji"))))
    if zi < 1e-6 and zj < 1e-6:
        return "near_short"
    if zi > 1e10 and zj > 1e10:
        return "near_open"
    return "normal"


def series_bypassed(branch):
    """Recognize an explicitly exported bypass state, absent means inserted."""
    return any(_optional(branch, key, 0) == 1 for key in ("bypass", "bypassed", "is_bypassed"))


def series_impedance(branch):
    """Physical positive-sequence impedance in ohms, REF 2024 native inputs."""
    if branch["class"] == "ElmScap":
        x = _optional(branch, "xcap", None)
        if x is None:
            b = _positive(_value(branch, "bcap"), "bcap")
            unit = branch.get("units", {}).get("bcap", "S")
            if unit not in ("S", "uS", "\u00b5S"):
                raise ValueError("[invalid_input] ElmScap.bcap requires S or uS")
            x = 1 / (b * (1e-6 if unit != "S" else 1))
        x = -_positive(x, "xcap")
        r = float(_optional(branch, "rrea", _optional(branch, "rcap", 0)))
        g = _optional(branch, "gcap", None)
        if g is None and _optional(branch, "tandc", None) is not None:
            g = float(_value(branch, "tandc")) / -x
        if g is not None:
            unit = branch.get("units", {}).get("gcap", "S")
            g = float(g) * (1e-6 if unit in ("uS", "\u00b5S") else 1)
            z = r + 1 / complex(g, -1/x)
            if not math.isfinite(abs(z)) or z.real < 0:
                raise ValueError("[invalid_input] Invalid ElmScap losses")
            return z
    else:
        x = _optional(branch, "xrea", None)
        r = _optional(branch, "rrea", None)
        if x is None or r is None:
            sn = _positive(_value(branch, "Sn"), "Sn")
            zb = _positive(_value(branch, "ucn"), "ucn") ** 2 / sn
            if r is None:
                r = float(_value(branch, "Pcu")) / (1000 * sn) * zb
            if x is None:
                z = float(_value(branch, "uk")) / 100 * zb
                if z < abs(r):
                    raise ValueError("[invalid_input] ElmSind.uk is smaller than its resistance")
                x = math.sqrt(z*z-r*r)
    z = complex(float(r), float(x))
    if not math.isfinite(abs(z)) or z.real < 0 or z == 0:
        raise ValueError("[invalid_input] Invalid series impedance")
    return z


def y_series(branch, un_i_kv, un_j_kv, s_base_mva=100):
    """Stamp ElmScap or ElmSind. Bypasses are contracted by topology."""
    if series_bypassed(branch):
        raise ValueError("[invalid_input] Bypass requires ideal topology contraction")
    return _pi(1 / series_impedance(branch), 0j, un_i_kv, un_j_kv, s_base_mva)


def y_shunt(shunt, un_kv, s_base_mva=100, *, f_nom_hz=None):
    """Balanced fundamental-frequency REF shunt/filter, all five shtype circuits."""
    raw_kind = float(_value(shunt, "shtype"))
    if not raw_kind.is_integer() or raw_kind not in range(5):
        raise ValueError("[invalid_input] shtype must be one of 0,1,2,3,4")
    kind = int(raw_kind)
    rated = _positive(_value(shunt, "ushnm"), "ushnm")
    active, maximum = (float(_value(shunt, key)) for key in ("ncapa", "ncapx"))
    if not active.is_integer() or not maximum.is_integer() or not 0 <= active <= maximum:
        raise ValueError("[invalid_input] ncapa/ncapx must be integers with 0 <= ncapa <= ncapx")
    # Native layout values are per phase. Delta requires three times the admittance.
    tech = _optional(shunt, "ctech", 1)
    if tech not in (0, 1, 2, "3PH-D", "3PH-YN", "3PH-Y"):
        raise ValueError("[invalid_input] Only balanced three-phase shunt technology is supported")
    factor = 3 if tech in (0, "3PH-D") else 1
    if _optional(shunt, "iTaps", 0):
        raise MissingParameter("ElmShnt measurement-table model (iTaps)")
    def capacitance_y():
        q = _optional(shunt, "qcapn", None)
        b = _optional(shunt, "bcap", None)
        if b is None:
            if q is None:
                raise MissingParameter("qcapn (Mvar) or bcap (uS)")
            b = float(q) / (rated*rated*factor) * 1e6
        b = _positive(b, "bcap") * 1e-6
        g = _optional(shunt, "gparac", None)
        if g is None:
            g = b * float(_optional(shunt, "tandc", 0))
        else:
            g = float(g)*1e-6
        return complex(g, b)
    if kind == 2:
        admittance = capacitance_y()
    else:
        x = _optional(shunt, "xrea", None)
        if x is None:
            x = factor*rated*rated / _positive(_value(shunt, "qrean"), "qrean")
        r = _optional(shunt, "rrea", None)
        if r is None:
            quality = float(_value(shunt, "grea"))
            r = float(x)/quality if quality else 0
        z = complex(float(r), float(x))
        if kind == 4:
            # Auxiliary C1 is in series with R-L, Rp bridges that series arm.
            f = _positive(_value(shunt, "f_nom_hz") if f_nom_hz is None else f_nom_hz, "f_nom_hz")
            c1 = _positive(_value(shunt, "c1"), "c1")*1e-6
            c2 = _positive(_value(shunt, "c2"), "c2")*1e-6
            z += 1/(1j*2*math.pi*f*c1)
            yc = 1j*2*math.pi*f*c2
        elif kind in (0, 3):
            yc = capacitance_y()
        if kind in (3, 4):
            rp = float(_value(shunt, "rpara"))
            if rp < 0:
                raise ValueError("[invalid_input] rpara must be nonnegative")
            if rp:
                z = z*rp/(z+rp)
        if kind in (0, 3, 4):
            z += 1/yc
        if z == 0:
            raise ValueError("[invalid_input] Singular ElmShnt impedance")
        admittance = 1/z
    if not math.isfinite(abs(admittance)) or admittance.real < 0:
        raise ValueError("[invalid_input] Invalid ElmShnt admittance")
    return active * factor * admittance * _base(un_kv, s_base_mva)


def gen_pq_mw(gen):
    "Return total injected P in MW and Q in Mvar, including ngnum and scale0."
    cls = gen.get("class")
    if cls == "ElmXnet":
        from .external_grid import powers, q_limits
        return powers(gen)
    if cls not in ("ElmSym", "ElmGenstat", "ElmPvsys"):
        raise ValueError('[invalid_input] ' + f"Unsupported generator class: {cls}")
    n = _positive(_value(gen, "ngnum"), "ngnum")
    scale = float(_optional(gen, "scale0", 1.0))
    divisor = 1000 if cls == "ElmPvsys" else 1
    return (float(_value(gen, "pgini")) * n * scale / divisor,
            float(_value(gen, "qgini")) * n * scale / divisor)


def gen_q_limits_mvar(gen):
    "Convert q_min/q_max from pu per machine to total Mvar using sgn and ngnum.\n\nExplicit total limits take precedence. Missing machine bases are not inferred."
    cls = gen.get("class")
    if cls == "ElmXnet":
        from .external_grid import powers, q_limits
        return q_limits(gen)
    if cls not in ("ElmSym", "ElmGenstat", "ElmPvsys"):
        raise ValueError('[invalid_input] ' + f"Unsupported generator class: {cls}")
    if cls == "ElmSym":
        sgn = _value(gen, "sgn", type_only=True)
    else:
        sgn = gen.get("params", {}).get("sgn")
        if sgn is None:
            raise MissingParameter("sgn")
    if cls == "ElmPvsys":
        sgn = float(sgn) / 1000  # kVA -> MVA
    n = _positive(_value(gen, "ngnum"), "ngnum")
    return (float(_value(gen, "q_min")) * float(sgn) * n,
            float(_value(gen, "q_max")) * float(sgn) * n)


def load_pq_mw(load):
    "Return consumed P in MW and Q in Mvar at nominal voltage with ElmLod scale0.\n\nNegative values remain explicit injections. Unexported ZIP dependence is not inferred."
    scale = float(_value(load, "scale0"))
    return (float(_value(load, "plini")) * scale,
            float(_value(load, "qlini")) * scale)


def load_voltage_dependence(load):
    "Return exported TypLod coefficients and exponents for P/Q(u/u0), or None if missing."
    names = ("aP", "bP", "cP", "aQ", "bQ", "cQ",
             "e_aP", "e_bP", "e_cP", "e_aQ", "e_bQ", "e_cQ")
    tp = load.get("type_params", {})
    missing = [name for name in names if tp.get(name) is None]
    if missing:
        warnings.warn("[missing_load_voltage_dependence] TypLod voltage dependence is incomplete: "
                      + ", ".join(missing), RuntimeWarning, stacklevel=2)
        return None
    return {name: float(tp[name]) for name in names}
