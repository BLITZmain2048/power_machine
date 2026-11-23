"""Motor de sensibilidades por diferencias finitas del flujo AC existente."""
from __future__ import annotations

import copy
from dataclasses import asdict
from pathlib import Path

from ..loadflow import run_loadflow
from ..lf_status import energized_status
from ..contingency import ContingencyOptions, run_contingencies
from .results import diagnostic, envelope, fingerprint, json_value
from .schema import SensitivityOptions


WORK_ROOT = Path(__file__).resolve().parents[2]


def _cancelled(cancel) -> bool:
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _identity(item: dict) -> dict:
    return {"object_id": item.get("object_id", item.get("id")),
            "relative_path": item.get("relative_path", item.get("path")),
            "name": item.get("name", item.get("id"))}


def _options(options: SensitivityOptions) -> dict:
    return {"q_limits": options.q_limits, "max_iter": options.max_iter,
            "tolerance_pu": options.tolerance_pu, "warm_start": False}


def _temporary_source(bus_id: str, p_mw: float, q_mvar: float, number: int) -> dict:
    return {"id": f"__sens_source_{number}", "object_id": None, "relative_path": None,
            "path": f"sensitivities/temporary_source_{number}", "name": "Inyección temporal",
            "class": "ElmGenstat", "bus": bus_id, "outserv": 0, "ref": False,
            "mode": "constq", "v_set_pu": 1.0, "s_rated_mva": None,
            "params": {"pgini": p_mw, "qgini": q_mvar, "ngnum": 1, "scale0": 1}}


def _solve_with_injections(model: dict, scenario: dict, injections: tuple[tuple[str, float, float], ...], options):
    work = copy.deepcopy(model)
    work.setdefault("gens", [])
    work["gens"].extend(_temporary_source(bus, p_mw, q_mvar, number)
                        for number, (bus, p_mw, q_mvar) in enumerate(injections, 1))
    return run_loadflow(work, scenario=scenario, options=options)


def _solved(result: dict) -> bool:
    return energized_status(result)["ok"]


def _bus_rows(result: dict) -> dict:
    return {row["id"]: row for row in result.get("buses", []) if row.get("u_pu") is not None}


def _branch_rows(result: dict) -> dict:
    return {row["id"]: row for row in result.get("branches", []) if row.get("p_from_mw") is not None}


def _loss_mw(result: dict):
    return result.get("totals", {}).get("branch_loss_p_mw")


def _central(base, plus, minus, field: str, step: float):
    base_value, plus_value, minus_value = base.get(field), plus.get(field), minus.get(field)
    if plus_value is None or minus_value is None:
        return None
    return (plus_value - minus_value) / (2.0 * step)


def _contingency_verification(model, scenario, options: SensitivityOptions, outage_id: str):
    """Obtiene el post-retiro desde el motor N-1 para no duplicar su semántica."""
    result = run_contingencies(model, scenario,
                               ContingencyOptions(orders=(1,), q_limits=options.q_limits),
                               selected_ids=[outage_id],
                               filter_reason="Verificación interna sensitivities de LODF")
    cases = result.get("payload", {}).get("cases", [])
    return cases[0] if cases else None


def run_sensitivities_legacy(model: dict, scenario: dict, options: SensitivityOptions, *, cancel=None, progress=None) -> dict:
    """Calcula dU/dP, dU/dQ, PTDF y LODF alrededor del punto de operación.

    PTDF y LODF se expresan para la transacción desde el extremo ``from`` al
    extremo ``to`` de la rama retirada. La perturbación de barra elegida se
    usa para las sensibilidades de tensión, flujo y pérdidas.
    """
    result = envelope(model, scenario, asdict(options))
    payload = result["payload"]
    try:
        if not isinstance(model, dict) or not isinstance(scenario, dict):
            raise ValueError("model y scenario deben ser diccionarios")
        buses = {row["id"]: row for row in model.get("buses", [])}
        branches = {row["id"]: row for row in model.get("branches", [])}
        if options.injection_bus_id not in buses:
            raise ValueError("La barra de inyección no existe")
        if buses[options.injection_bus_id].get("outserv", 0) == 1:
            raise ValueError("La barra de inyección está fuera de servicio")
        outage_id = options.outage_branch_id or next(iter(branches), None)
        if outage_id is None or outage_id not in branches:
            raise ValueError("Se requiere una rama existente para PTDF y LODF")
        if branches[outage_id].get("outserv", 0) == 1:
            raise ValueError("La rama de salida está fuera de servicio")
        if _cancelled(cancel):
            result["status"] = "cancelled"
            return json_value(result)
        if progress:
            progress({"stage": "base", "fraction": 0.0})
        base = run_loadflow(copy.deepcopy(model), scenario=scenario, options=_options(options))
        payload["base"] = base
        payload["injection_bus"] = {"id": options.injection_bus_id, **_identity(buses[options.injection_bus_id])}
        payload["outage_branch"] = {"id": outage_id, **_identity(branches[outage_id])}
        payload["finite_difference"] = {"method": "centrada", "step_p_mw": options.step_p_mw,
                                          "step_q_mvar": options.step_q_mvar,
                                          "formula": "(f(+h)-f(-h))/(2h)"}
        if not _solved(base):
            result["status"] = "partial"
            result["diagnostics"].append(diagnostic("base_not_converged", "El flujo base no quedó completamente resuelto", level="error"))
            return json_value(result)
        if _cancelled(cancel):
            result["status"] = "cancelled"
            return json_value(result)
        if progress:
            progress({"stage": "voltage_p", "fraction": 0.15})
        p_plus = _solve_with_injections(model, scenario, ((options.injection_bus_id, options.step_p_mw, 0.0),), _options(options))
        p_minus = _solve_with_injections(model, scenario, ((options.injection_bus_id, -options.step_p_mw, 0.0),), _options(options))
        if progress:
            progress({"stage": "voltage_q", "fraction": 0.40})
        q_plus = _solve_with_injections(model, scenario, ((options.injection_bus_id, 0.0, options.step_q_mvar),), _options(options))
        q_minus = _solve_with_injections(model, scenario, ((options.injection_bus_id, 0.0, -options.step_q_mvar),), _options(options))
        perturbations = {"p_plus": p_plus, "p_minus": p_minus, "q_plus": q_plus, "q_minus": q_minus}
        payload["perturbation_converged"] = {key: _solved(value) for key, value in perturbations.items()}
        if not all(payload["perturbation_converged"].values()):
            result["status"] = "partial"
            result["diagnostics"].append(diagnostic("perturbation_not_converged", "Una perturbación finita no convergió", level="warning"))
        base_buses, p_buses, m_buses = _bus_rows(base), _bus_rows(p_plus), _bus_rows(p_minus)
        qp_buses, qm_buses = _bus_rows(q_plus), _bus_rows(q_minus)
        payload["buses"] = [{"id": bus_id, **_identity(buses[bus_id]), "un_kv": buses[bus_id].get("un_kv"),
                              "u_pu": row.get("u_pu"),
                              "du_dp_pu_per_mw": _central(row, p_buses.get(bus_id, {}), m_buses.get(bus_id, {}), "u_pu", options.step_p_mw),
                              "du_dq_pu_per_mvar": _central(row, qp_buses.get(bus_id, {}), qm_buses.get(bus_id, {}), "u_pu", options.step_q_mvar)}
                            for bus_id, row in sorted(base_buses.items())]
        base_branches, p_branches, m_branches = _branch_rows(base), _branch_rows(p_plus), _branch_rows(p_minus)
        p_loss = (_loss_mw(p_plus), _loss_mw(p_minus))
        payload["loss_sensitivity_dp_mw_per_mw"] = None if None in p_loss else (p_loss[0] - p_loss[1]) / (2 * options.step_p_mw)
        branch = branches[outage_id]
        if _cancelled(cancel):
            result["status"] = "cancelled"
            return json_value(result)
        if progress:
            progress({"stage": "ptdf_lodf", "fraction": 0.60})
        f_bus, t_bus = branch.get("bus_from"), branch.get("bus_to")
        transaction_plus = _solve_with_injections(model, scenario, ((f_bus, options.step_p_mw, 0.0), (t_bus, -options.step_p_mw, 0.0)), _options(options))
        transaction_minus = _solve_with_injections(model, scenario, ((f_bus, -options.step_p_mw, 0.0), (t_bus, options.step_p_mw, 0.0)), _options(options))
        if not _solved(transaction_plus) or not _solved(transaction_minus):
            result["status"] = "partial"
            result["diagnostics"].append(diagnostic("transaction_not_converged", "No se obtuvo PTDF por transacción centrada", level="warning", object_id=outage_id, relative_path=branch.get("relative_path", branch.get("path"))))
        tp_branches, tm_branches = _branch_rows(transaction_plus), _branch_rows(transaction_minus)
        ptdf = {branch_id: _central(base_row, tp_branches.get(branch_id, {}), tm_branches.get(branch_id, {}), "p_from_mw", options.step_p_mw)
                for branch_id, base_row in base_branches.items()}
        denominator = None if ptdf.get(outage_id) is None else 1.0 - ptdf[outage_id]
        if denominator is None or abs(denominator) < 1e-8:
            result["status"] = "partial"
            result["diagnostics"].append(diagnostic("lodf_singular", "El denominador 1-PTDF_kk es nulo o casi nulo", level="warning", object_id=outage_id, relative_path=branch.get("relative_path", branch.get("path"))))
        lodf = {branch_id: None if denominator is None or abs(denominator) < 1e-8 or value is None else value / denominator
                for branch_id, value in ptdf.items()}
        if progress:
            progress({"stage": "lodf_verification", "fraction": 0.80})
        case = _contingency_verification(model, scenario, options, outage_id)
        actual = _branch_rows(case.get("loadflow", {})) if case and case.get("loadflow") else {}
        outage_base_p = base_branches.get(outage_id, {}).get("p_from_mw")
        rows = []
        for branch_id, base_row in sorted(base_branches.items()):
            value = lodf.get(branch_id)
            predicted = None if value is None or outage_base_p is None else base_row["p_from_mw"] + value * outage_base_p
            actual_p = actual.get(branch_id, {}).get("p_from_mw")
            item = branches.get(branch_id, {})
            rows.append({"id": branch_id, **_identity(item), "class": item.get("class"),
                         "bus_from": base_row.get("bus_from"), "bus_to": base_row.get("bus_to"),
                         "p_from_mw": base_row.get("p_from_mw"), "dp_dp_mw_per_mw": _central(base_row, p_branches.get(branch_id, {}), m_branches.get(branch_id, {}), "p_from_mw", options.step_p_mw),
                         "ptdf_mw_per_mw": ptdf.get(branch_id), "lodf_mw_per_mw": value,
                         "predicted_post_outage_p_mw": predicted, "actual_post_outage_p_mw": actual_p,
                         "lodf_error_mw": None if predicted is None or actual_p is None else actual_p - predicted})
        payload["branches"] = rows
        payload["lodf_verification"] = {"engine": "power_machine.contingency", "case_status": case.get("status") if case else None,
                                        "outage_base_p_mw": outage_base_p,
                                        "max_abs_error_mw": max((abs(row["lodf_error_mw"]) for row in rows if row["lodf_error_mw"] is not None), default=None)}
        if case is None or case.get("status") not in ("completed", "limit_violation"):
            result["status"] = "partial"
            result["diagnostics"].append(diagnostic("lodf_reference_unavailable", "La contingencia no produjo un flujo post-retiro completo", level="warning", object_id=outage_id, relative_path=branch.get("relative_path", branch.get("path"))))
        if progress:
            progress({"stage": "done", "fraction": 1.0})
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        result["status"] = "invalid_input"
        result["diagnostics"].append(diagnostic("invalid_input", exc, level="error"))
    return json_value(result)


from .ac import run_sensitivities


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    """Punto de entrada para trabajos GUI, con snapshots y hashes obligatorios."""
    try:
        if not isinstance(request, dict) or not str(request.get("schema")).endswith("-parity-request-v1") or request.get("module") != "sensitivities":
            raise ValueError("Esquema o módulo de request incompatible")
        root = Path(request["root"]).resolve()
        if root == Path(root.anchor):
            raise ValueError("La raíz debe ser una carpeta de trabajo")
        snapshot = copy.deepcopy(request["snapshot"])
        model, study = snapshot["model"], snapshot["study"]
        hashes = snapshot.get("fingerprints", {})
        if hashes.get("model_sha256") != fingerprint(model) or hashes.get("study_sha256") != fingerprint(study):
            raise ValueError("Fingerprint incompatible del snapshot")
        return run_sensitivities(model, study.get("scenario", {}), SensitivityOptions(**request.get("options", {})), cancel=cancel, progress=progress)
    except (ValueError, KeyError, TypeError) as exc:
        result = envelope({}, {}, {})
        result["model_sha256"] = None
        result["status"] = "invalid_input"
        result["diagnostics"].append(diagnostic("invalid_request", exc, level="error"))
        return json_value(result)
