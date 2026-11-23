"""Motor PV/QV que delega las ecuaciones al flujo de potencia existente."""
from __future__ import annotations

import copy
from dataclasses import asdict
import math
from pathlib import Path

from .. import elements
from ..lf_status import energized_status
from ..loadflow import _scenario, run_loadflow
from .results import diagnostic, envelope, fingerprint, json_data
from .schema import PVOptions, QVOptions

WORK_ROOT = Path(__file__).resolve().parents[2]


class UnsupportedStudy(ValueError):
    """Entrada físicamente no cubierta por el motor con los datos disponibles."""


class DispatchCapacityExceeded(ValueError):
    """El incremento solicitado supera la reserva P de participantes declarados."""


def _cancelled(cancel) -> bool:
    return bool(cancel and (cancel.is_set() if hasattr(cancel, "is_set") else cancel()))


def _identity(item: dict) -> dict:
    return {"model_id": item["id"], "object_id": item.get("object_id"),
            "relative_path": item.get("relative_path", item.get("path")),
            "name": item.get("name", item["id"])}


def _active_loads(model: dict) -> list[dict]:
    return [item for item in model.get("loads", []) if not item.get("outserv", 0)]


def _substation(item: dict, buses: dict) -> str:
    """Etiqueta disponible sin inventar una subestación ausente del modelo."""
    direct = item.get("substation") or item.get("station")
    if direct:
        return str(direct)
    bus = buses.get(item.get("bus"), {})
    direct = bus.get("substation") or bus.get("station")
    if direct:
        return str(direct)
    path = str(bus.get("relative_path", bus.get("path", item.get("relative_path", item.get("path", "")))))
    return path.split("\\")[-2] if "\\" in path else "Sin subestación exportada"


def _select_loads(model: dict, inputs: dict, *, allow_nonphysical_loads: bool = False) -> tuple[list[dict], dict]:
    loads = _active_loads(model)
    buses = {item["id"]: item for item in model.get("buses", [])}
    has_scopes = any(item.get("scope") in ("sistema_interno", "equivalente") for item in loads)
    selection = inputs.get("load_selection")
    metadata = {"scope_filter_available": has_scopes, "default_internal": selection is None,
                "selection": "sistema_interno" if selection is None else selection,
                "advanced_nonphysical_selection": allow_nonphysical_loads}
    def active_p(item):
        return elements.load_pq_mw(item)[0]
    def physical_internal(item):
        return active_p(item) > 0 and (not has_scopes or item.get("scope") == "sistema_interno")
    if selection is None:
        selected = [item for item in loads if physical_internal(item)]
    elif isinstance(selection, str):
        if selection in ("all", "internal", "sistema_interno"):
            selected = [item for item in loads if physical_internal(item)]
        elif selection in ("equivalent", "equivalente"):
            selected = [item for item in loads if item.get("scope") == "equivalente"]
        else:
            raise ValueError("load_selection debe ser all, internal, equivalent o un diccionario")
    elif isinstance(selection, dict):
        kind = selection.get("kind")
        if kind == "list":
            requested = set(selection.get("load_ids", []))
            if not requested or not all(isinstance(key, str) for key in requested):
                raise ValueError("load_ids debe contener al menos un ID")
            unknown = requested - {item["id"] for item in loads}
            if unknown:
                raise ValueError(f"Cargas seleccionadas desconocidas o fuera de servicio: {sorted(unknown)}")
            selected = [item for item in loads if item["id"] in requested]
        elif kind == "substation":
            value = selection.get("substation")
            if not isinstance(value, str) or not value.strip():
                raise ValueError("substation es obligatoria")
            selected = [item for item in loads if _substation(item, buses) == value]
        else:
            raise ValueError("kind de load_selection debe ser list o substation")
    else:
        raise ValueError("load_selection inválida")
    if not selected:
        raise ValueError("La selección no contiene cargas activas")
    nonphysical = [item for item in selected if not physical_internal(item)]
    if nonphysical and not allow_nonphysical_loads:
        ids = [item["id"] for item in nonphysical]
        raise ValueError(f"La selección contiene cargas no físicas para demanda interna: {ids}. Active allow_nonphysical_loads para un ensayo avanzado rotulado")
    metadata["nonphysical_selected_ids"] = [item["id"] for item in nonphysical]
    metadata["excluded_by_default_filter_ids"] = [item["id"] for item in loads if item not in selected and not physical_internal(item)]
    metadata["selected_scope_counts"] = {scope: sum(item.get("scope") == scope for item in selected)
                                         for scope in ("sistema_interno", "equivalente")}
    metadata["equivalent_included"] = any(item.get("scope") == "equivalente" for item in selected)
    return selected, metadata


def _set_load_pq(load: dict, p_mw: float, q_mvar: float) -> None:
    load["p_mw"], load["q_mvar"], load["scale"] = p_mw, q_mvar, 1.0
    params = load.setdefault("params", {})
    params.update(plini=p_mw, qlini=q_mvar, scale0=1.0)


def _set_gen_p(gen: dict, p_mw: float) -> None:
    """Escribe pgini conservando la convención por unidad del generador."""
    scale = float(gen.get("params", {}).get("scale0", 1.0))
    units = float(gen.get("params", {}).get("ngnum", gen.get("n_units", 1.0)))
    divisor = 1000.0 if gen.get("class") == "ElmPvsys" else 1.0
    if scale <= 0 or units <= 0:
        raise ValueError(f"Generador {gen['id']} tiene escala o número de unidades no positivo")
    gen["p_mw"] = p_mw
    gen.setdefault("params", {})["pgini"] = p_mw * divisor / (units * scale)


def _dispatch_participants(model: dict, inputs: dict) -> list[dict]:
    """Lee una lista explícita de participantes con factores y límites P en MW."""
    requested = inputs.get("generator_participants")
    if not isinstance(requested, list) or not requested:
        raise UnsupportedStudy("El reparto proportional requiere generator_participants explícito")
    generators = {item["id"]: item for item in model.get("gens", [])}
    records, used = [], set()
    for row in requested:
        if not isinstance(row, dict):
            raise ValueError("Cada participante debe ser un diccionario")
        gen_id = row.get("generator_id", row.get("id"))
        factor = row.get("participation_factor", row.get("factor"))
        if not isinstance(gen_id, str) or gen_id in used or gen_id not in generators:
            raise ValueError("Participante de generación desconocido o duplicado")
        gen = generators[gen_id]
        if gen.get("outserv", 0) or gen.get("ref"):
            raise ValueError(f"{gen_id} no es un participante activo no referencia")
        try:
            factor = float(factor)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Factor inválido para {gen_id}") from exc
        if not math.isfinite(factor) or factor < 0:
            raise ValueError(f"Factor no negativo y finito requerido para {gen_id}")
        params, units = gen.get("params", {}), gen.get("units", {})
        p_min, p_max = params.get("Pmin_uc"), params.get("Pmax_uc")
        if p_min is None or p_max is None or units.get("Pmin_uc") != "MW" or units.get("Pmax_uc") != "MW":
            raise UnsupportedStudy(f"{gen_id} no tiene límites Pmin_uc/Pmax_uc trazables en MW")
        p_min, p_max = float(p_min), float(p_max)
        if not all(math.isfinite(value) for value in (p_min, p_max)) or p_min > p_max:
            raise UnsupportedStudy(f"{gen_id} tiene límites P inválidos")
        p_now = elements.gen_pq_mw(gen)[0]
        if p_now < p_min - 1e-8 or p_now > p_max + 1e-8:
            raise UnsupportedStudy(f"{gen_id} parte fuera de sus límites P declarados")
        records.append({"generator": gen, "generator_id": gen_id, "factor": factor,
                        "p_initial_mw": p_now, "p_min_mw": p_min, "p_max_mw": p_max,
                        "reserve_up_mw": max(0.0, p_max - p_now)})
        used.add(gen_id)
    if not records or sum(item["factor"] for item in records) <= 0:
        raise ValueError("Los factores de participación deben sumar un valor positivo")
    return records


def _apply_proportional_dispatch(model: dict, inputs: dict, increase_mw: float) -> list[dict]:
    records = _dispatch_participants(model, inputs)
    if increase_mw < 0:
        raise ValueError("El incremento PV no puede ser negativo")
    remaining = float(increase_mw)
    allocation = {item["generator_id"]: 0.0 for item in records}
    eligible = [item for item in records if item["factor"] > 0 and item["reserve_up_mw"] > 1e-9]
    if remaining > sum(item["reserve_up_mw"] for item in eligible) + 1e-8:
        raise DispatchCapacityExceeded("La reserva P de los participantes no cubre el incremento solicitado")
    while remaining > 1e-9:
        factor_sum = sum(item["factor"] for item in eligible)
        if factor_sum <= 0:
            raise DispatchCapacityExceeded("No queda factor de participación con reserva P")
        granted = 0.0
        next_eligible = []
        for item in eligible:
            available = item["reserve_up_mw"] - allocation[item["generator_id"]]
            share = remaining * item["factor"] / factor_sum
            value = min(share, available)
            allocation[item["generator_id"]] += value
            granted += value
            if available - value > 1e-9:
                next_eligible.append(item)
        remaining -= granted
        if granted <= 1e-12:
            raise DispatchCapacityExceeded("El reparto proporcional no pudo asignar reserva P")
        eligible = next_eligible
    index = {item["id"]: item for item in model.get("gens", [])}
    output = []
    for item in records:
        assigned = allocation[item["generator_id"]]
        _set_gen_p(index[item["generator_id"]], item["p_initial_mw"] + assigned)
        output.append({key: value for key, value in item.items() if key != "generator"} | {"assigned_delta_p_mw": assigned,
                                                                                              "p_target_mw": item["p_initial_mw"] + assigned})
    return output


def _monitored_ids(model: dict, inputs: dict) -> list[str]:
    buses = {item["id"]: item for item in model.get("buses", []) if not item.get("outserv", 0)}
    requested = inputs.get("monitored_bus_ids")
    if requested is None:
        internal = [item_id for item_id, item in buses.items() if item.get("scope") == "sistema_interno"]
        return sorted(internal or buses)
    if not isinstance(requested, list) or not requested or not all(isinstance(key, str) for key in requested):
        raise ValueError("monitored_bus_ids debe ser una lista no vacía de IDs")
    unknown = set(requested) - set(buses)
    if unknown:
        raise ValueError(f"Barras monitorizadas desconocidas o fuera de servicio: {sorted(unknown)}")
    return list(dict.fromkeys(requested))


def _boundary_interchange(lf: dict, model: dict) -> dict:
    """P y Q positivos saliendo de boundary hacia el equivalente, por circuito."""
    rows = {row["id"]: row for row in lf.get("branches", [])}
    links, totals = [], [0.0, 0.0]
    available = False
    for entry in model.get("interface", []):
        row, host = rows.get(entry.get("link_id")), entry.get("host_bus")
        if row and row.get("bus_from") == host:
            p_value, q_value = row.get("p_from_mw"), row.get("q_from_mvar")
        elif row and row.get("bus_to") == host:
            p_value, q_value = row.get("p_to_mw"), row.get("q_to_mvar")
        else:
            p_value = q_value = None
        if isinstance(p_value, (int, float)) and isinstance(q_value, (int, float)):
            totals[0] += p_value
            totals[1] += q_value
            available = True
        links.append({"circuit": entry.get("circuit"), "link_id": entry.get("link_id"),
                      "host_bus_id": host, "p_out_of_boundary_mw": p_value,
                      "q_out_of_boundary_mvar": q_value, "status": row.get("status") if row else "unavailable"})
    return {"p_out_of_boundary_mw": totals[0] if available else None,
            "q_out_of_boundary_mvar": totals[1] if available else None,
            "links": links,
            "sign_convention": "Positivo saliendo de la frontera hacia el equivalente boundary equivalent"}


def _load_breakdown(lf: dict, selected_ids: set[str]) -> dict:
    selected_p = selected_q = other_p = other_q = 0.0
    selected_found, other_found = [], []
    for row in lf.get("loads", []):
        if row.get("id") in selected_ids:
            selected_p += row.get("p_mw", 0.0)
            selected_q += row.get("q_mvar", 0.0)
            selected_found.append(row["id"])
        else:
            other_p += row.get("p_mw", 0.0)
            other_q += row.get("q_mvar", 0.0)
            other_found.append(row["id"])
    return {"selected_p_mw": selected_p, "selected_q_mvar": selected_q,
            "nonselected_p_mw": other_p, "nonselected_q_mvar": other_q,
            "selected_load_ids_solved": selected_found,
            "nonselected_load_ids_solved": other_found}


def _point_from_lf(lf: dict, monitored_ids: list[str], selected_ids: set[str], model: dict, *, increase_mw: float, load_factor: float,
                   converged: bool | None = None) -> dict:
    island_status = energized_status(lf)
    bus_rows = {row["id"]: row for row in lf.get("buses", [])}
    model_buses = {row["id"]: row for row in model.get("buses", [])}
    monitored = []
    for bus_id in monitored_ids:
        row = bus_rows.get(bus_id)
        if row is None:
            continue
        source = model_buses.get(bus_id, {})
        monitored.append({"bus_id": bus_id, "object_id": source.get("object_id"),
                          "relative_path": source.get("relative_path", source.get("path")),
                          "name": source.get("name", row.get("name", bus_id)), "scope": source.get("scope"),
                          "un_kv": source.get("un_kv"), "u_pu": row.get("u_pu"), "u_kv": row.get("u_kv"),
                          "status": row.get("status")})
    return {"increase_mw": float(increase_mw), "load_factor": float(load_factor),
            "total_load_p_mw": lf.get("totals", {}).get("load_p_mw"),
            "total_load_q_mvar": lf.get("totals", {}).get("load_q_mvar"),
            "load_breakdown": _load_breakdown(lf, selected_ids),
            "boundary": _boundary_interchange(lf, model),
            "converged": island_status["ok"] if converged is None else bool(converged),
            "loadflow_converged": bool(lf.get("converged")), "iterations": lf.get("iterations"),
            "energized_status": island_status,
            "max_mismatch_mva": lf.get("max_mismatch_mva"),
            "jacobian_condition": lf.get("jacobian_condition"), "monitored_buses": monitored,
            "q_limited_generator_ids": [row["id"] for row in lf.get("gens", []) if row.get("q_limit")],
            "warnings": list(lf.get("warnings", []))}


def _target_resolved(lf: dict, loads: list[dict]) -> bool:
    statuses = {row["id"]: row.get("status") for row in lf.get("buses", [])}
    return energized_status(lf)["ok"] and all(statuses.get(item["bus"]) == "resuelto" for item in loads)


def _warm_start_solve(model: dict, last_loadflow: dict, q_limits: bool) -> dict:
    """Reintenta con tensiones previas sin cambiar el estado físico del modelo."""
    active_bus = next((item["id"] for item in model.get("buses", []) if not item.get("outserv", 0)), None)
    if active_bus is None:
        raise ValueError("No hay barras activas para un reintento cálido")
    return run_loadflow(model, scenario={"outserv": {active_bus: 0}},
                        options={"q_limits": q_limits, "warm_start": True, "base_result": last_loadflow})


def _pv_model(base_model: dict, scalable_ids: set[str], base_pq: dict[str, tuple[float, float]],
              increase_mw: float, options: PVOptions, inputs: dict | None = None) -> dict:
    model = copy.deepcopy(base_model)
    weighted = sum(base_pq[key][0] for key in scalable_ids)
    if weighted <= 0:
        raise ValueError("Las cargas escalables no tienen potencia activa positiva para una curva PV")
    for load in model["loads"]:
        if load["id"] not in scalable_ids:
            continue
        p0, q0 = base_pq[load["id"]]
        p_mw = p0 + increase_mw * p0 / weighted
        if options.power_factor is None:
            q_mvar = q0 * (p_mw / p0) if p0 else q0
        else:
            q_mvar = math.copysign(abs(p_mw) * math.tan(math.acos(abs(options.power_factor))), options.power_factor)
        _set_load_pq(load, p_mw, q_mvar)
    if options.generator_dispatch == "proportional" and increase_mw:
        _apply_proportional_dispatch(model, inputs or {}, increase_mw)
    return model


def _margin_summary(payload: dict) -> dict:
    """Resume una corrida PV para comparar sensibilidades de selección."""
    margin = payload.get("loadability_margin", {})
    monitored = payload.get("points", [{}])[-1].get("monitored_buses", [])
    valid = [row for row in monitored if isinstance(row.get("u_pu"), (int, float)) and row.get("status") == "resuelto"]
    critical = min(valid, key=lambda row: row["u_pu"]) if valid else None
    return {"status": payload.get("status"),
            "base_selected_p_mw": margin.get("base_selected_p_mw"),
            "last_converged_increase_mw": margin.get("last_converged_increase_mw"),
            "last_converged_percent": margin.get("last_converged_percent"),
            "first_failure": payload.get("search_interval", {}).get("first_failure"),
            "active_upper_failure": payload.get("search_interval", {}).get("active_upper_failure"),
            "critical_bus": critical,
            "maximum_increase_reached": margin.get("maximum_increase_reached")}


def _pv_curve(model: dict, inputs: dict, options: PVOptions, *, cancel=None, progress=None,
              include_margin_sensitivity: bool = True) -> tuple[dict, list[dict], str]:
    selected, selection_metadata = _select_loads(model, inputs, allow_nonphysical_loads=options.allow_nonphysical_loads)
    dispatch_plan = _dispatch_participants(model, inputs) if options.generator_dispatch == "proportional" else []
    monitored_ids = _monitored_ids(model, inputs)
    selected_ids = {item["id"] for item in selected}
    original_pq = {item["id"]: elements.load_pq_mw(item) for item in selected}
    # Incluso en el ensayo avanzado, las cargas equivalentes o no consumidoras
    # se informan pero nunca reciben el incremento de una curva de demanda.
    nonphysical_ids = set(selection_metadata["nonphysical_selected_ids"])
    scalable_ids = {item_id for item_id, (p_mw, _) in original_pq.items()
                    if p_mw > 0 and item_id not in nonphysical_ids}
    # Con FP pedido, el incremento cero debe tener exactamente el mismo perfil
    # que el resto de la curva. Así todos los puntos son comparables.
    materialized_base = _pv_model(model, scalable_ids, original_pq, 0.0, options, inputs)
    base_lf = run_loadflow(materialized_base, options={"q_limits": options.q_limits})
    base_status = energized_status(base_lf)
    if not base_status["ok"]:
        raise ValueError(base_status["message"])
    base_bus_status = {row["id"]: row.get("status") for row in base_lf.get("buses", [])}
    unresolved = [item for item in selected if base_bus_status.get(item.get("bus")) != "resuelto"]
    selected = [item for item in selected if item not in unresolved]
    if not selected:
        raise ValueError("Ninguna carga seleccionada está en una isla resuelta del flujo base")
    selected_ids = {item["id"] for item in selected}
    selection_metadata["selected_scope_counts"] = {scope: sum(item.get("scope") == scope for item in selected)
                                                  for scope in ("sistema_interno", "equivalente")}
    selection_metadata["excluded_unresolved_load_ids"] = [item["id"] for item in unresolved]
    scalable_ids &= selected_ids
    if not scalable_ids:
        raise ValueError("No queda ninguna carga positiva y energizada para escalar")
    selection_metadata["scalable_load_ids"] = sorted(scalable_ids)
    selection_metadata["frozen_nonpositive_load_ids"] = sorted(selected_ids - scalable_ids)
    base_pq = {item["id"]: original_pq[item["id"]] for item in selected}
    points = [_point_from_lf(base_lf, monitored_ids, selected_ids, model,
                             increase_mw=0.0, load_factor=1.0, converged=True)]
    step, increase, failed_at = options.initial_step_mw, 0.0, None
    last_lf, failures, recovered_flat_failures = base_lf, [], []
    attempts = 0
    while increase < options.maximum_increase_mw and len(points) < options.max_points:
        if _cancelled(cancel):
            return {"points": points, "cancelled": True}, [], "cancelled"
        candidate = min(increase + step, options.maximum_increase_mw)
        attempts += 1
        try:
            candidate_model = _pv_model(model, scalable_ids, base_pq, candidate, options, inputs)
        except DispatchCapacityExceeded as exc:
            failed_at = candidate
            failures.append({"increase_mw": candidate, "step_mw": step, "kind": "dispatch_capacity", "message": str(exc)})
            break
        if options.initialization_mode == "warm_from_last":
            solved = _warm_start_solve(candidate_model, last_lf, options.q_limits)
        else:
            solved = run_loadflow(candidate_model, options={"q_limits": options.q_limits})
        target_converged = _target_resolved(solved, selected)
        warm_solved = None
        if not target_converged and options.initialization_mode == "flat":
            warm_solved = _warm_start_solve(candidate_model, last_lf, options.q_limits)
            target_converged = _target_resolved(warm_solved, selected)
            if target_converged:
                recovered_flat_failures.append({"increase_mw": candidate,
                                                "flat_warnings": list(solved.get("warnings", [])),
                                                "warm_warnings": list(warm_solved.get("warnings", []))})
                solved = warm_solved
        elif not target_converged:
            warm_solved = run_loadflow(candidate_model, options={"q_limits": options.q_limits})
            target_converged = _target_resolved(warm_solved, selected)
            if target_converged:
                recovered_flat_failures.append({"increase_mw": candidate,
                                                "warm_warnings": list(solved.get("warnings", [])),
                                                "flat_warnings": list(warm_solved.get("warnings", []))})
                solved = warm_solved
        if target_converged:
            increase = candidate
            total_base = sum(base_pq[item_id][0] for item_id in scalable_ids)
            points.append(_point_from_lf(solved, monitored_ids, selected_ids, model, increase_mw=increase,
                                         load_factor=(total_base + increase) / total_base, converged=True))
            last_lf = solved
            step = min(step * 1.25, max(options.initial_step_mw, options.maximum_increase_mw / 4))
            if progress:
                progress({"phase": "pv", "completed": len(points) - 1, "total": options.max_points,
                          "fraction": min(1.0, increase / options.maximum_increase_mw) if options.maximum_increase_mw else 1.0})
        else:
            failed_at = candidate
            failures.append({"increase_mw": candidate, "step_mw": step,
                             "flat_energized_ok": energized_status(solved)["ok"],
                             "warm_energized_ok": bool(warm_solved and energized_status(warm_solved)["ok"]),
                             "flat_reason": [row.get("reason") for row in solved.get("islands", []) if row.get("status") == "no_convergente"],
                             "warm_reason": [row.get("reason") for row in (warm_solved or {}).get("islands", []) if row.get("status") == "no_convergente"]})
            step /= 2.0
            if step < options.minimum_step_mw:
                break
    last = points[-1]
    total_base = sum(base_pq[item_id][0] for item_id in scalable_ids)
    capacity_failure = next((row for row in failures if row.get("kind") == "dispatch_capacity"), None)
    collapse = None
    if failed_at is not None and capacity_failure is None:
        collapse = {"method": "non_convergence_bracket", "lower_increase_mw": last["increase_mw"],
                    "upper_increase_mw": failed_at, "estimated_increase_mw": (last["increase_mw"] + failed_at) / 2,
                    "estimated_total_load_mw": last["total_load_p_mw"] + (failed_at - last["increase_mw"]) / 2,
                    "message": "Estimación por no convergencia de Newton, sin continuación predictor-corrector"}
    last_converged = {"increase_mw": last["increase_mw"], "total_load_p_mw": last["total_load_p_mw"]}
    first_failure = failures[0] if failures else None
    payload = {"curve_type": "PV", "selected_loads": [_identity(item) for item in selected],
               "excluded_unresolved_loads": [_identity(item) for item in unresolved],
               "selection": selection_metadata, "monitored_bus_ids": monitored_ids, "points": points, "attempts": attempts,
               "initialization_mode": options.initialization_mode,
               "demand_base": points[0]["load_breakdown"],
               "collapse_estimate": collapse,
               "search_interval": {"last_converged": last_converged, "first_failure": first_failure,
                                   "active_upper_failure": failures[-1] if failures else None,
                                   "minimum_step_mw": options.minimum_step_mw},
               "failure_assessment": {"classification": "dispatch_capacity_reached" if capacity_failure else "no_failure_observed" if not failures else "nonconvergence_persists_after_warm_start_and_step_reduction",
                                      "physical_limit_confirmed": False,
                                      "message": "El límite de despacho o una no convergencia no confirma un límite físico ni un punto nariz exacto.",
                                      "flat_failures_recovered_by_warm_start": recovered_flat_failures},
               "dispatch": {"mode": options.generator_dispatch,
                            "reference_hypothesis": options.generator_dispatch == "reference",
                            "participants": [{key: value for key, value in row.items() if key != "generator"} for row in dispatch_plan],
                            "operational_limit": capacity_failure},
               "continuation": {"available": False, "method": "not_implemented",
                                "reason": "run_loadflow solo corrige un parámetro de carga fijo y no expone una restricción aumentada ni el Jacobiano público necesarios para predictor-corrector de longitud de arco. Duplicar internals o editar loadflow.py excede este estudio."},
               "loadability_margin": {"base_selected_p_mw": total_base, "last_converged_increase_mw": last["increase_mw"],
                                       "last_converged_percent": 100 * last["increase_mw"] / total_base,
                                       "point_limit_reached": len(points) >= options.max_points,
                                       "maximum_increase_reached": increase >= options.maximum_increase_mw}}
    diagnostics = []
    status = "partial" if capacity_failure else "completed"
    payload["status"] = status
    if base_status["n_deenergized"]:
        diagnostics.append(diagnostic("deenergized_islands", base_status["message"], level="warning"))
    if unresolved:
        diagnostics.append(diagnostic("deenergized_loads_excluded", "Se excluyeron cargas de islas sin referencia del escalamiento PV", level="warning"))
    if selection_metadata["equivalent_included"]:
        diagnostics.append(diagnostic("equivalent_loads_selected", "La selección incluye cargas del equivalente boundary equivalent por solicitud explícita", level="warning"))
    if selection_metadata["nonphysical_selected_ids"]:
        diagnostics.append(diagnostic("nonphysical_loads_not_scaled", "Las cargas equivalentes o con P no positiva del ensayo avanzado se conservaron sin escalar", level="warning"))
    if capacity_failure:
        diagnostics.append(diagnostic("dispatch_capacity", capacity_failure["message"], level="warning"))
    if collapse is None and increase < options.maximum_increase_mw and len(points) >= options.max_points:
        diagnostics.append(diagnostic("point_limit_reached", "Se alcanzó max_points antes del límite de carga", level="warning"))
    if options.power_factor is not None:
        payload["power_factor_profile"] = {"requested": options.power_factor,
                                            "base_materialized": True,
                                            "message": "El punto base y los incrementos usan el factor de potencia solicitado."}
    else:
        payload["power_factor_profile"] = {"requested": None, "base_materialized": True,
                                            "message": "Cada carga conserva su factor de potencia base en toda la curva."}
    if include_margin_sensitivity and selection_metadata["scope_filter_available"] and not selection_metadata["equivalent_included"]:
        bus_index = {item["id"]: item for item in model.get("buses", [])}
        valid = [row for row in last.get("monitored_buses", [])
                 if isinstance(row.get("u_pu"), (int, float)) and row.get("status") == "resuelto"]
        critical = min(valid, key=lambda row: row["u_pu"]) if valid else None
        sensitivity = {"all_internal": _margin_summary(payload),
                       "critical_substation": {"status": "not_available"}}
        if critical:
            station = _substation(bus_index.get(critical["bus_id"], {}), bus_index)
            nested_inputs = dict(inputs)
            nested_inputs["load_selection"] = {"kind": "substation", "substation": station}
            nested_inputs.pop("monitored_bus_ids", None)
            try:
                nested, nested_diagnostics, nested_status = _pv_curve(
                    model, nested_inputs, options, cancel=cancel, progress=None,
                    include_margin_sensitivity=False)
                nested["status"] = nested_status
                sensitivity["critical_substation"] = {"substation": station,
                                                       **_margin_summary(nested)}
                diagnostics.extend(nested_diagnostics)
            except (ValueError, UnsupportedStudy) as exc:
                sensitivity["critical_substation"] = {"substation": station, "status": "invalid_input",
                                                       "message": str(exc)}
        payload["margin_sensitivity"] = sensitivity
    return payload, diagnostics, status


def _qv_values(options: QVOptions) -> list[float]:
    direction = 1.0 if options.voltage_end_pu > options.voltage_start_pu else -1.0
    values, current = [], options.voltage_start_pu
    while (direction > 0 and current < options.voltage_end_pu - 1e-12) or (direction < 0 and current > options.voltage_end_pu + 1e-12):
        values.append(round(current, 12))
        current += direction * options.voltage_step_pu
    values.append(options.voltage_end_pu)
    return values


def _qv_model(base_model: dict, bus_id: str, voltage_set_pu: float) -> tuple[dict, str]:
    model = copy.deepcopy(base_model)
    if any(item.get("ref") and item.get("bus") == bus_id for item in model.get("gens", [])):
        raise ValueError("La barra QV es referencia angular, no admite una fuente ficticia independiente")
    for gen in model.get("gens", []):
        if gen.get("bus") == bus_id and gen.get("mode") == "constv" and not gen.get("ref"):
            gen["mode"] = "constq"
    source_id = "__pvqv_fictitious_reactive_source__"
    model.setdefault("gens", []).insert(0, {"id": source_id, "object_id": None, "relative_path": None,
        "name": "Fuente ficticia QV", "class": "ElmGenstat", "bus": bus_id, "outserv": 0,
        "ref": False, "mode": "constv", "v_set_pu": voltage_set_pu, "p_mw": 0.0,
        "q_mvar": 0.0, "s_rated_mva": 1.0,
        "params": {"pgini": 0.0, "qgini": 0.0, "ngnum": 1.0, "scale0": 1.0,
                   "sgn": 1e12, "q_min": -1e6, "q_max": 1e6}})
    return model, source_id


def _qv_curve(model: dict, inputs: dict, options: QVOptions, *, cancel=None, progress=None) -> tuple[dict, list[dict], str]:
    bus_id = inputs.get("qv_bus_id")
    buses = {item["id"]: item for item in model.get("buses", []) if not item.get("outserv", 0)}
    if bus_id not in buses:
        raise ValueError("qv_bus_id debe identificar una barra activa")
    points, failed = [], []
    deenergized_statuses = []
    reference_id = next((item["id"] for item in model.get("gens", []) if item.get("ref")), None)
    values = _qv_values(options)
    for index, setpoint in enumerate(values):
        if _cancelled(cancel):
            return {"curve_type": "QV", "qv_bus": _identity(buses[bus_id]), "points": points, "cancelled": True}, [], "cancelled"
        work, source_id = _qv_model(model, bus_id, setpoint)
        enforce_real_limits = options.reactive_limit_mode == "active_generator_limits"
        lf = run_loadflow(work, options={"q_limits": enforce_real_limits})
        source = next((row for row in lf.get("gens", []) if row["id"] == source_id), None)
        actual_voltage = next((item.get("u_pu") for item in lf.get("buses", []) if item["id"] == bus_id), None)
        island_status = energized_status(lf)
        target_converged = island_status["ok"] and actual_voltage is not None and source is not None and source.get("q_mvar") is not None
        slack = next((item for item in lf.get("gens", []) if item["id"] == reference_id), None)
        q_value = source.get("q_mvar") if source else None
        source_within_limits = (q_value is not None and (options.source_q_min_mvar is None or
                                options.source_q_min_mvar <= q_value <= options.source_q_max_mvar))
        if island_status["n_deenergized"]:
            deenergized_statuses.append(island_status)
        row = {"voltage_set_pu": setpoint, "voltage_actual_pu": actual_voltage,
               "q_source_mvar": q_value, "converged": target_converged,
               "loadflow_converged": bool(lf.get("converged")),
               "energized_status": island_status,
               "slack_generator_id": reference_id, "slack_p_mw": slack.get("p_mw") if slack else None,
               "slack_q_mvar": slack.get("q_mvar") if slack else None,
               "source_within_configured_q_limits": source_within_limits,
               "q_limited_generator_ids": [item["id"] for item in lf.get("gens", []) if item.get("q_limit")],
               "iterations": lf.get("iterations"), "max_mismatch_mva": lf.get("max_mismatch_mva"),
               "jacobian_condition": lf.get("jacobian_condition"), "warnings": list(lf.get("warnings", []))}
        if row["converged"] and row["q_source_mvar"] is not None:
            points.append(row)
        else:
            failed.append(row)
        if progress:
            progress({"phase": "qv", "completed": index + 1, "total": len(values), "fraction": (index + 1) / len(values)})
    if not points:
        raise ValueError("Ningún punto QV convergió")
    q_values = [row["q_source_mvar"] for row in points]
    near_zero = min(points, key=lambda row: abs(row["q_source_mvar"]))
    limit_exceeded = [row for row in points if not row["source_within_configured_q_limits"]]
    payload = {"curve_type": "QV", "qv_bus": _identity(buses[bus_id]), "points": points, "failed_points": failed,
               "reactive_limit_mode": options.reactive_limit_mode,
               "reactive_source_limits": {"minimum_mvar": options.source_q_min_mvar, "maximum_mvar": options.source_q_max_mvar,
                                           "enforced_in_solver": False,
                                           "message": "Los límites configurados califican puntos requeridos. La fuente ficticia se resuelve sin saturación."},
               "real_generator_limits": {"enforced_in_solver": options.reactive_limit_mode == "active_generator_limits",
                                           "message": "Límites de generadores reales activos" if options.reactive_limit_mode == "active_generator_limits" else "Modo idealizado, límites Q de generadores reales desactivados"},
               "reactive_margin": {"minimum_q_source_mvar": min(q_values), "maximum_q_source_mvar": max(q_values),
                                   "absorption_margin_mvar": max(0.0, -min(q_values)),
                                   "injection_margin_mvar": max(0.0, max(q_values)),
                                   "nearest_zero_q_point": near_zero,
                                   "definition": "Rango de la fuente ficticia en el barrido solicitado, no límite de un equipo real"}}
    diagnostics = [diagnostic("fictitious_source", "La fuente QV es ficticia y no representa la capacidad de un equipo instalado", level="warning")]
    if limit_exceeded:
        diagnostics.append(diagnostic("qv_source_limit_exceeded", "Uno o más puntos requieren Q fuera de los límites configurados", level="warning"))
    if deenergized_statuses:
        diagnostics.append(diagnostic("deenergized_islands", deenergized_statuses[0]["message"], level="warning"))
    return payload, diagnostics, "partial" if failed or limit_exceeded else "completed"


def run_voltage_stability(model: dict, scenario: dict | None, inputs: dict, options: dict, *, cancel=None, progress=None, study=None) -> dict:
    """Ejecuta una curva PV o QV en una copia materializada del modelo."""
    if not isinstance(model, dict) or not isinstance(inputs, dict) or not isinstance(options, dict):
        raise ValueError("model, inputs y options deben ser diccionarios")
    scenario = scenario or {}
    if not isinstance(scenario, dict):
        raise ValueError("scenario debe ser un diccionario")
    effective = _scenario(copy.deepcopy(model), copy.deepcopy(scenario))
    kind = inputs.get("curve_type")
    option_object = PVOptions(**options) if kind == "PV" else QVOptions(**options) if kind == "QV" else None
    if option_object is None:
        raise ValueError("curve_type debe ser PV o QV")
    result = envelope(model, study or {"scenario": scenario}, {"curve_type": kind, **asdict(option_object)})
    result["provenance"]["inputs"] = [{"path": None, "kind": "model_snapshot", "sha256": result["model_sha256"]},
                                        {"path": None, "kind": "scenario_snapshot", "sha256": fingerprint(scenario)}]
    try:
        if kind == "PV":
            payload, diagnostics, status = _pv_curve(effective, inputs, option_object, cancel=cancel, progress=progress)
        else:
            payload, diagnostics, status = _qv_curve(effective, inputs, option_object, cancel=cancel, progress=progress)
        result["payload"] = payload
        result["diagnostics"].extend(diagnostics)
        result["status"] = status
        if any("Límite Q no disponible" in warning for point in payload.get("points", []) for warning in point.get("warnings", [])):
            result["status"] = "partial"
            result["diagnostics"].append(diagnostic("q_limits_missing", "Faltan límites Q en uno o más controles PV", level="warning"))
    except UnsupportedStudy as exc:
        result["status"] = "unsupported"
        result["diagnostics"].append(diagnostic("unsupported", exc, level="error"))
    except (ValueError, KeyError, TypeError, ArithmeticError) as exc:
        result["status"] = "invalid_input"
        result["diagnostics"].append(diagnostic("invalid_input", exc, level="error"))
    except Exception as exc:
        result["status"] = "error"
        result["diagnostics"].append(diagnostic("solver_failure", f"{type(exc).__name__}: {exc}", level="error"))
    return json_data(result)


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    """Entrada serializable para el trabajador de la interfaz."""
    fallback = envelope({}, {}, {})
    try:
        if not isinstance(request, dict):
            raise ValueError("request debe ser un diccionario")
        if not str(request.get("schema")).endswith("-voltage-stability-request-v1") or request.get("module") != "voltage_stability":
            raise ValueError("Esquema o módulo de request incompatible")
        root = Path(request["root"]).resolve()
        if root == Path(root.anchor):
            raise ValueError("La raíz debe ser una carpeta de trabajo")
        snapshot = copy.deepcopy(request["snapshot"])
        model, study = snapshot["model"], snapshot["study"]
        hashes = snapshot.get("fingerprints", {})
        if not isinstance(model, dict) or not isinstance(study, dict) or not isinstance(hashes, dict):
            raise ValueError("Snapshot incompatible")
        if hashes.get("model_sha256") != fingerprint(model) or hashes.get("study_sha256") != fingerprint(study):
            raise ValueError("Fingerprint incompatible")
        return run_voltage_stability(model, study.get("scenario", {}), request.get("input", {}), request.get("options", {}),
                                     cancel=cancel, progress=progress, study=study)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        fallback["status"] = "invalid_input"
        fallback["model_sha256"] = None
        fallback["diagnostics"].append(diagnostic("invalid_request", exc, level="error"))
        return json_data(fallback)
