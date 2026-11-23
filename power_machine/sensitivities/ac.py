"""Cálculo sensitivities con criterio de islas energizadas y convergencia de paso."""
from __future__ import annotations

import copy
from dataclasses import asdict

from ..contingency import ContingencyOptions, run_contingencies
from ..lf_status import energized_status
from ..loadflow import run_loadflow
from .results import diagnostic, envelope, json_value


def _source(bus_id, p_mw, q_mvar, ordinal):
    return {"id": f"__sens_source_{ordinal}", "object_id": None, "relative_path": None,
            "path": f"sensitivities/temporary_source_{ordinal}", "name": "Inyección temporal",
            "class": "ElmGenstat", "bus": bus_id, "outserv": 0, "ref": False,
            "mode": "constq", "v_set_pu": 1.0, "s_rated_mva": None,
            "params": {"pgini": p_mw, "qgini": q_mvar, "ngnum": 1, "scale0": 1}}


def _options(options):
    return {"q_limits": options.q_limits, "max_iter": options.max_iter,
            "tolerance_pu": options.tolerance_pu, "warm_start": False}


def _solve(model, scenario, injections, options):
    work = copy.deepcopy(model)
    work.setdefault("gens", []).extend(_source(bus, p, q, number)
                                         for number, (bus, p, q) in enumerate(injections, 1))
    return run_loadflow(work, scenario=scenario, options=options)


def _rows(flow, group, key):
    return {row["id"]: row for row in flow.get(group, []) if row.get(key) is not None}


def _central(plus, minus, key, step):
    first, second = plus.get(key), minus.get(key)
    return None if first is None or second is None else (first - second) / (2.0 * step)


def _identity(item):
    return {"object_id": item.get("object_id", item.get("id")),
            "relative_path": item.get("relative_path", item.get("path")),
            "name": item.get("name", item.get("id"))}


def _regime(flow):
    status = energized_status(flow)
    return {"reference_generators": sorted(row.get("reference_gen") for row in flow.get("islands", [])
                                             if row.get("status") == "resuelto" and row.get("reference_gen")),
            "q_limited_generators": sorted(row["id"] for row in flow.get("gens", []) if row.get("q_limit")),
            "energized_islands": status["n_solved"], "failed_energized_islands": status["n_failed"]}


def _record_status(payload, diagnostics, name, flow):
    status = energized_status(flow)
    payload.setdefault("flow_status", {})[name] = status
    if status["n_deenergized"]:
        diagnostics.append(diagnostic("deenergized_islands", f"{name}: {status['message']}", level="warning"))
    return status


def _cancelled(cancel):
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _pair(model, scenario, injections, step, options):
    plus = tuple((bus, p * step, q * step) for bus, p, q in injections)
    minus = tuple((bus, -p * step, -q * step) for bus, p, q in injections)
    return _solve(model, scenario, plus, options), _solve(model, scenario, minus, options)


def run_sensitivities(model, scenario, options, *, cancel=None, progress=None):
    """Derivadas AC centrales, explícitamente distintas de un PTDF DC."""
    result = envelope(model, scenario, asdict(options))
    payload, diagnostics = result["payload"], result["diagnostics"]
    try:
        buses = {row["id"]: row for row in model.get("buses", [])}
        branches = {row["id"]: row for row in model.get("branches", [])}
        bus_id = options.injection_bus_id
        outage_id = options.outage_branch_id or next(iter(branches), None)
        if not isinstance(scenario, dict) or bus_id not in buses or buses[bus_id].get("outserv", 0) == 1:
            raise ValueError("La barra de inyección no existe o está fuera de servicio")
        if outage_id not in branches or branches[outage_id].get("outserv", 0) == 1:
            raise ValueError("La rama de salida no existe o está fuera de servicio")
        branch = branches[outage_id]
        from_bus, to_bus = branch.get("bus_from"), branch.get("bus_to")
        if from_bus not in buses or to_bus not in buses:
            raise ValueError("LODF solo admite una rama de dos terminales")
        if _cancelled(cancel):
            result["status"] = "cancelled"
            return json_value(result)
        opts = _options(options)
        if progress:
            progress({"stage": "base", "fraction": 0.0})
        base = run_loadflow(copy.deepcopy(model), scenario=scenario, options=opts)
        payload["base"] = base
        base_status = _record_status(payload, diagnostics, "base", base)
        payload["injection_bus"] = {"id": bus_id, **_identity(buses[bus_id])}
        payload["outage_branch"] = {"id": outage_id, **_identity(branch)}
        injection_row = next((row for row in base.get("buses", []) if row.get("id") == bus_id), None)
        injection_island = next((row for row in base.get("islands", [])
                                 if row.get("id") == (injection_row or {}).get("island")), None)
        ref_id = next((row.get("reference_gen") for row in base.get("islands", [])
                       if row.get("status") == "resuelto" and bus_id in row.get("buses", [])), None)
        gens = {row["id"]: row for row in model.get("gens", [])}
        payload["compensating_generator"] = {"generator_id": ref_id, "reference_bus_id": gens.get(ref_id, {}).get("bus"), **_identity(gens.get(ref_id, {}))}
        payload["finite_difference"] = {"method": "centrada AC", "derivative_kind": "derivada AC por diferencias finitas, no PTDF DC",
                                          "step_p_mw": options.step_p_mw, "step_q_mvar": options.step_q_mvar,
                                          "half_step_p_mw": options.step_p_mw / 2.0, "half_step_q_mvar": options.step_q_mvar / 2.0,
                                          "formula": "(f(+h)-f(-h))/(2h)"}
        if (injection_row is None or injection_row.get("status") != "resuelto"
                or injection_island is None or injection_island.get("status") != "resuelto"
                or not injection_island.get("reference_gen")):
            result["status"] = "invalid_input"
            diagnostics.append(diagnostic("injection_bus_without_reference",
                "La barra de inyección no pertenece a una isla resuelta con referencia", level="error",
                object_id=buses[bus_id].get("object_id", bus_id),
                relative_path=buses[bus_id].get("relative_path", buses[bus_id].get("path"))))
            return json_value(result)
        if not base_status["ok"]:
            result["status"] = "partial"
            diagnostics.append(diagnostic("base_energized_not_converged", base_status["message"], level="error"))
            return json_value(result)
        h_p, h_q = options.step_p_mw, options.step_q_mvar
        if progress:
            progress({"stage": "h_and_h2", "fraction": .15})
        flows = {
            "p_h": _pair(model, scenario, ((bus_id, 1.0, 0.0),), h_p, opts),
            "p_h2": _pair(model, scenario, ((bus_id, 1.0, 0.0),), h_p / 2.0, opts),
            "q_h": _pair(model, scenario, ((bus_id, 0.0, 1.0),), h_q, opts),
            "q_h2": _pair(model, scenario, ((bus_id, 0.0, 1.0),), h_q / 2.0, opts),
            "transfer_h": _pair(model, scenario, ((from_bus, 1.0, 0.0), (to_bus, -1.0, 0.0)), h_p, opts),
            "transfer_h2": _pair(model, scenario, ((from_bus, 1.0, 0.0), (to_bus, -1.0, 0.0)), h_p / 2.0, opts),
        }
        regimes = {"base": _regime(base)}
        consistent = {}
        for name, pair in flows.items():
            first_status = _record_status(payload, diagnostics, name + "_plus", pair[0])
            second_status = _record_status(payload, diagnostics, name + "_minus", pair[1])
            regimes[name + "_plus"], regimes[name + "_minus"] = _regime(pair[0]), _regime(pair[1])
            consistent[name] = first_status["ok"] and second_status["ok"] and regimes[name + "_plus"] == regimes["base"] and regimes[name + "_minus"] == regimes["base"]
        payload["control_regime"] = regimes
        payload["control_regime_consistent"] = consistent
        all_regimes_consistent = all(consistent.values())
        p_evaluable = all_regimes_consistent
        q_evaluable = all_regimes_consistent
        transfer_evaluable = all_regimes_consistent
        payload["evaluable"] = {"voltage_p": p_evaluable, "voltage_q": q_evaluable,
                                "flow_p": p_evaluable, "loss_p": p_evaluable,
                                "ac_transfer": transfer_evaluable, "lodf": False,
                                "conclusions_blocked": not all_regimes_consistent}
        if not all(consistent.values()):
            result["status"] = "partial"
            diagnostics.append(diagnostic("control_regime_changed", "h o h/2 no conservó el régimen de controles del caso base", level="warning"))
        if _cancelled(cancel):
            result["status"] = "cancelled"
            return json_value(result)
        amplitude_p, amplitude_q = 1.5 * h_p, 1.5 * h_q
        if progress:
            progress({"stage": "independent_lf", "fraction": .62})
        check_p = _solve(model, scenario, ((bus_id, amplitude_p, 0.0),), opts)
        check_q = _solve(model, scenario, ((bus_id, 0.0, amplitude_q),), opts)
        check_p_status = _record_status(payload, diagnostics, "verification_p", check_p)
        check_q_status = _record_status(payload, diagnostics, "verification_q", check_q)
        regimes["verification_p"], regimes["verification_q"] = _regime(check_p), _regime(check_q)
        if not check_p_status["ok"] or not check_q_status["ok"] or regimes["verification_p"] != regimes["base"] or regimes["verification_q"] != regimes["base"]:
            result["status"] = "partial"
            diagnostics.append(diagnostic("prediction_regime_changed", "El flujo independiente no conserva el régimen de controles", level="warning"))
        base_buses = _rows(base, "buses", "u_pu")
        p_h_plus, p_h_minus = (_rows(row, "buses", "u_pu") for row in flows["p_h"])
        p_h2_plus, p_h2_minus = (_rows(row, "buses", "u_pu") for row in flows["p_h2"])
        q_h_plus, q_h_minus = (_rows(row, "buses", "u_pu") for row in flows["q_h"])
        q_h2_plus, q_h2_minus = (_rows(row, "buses", "u_pu") for row in flows["q_h2"])
        check_p_buses, check_q_buses = _rows(check_p, "buses", "u_pu"), _rows(check_q, "buses", "u_pu")
        payload["buses"] = []
        for key, base_row in sorted(base_buses.items()):
            dp_h = _central(p_h_plus.get(key, {}), p_h_minus.get(key, {}), "u_pu", h_p) if p_evaluable else None
            dp_h2 = _central(p_h2_plus.get(key, {}), p_h2_minus.get(key, {}), "u_pu", h_p / 2.0) if p_evaluable else None
            dq_h = _central(q_h_plus.get(key, {}), q_h_minus.get(key, {}), "u_pu", h_q) if q_evaluable else None
            dq_h2 = _central(q_h2_plus.get(key, {}), q_h2_minus.get(key, {}), "u_pu", h_q / 2.0) if q_evaluable else None
            actual_p = check_p_buses.get(key, {}).get("u_pu")
            actual_q = check_q_buses.get(key, {}).get("u_pu")
            predicted_p = None if dp_h2 is None or regimes["verification_p"] != regimes["base"] else dp_h2 * amplitude_p
            predicted_q = None if dq_h2 is None or regimes["verification_q"] != regimes["base"] else dq_h2 * amplitude_q
            delta_p = None if actual_p is None else actual_p - base_row["u_pu"]
            delta_q = None if actual_q is None else actual_q - base_row["u_pu"]
            payload["buses"].append({"id": key, **_identity(buses[key]), "un_kv": buses[key].get("un_kv"), "u_pu": base_row["u_pu"],
                                     "du_dp_pu_per_mw": dp_h2, "du_dq_pu_per_mvar": dq_h2,
                                     "du_dp_h_pu_per_mw": dp_h, "du_dp_h2_pu_per_mw": dp_h2,
                                     "du_dq_h_pu_per_mvar": dq_h, "du_dq_h2_pu_per_mvar": dq_h2,
                                     "du_dp_step_difference_pu_per_mw": None if dp_h is None or dp_h2 is None else abs(dp_h-dp_h2),
                                     "du_dq_step_difference_pu_per_mvar": None if dq_h is None or dq_h2 is None else abs(dq_h-dq_h2),
                                     "p_verification_amplitude_mw": amplitude_p, "p_predicted_delta_u_pu": predicted_p, "p_actual_delta_u_pu": delta_p,
                                     "p_prediction_error_pu": None if delta_p is None or predicted_p is None else delta_p-predicted_p,
                                     "p_prediction_relative_error": None if not delta_p or predicted_p is None else abs(delta_p-predicted_p)/abs(delta_p),
                                     "q_verification_amplitude_mvar": amplitude_q, "q_predicted_delta_u_pu": predicted_q, "q_actual_delta_u_pu": delta_q,
                                     "q_prediction_error_pu": None if delta_q is None or predicted_q is None else delta_q-predicted_q,
                                     "q_prediction_relative_error": None if not delta_q or predicted_q is None else abs(delta_q-predicted_q)/abs(delta_q)})
        base_branches = _rows(base, "branches", "p_from_mw")
        thp, thm = (_rows(row, "branches", "p_from_mw") for row in flows["transfer_h"])
        th2p, th2m = (_rows(row, "branches", "p_from_mw") for row in flows["transfer_h2"])
        ptdf_h = {key: _central(thp.get(key, {}), thm.get(key, {}), "p_from_mw", h_p) if transfer_evaluable else None for key in base_branches}
        ptdf_h2 = {key: _central(th2p.get(key, {}), th2m.get(key, {}), "p_from_mw", h_p / 2.0) if transfer_evaluable else None for key in base_branches}
        denominator = None if ptdf_h2.get(outage_id) is None else 1.0-ptdf_h2[outage_id]
        degenerate = denominator is None or abs(denominator) < 1e-8
        if progress:
            progress({"stage": "lodf_n1", "fraction": .80})
        n1 = run_contingencies(model, scenario, ContingencyOptions(orders=(1,), q_limits=options.q_limits), selected_ids=[outage_id], filter_reason="Verificación sensitivities LODF")
        case = n1.get("payload", {}).get("cases", [None])[0]
        case_flow = case.get("loadflow", {}) if case else {}
        island_formed = not case_flow or not energized_status(case_flow)["ok"] or len(case_flow.get("islands", [])) > len(base.get("islands", []))
        lodf_evaluable = transfer_evaluable and not degenerate and not island_formed
        payload["evaluable"]["lodf"] = lodf_evaluable
        if degenerate:
            diagnostics.append(diagnostic("lodf_denominator_degenerate", "LODF no evaluable, denominador de la derivada AC degenerado", level="warning", object_id=outage_id, relative_path=branch.get("relative_path", branch.get("path"))))
        if island_formed:
            diagnostics.append(diagnostic("lodf_outage_islanded", "LODF no evaluable, la salida N-1 forma una isla", level="warning", object_id=outage_id, relative_path=branch.get("relative_path", branch.get("path"))))
        if not lodf_evaluable:
            result["status"] = "partial"
        actual = _rows(case_flow, "branches", "p_from_mw") if lodf_evaluable else {}
        outage_flow = base_branches[outage_id]["p_from_mw"]
        payload["branches"] = []
        for key, base_row in sorted(base_branches.items()):
            lodf = (-1.0 if key == outage_id else ptdf_h2[key]/denominator) if lodf_evaluable and ptdf_h2[key] is not None else None
            predicted = 0.0 if key == outage_id and lodf_evaluable else (None if lodf is None else base_row["p_from_mw"] + lodf*outage_flow)
            actual_p = 0.0 if key == outage_id and lodf_evaluable else actual.get(key, {}).get("p_from_mw")
            error = None if predicted is None or actual_p is None else actual_p-predicted
            payload["branches"].append({"id": key, **_identity(branches.get(key, {})), "class": branches.get(key, {}).get("class"), "bus_from": base_row.get("bus_from"), "bus_to": base_row.get("bus_to"),
                                        "p_from_mw": base_row["p_from_mw"], "ac_transfer_derivative_h_mw_per_mw": ptdf_h[key], "ac_transfer_derivative_h2_mw_per_mw": ptdf_h2[key],
                                        "ac_transfer_step_difference_mw_per_mw": None if ptdf_h[key] is None or ptdf_h2[key] is None else abs(ptdf_h[key]-ptdf_h2[key]),
                                        "ptdf_mw_per_mw": ptdf_h2[key], "lodf_mw_per_mw": lodf, "predicted_post_outage_p_mw": predicted, "actual_post_outage_p_mw": actual_p,
                                        "lodf_error_mw": error, "lodf_error_relative_to_actual_flow": None if error is None or not actual_p else abs(error)/abs(actual_p)})
        error_rows = [row for row in payload["branches"] if row["lodf_error_mw"] is not None]
        material_rows = [row for row in error_rows if abs(row["actual_post_outage_p_mw"]) >= 1.0]
        worst = max(material_rows, key=lambda row: abs(row["lodf_error_mw"]), default=None)
        worst_any = max(error_rows, key=lambda row: abs(row["lodf_error_mw"]), default=None)
        payload["lodf_verification"] = {"engine": "power_machine.contingency", "contingency_outage_id": outage_id, "case_status": case.get("status") if case else None,
                                        "evaluable": lodf_evaluable, "island_formed": island_formed, "denominator": denominator, "outage_base_p_mw": outage_flow,
                                        "max_abs_error_mw": abs(worst["lodf_error_mw"]) if worst else None, "max_error_branch_id": worst["id"] if worst else None,
                                        "max_error_branch_actual_flow_mw": worst["actual_post_outage_p_mw"] if worst else None,
                                        "max_error_relative_to_actual_flow": worst["lodf_error_relative_to_actual_flow"] if worst else None,
                                        "material_flow_threshold_mw": 1.0,
                                        "max_abs_error_all_flows_mw": abs(worst_any["lodf_error_mw"]) if worst_any else None}
        loss_h = _central({"value": flows["p_h"][0].get("totals", {}).get("branch_loss_p_mw")}, {"value": flows["p_h"][1].get("totals", {}).get("branch_loss_p_mw")}, "value", h_p) if p_evaluable else None
        loss_h2 = _central({"value": flows["p_h2"][0].get("totals", {}).get("branch_loss_p_mw")}, {"value": flows["p_h2"][1].get("totals", {}).get("branch_loss_p_mw")}, "value", h_p/2.0) if p_evaluable else None
        payload["loss_sensitivity_dp_mw_per_mw"] = loss_h2
        payload["loss_sensitivity_h_mw_per_mw"] = loss_h
        payload["loss_sensitivity_h2_mw_per_mw"] = loss_h2
        payload["loss_sensitivity_step_difference_mw_per_mw"] = None if loss_h is None or loss_h2 is None else abs(loss_h-loss_h2)
        if progress:
            progress({"stage": "done", "fraction": 1.0})
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        result["status"] = "invalid_input"
        diagnostics.append(diagnostic("invalid_input", exc, level="error"))
    return json_value(result)
