"""Despacho activo [MW], sin recorte de P ni límites Q.

La referencia absorbe el residuo. s_rated_mva solo produce una marca.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from ..elements import load_pq_mw
from ..lf_status import energized_status


@dataclass(frozen=True)
class DispatchBase:
    reference_id: str
    reference_p_mw: float
    nonreference_p_mw: dict[str, float]
    loss_p_mw: float


def dispatch_base(model, base_result) -> DispatchBase:
    """P_ref0 y P_no_ref0 [MW] desde LF base, nunca desde pgini de referencia."""
    if not energized_status(base_result)["ok"]:
        raise ValueError("La LF base no converge en las islas energizadas")
    active = [g for g in model["gens"] if not g.get("outserv", 0)]
    refs = [g for g in active if g.get("ref")]
    if len(refs) != 1:
        raise ValueError("QSS requiere exactamente una referencia activa")
    powers = {g["id"]: g["p_mw"] for g in base_result["gens"]}
    if any(powers.get(g["id"]) is None for g in active):
        raise ValueError("Generación base ausente")
    ref_id = refs[0]["id"]
    nonref = {g["id"]: powers[g["id"]] for g in active if g["id"] != ref_id}
    return DispatchBase(ref_id, powers[ref_id], nonref,
                        base_result["totals"]["branch_loss_p_mw"])


def plan_dispatch(model, step, base, previous_loss_p_mw, rule="reference"):
    """Un escenario por instante, factores multiplican scale0 base.

    lambda = (P_carga + pérdidas previas - P_ref0) / suma P_no_ref0.
    Todas las potencias activas están en MW, carga positiva consumida.
    """
    if rule not in ("reference", "proportional"):
        raise ValueError(f"Regla de despacho desconocida: {rule}")
    load_scale = {}
    p_load_mw = q_load_mvar = 0.0
    for load in model["loads"]:
        factor = step.factors[load["id"]]
        load_scale[load["id"]] = float(load["params"]["scale0"]) * factor
        if not load.get("outserv", 0):
            p_mw, q_mvar = load_pq_mw(load)
            p_load_mw += p_mw * factor
            q_load_mvar += q_mvar * factor
    lambda_pu = 1.0
    if rule == "proportional":
        denominator_mw = sum(base.nonreference_p_mw.values())
        if denominator_mw == 0:
            raise ValueError("Despacho proporcional con suma P_no_ref0 igual a cero")
        lambda_pu = (p_load_mw + previous_loss_p_mw - base.reference_p_mw) / denominator_mw
    gen_p_mw = {key: value * lambda_pu for key, value in base.nonreference_p_mw.items()}
    return {"scenario": {"load_scale": load_scale, "gen_p_mw": gen_p_mw},
            "lambda_pu": lambda_pu, "load_p_mw": p_load_mw, "load_q_mvar": q_load_mvar,
            "previous_loss_p_mw": previous_loss_p_mw,
            "reference_expected_p_mw": p_load_mw + previous_loss_p_mw - sum(gen_p_mw.values())}


def apparent_limit(p_mw, q_mvar, s_rated_mva):
    """S [MVA] y excedencia booleana. Dato ausente conserva None, nunca cero."""
    if p_mw is None or q_mvar is None:
        return None, None
    s_mva = math.hypot(p_mw, q_mvar)
    return s_mva, s_mva > s_rated_mva if s_rated_mva is not None else None
