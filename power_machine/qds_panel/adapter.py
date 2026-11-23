"""Adaptación segura de QSS al contrato de trabajos de la interfaz.

No replica las ecuaciones de flujo. Reutiliza el ejecutor QSS por cada
instante para poder informar avance y detenerse entre flujos de potencia.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import time
import uuid
from datetime import datetime
from pathlib import Path
from power_machine.resources import output_root, confined_output

from power_machine.benchmarks.ieee39_pf import LDF_OPTIONS
from power_machine.lf_status import energized_status
from power_machine.loadflow import run_loadflow
from power_machine.qss import Series, read_series
from power_machine.qss.dispatch import dispatch_base, plan_dispatch
from power_machine.qss.report import write_report


WORK_ROOT = Path(__file__).resolve().parents[2]
REQUEST_SCHEMA = "pm-parity-request-v1"
RESULT_SCHEMA = "pm-parity-result-v1"
MODULE = "qds_panel"


class UnsupportedProfile(ValueError):
    """El perfil existe, pero no es compatible con el modelo activo."""


def fingerprint(value: object) -> str:
    """Devuelve SHA256 de una estructura JSON finita."""
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _diagnostic(code, message, *, level="error"):
    return {"code": code, "level": level, "message": str(message),
            "object_id": None, "relative_path": None}


def _envelope(model, study):
    return {
        "schema": RESULT_SCHEMA,
        "module": MODULE,
        "run_id": "qds_" + uuid.uuid4().hex,
        "model_sha256": fingerprint(model),
        "study_sha256": fingerprint(study),
        "status": "completed",
        "units": {
            "time": "s", "voltage": "p.u.", "voltage_kv": "kV",
            "active_power": "MW", "reactive_power": "Mvar",
            "current": "kA", "loading": "%", "energy": "MWh",
        },
        "provenance": {
            "pf_comparison": "sin validar contra PF",
            "calculation": "QSS por flujos de potencia consecutivos, sin cambio topológico",
        },
        "diagnostics": [],
        "payload": {},
    }


def _cancelled(cancel) -> bool:
    return bool(cancel.is_set()) if hasattr(cancel, "is_set") else bool(cancel and cancel())


def _confined(root: Path, relative_path, *, label: str) -> Path:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError(f"{label} debe ser una ruta relativa no vacía")
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root):
        raise ValueError(f"{label} está fuera de la carpeta de trabajo")
    return candidate


def _read_profile(root: Path, model: dict, inputs: dict):
    """Obtiene serie verificada y limita paso y horizonte sin interpolar."""
    profile = inputs.get("profile", {})
    if not isinstance(profile, dict):
        raise ValueError("profile debe ser un diccionario")
    kind = profile.get("kind", "builtin_ieee39")
    if kind == "builtin_ieee39":
        raise UnsupportedProfile("Este perfil requiere un manifiesto CSV del usuario con kind=csv_manifest")
        name = profile.get("name", "S1_diaria_15min")
        label = "Perfil IEEE 39 sintético"
    elif kind == "csv_manifest":
        model_path = _confined(root, profile.get("model_path", "model.json"), label="model_path")
        manifest_path = _confined(root, profile.get("manifest_path"), label="manifest_path")
        name = profile.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("name de la serie CSV es obligatorio")
        label = "Perfil CSV de usuario"
    else:
        raise ValueError("Tipo de perfil no soportado")
    if not model_path.is_file() or not manifest_path.is_file():
        raise ValueError("Modelo o manifiesto de perfil no disponible")
    source_model, series = read_series(model_path, manifest_path, name,
                                       default_factor=profile.get("default_factor"))
    if fingerprint(source_model) != fingerprint(model):
        raise UnsupportedProfile("El perfil verificado no es compatible con el modelo activo")
    if len(series.steps) < 1:
        raise ValueError("La serie no tiene instantes")
    configuration = inputs.get("configuration", {})
    if not isinstance(configuration, dict):
        raise ValueError("configuration debe ser un diccionario")
    requested_step_s = configuration.get("step_s")
    base_step_s = (series.steps[1].t_s - series.steps[0].t_s
                   if len(series.steps) > 1 else None)
    stride = 1
    if requested_step_s is not None:
        requested_step_s = float(requested_step_s)
        if not math.isfinite(requested_step_s) or requested_step_s <= 0:
            raise ValueError("step_s debe ser finito y positivo")
        if base_step_s is None:
            if not math.isclose(requested_step_s, 0.0, abs_tol=0.0):
                raise ValueError("La serie de un instante no permite configurar step_s")
        else:
            ratio = requested_step_s / base_step_s
            stride = round(ratio)
            if stride < 1 or not math.isclose(ratio, stride, rel_tol=0.0, abs_tol=1e-9):
                raise ValueError("step_s debe ser un múltiplo entero del paso del perfil, no se interpola")
    steps = list(series.steps[::stride])
    horizon_s = configuration.get("horizon_s")
    if horizon_s is not None:
        horizon_s = float(horizon_s)
        if not math.isfinite(horizon_s) or horizon_s < 0:
            raise ValueError("horizon_s debe ser finito y no negativo")
        start_s = steps[0].t_s
        steps = [step for step in steps if step.t_s <= start_s + horizon_s + 1e-9]
    if not steps:
        raise ValueError("La combinación de paso y horizonte no contiene instantes")
    selected = Series(series.name, tuple(steps), {
        **series.provenance,
        "profile_kind": kind,
        "profile_label": label,
        "requested_step_s": requested_step_s,
        "requested_horizon_s": horizon_s,
        "source_step_s": base_step_s,
        "timestamp_zone": _timestamp_zone(steps),
    })
    return source_model, selected, configuration


def _timestamp_zone(steps) -> str:
    """Declara la zona presente en los timestamps, sin inferir una ausente."""
    offsets = set()
    for step in steps:
        parsed = datetime.fromisoformat(step.timestamp_iso)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return "no declarada en perfil"
        offsets.add(parsed.utcoffset().total_seconds())
    if len(offsets) == 1:
        offset_s = offsets.pop()
        sign = "+" if offset_s >= 0 else "-"
        total_minutes = int(abs(offset_s) // 60)
        return f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
    return "varía según timestamp del perfil"


def _with_traceability(model: dict):
    """Completa aliases de trazabilidad solo desde identificadores ya exportados."""
    adapted = copy.deepcopy(model)
    aliases = 0
    for group in ("buses", "branches", "branches3", "switches", "loads", "gens", "shunts"):
        for item in adapted.get(group, []):
            if not item.get("object_id"):
                item["object_id"] = item["id"]
                aliases += 1
            if not item.get("relative_path"):
                path = item.get("path")
                if not isinstance(path, str) or not path:
                    raise ValueError(f"Falta relative_path de trazabilidad: {item['id']}")
                item["relative_path"] = path
                aliases += 1
    return adapted, aliases


def _rating_ka(branch: dict):
    """Obtiene corriente nominal [kA] con conversión explícita de unidad."""
    for source, units in ((branch.get("type_params", {}), branch.get("type_units", {})),
                          (branch.get("params", {}), branch.get("units", {}))):
        for key in ("sline", "i_rated_ka", "inom", "Inom"):
            value = source.get(key)
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
                continue
            unit = str(units.get(key, "")).strip().lower().replace(" ", "")
            if unit in ("ka", "kamps", "kiloampere", "kiloamperes"):
                current_ka = float(value)
            elif unit in ("a", "amp", "amps", "ampere", "amperes"):
                current_ka = float(value) / 1000.0
            else:
                continue
            if key == "sline":
                circuits = branch.get("params", {}).get("nlnum", 1)
                factor = branch.get("params", {}).get("fline", 1.0)
                if not isinstance(circuits, (int, float)) or circuits <= 0:
                    return None
                if not isinstance(factor, (int, float)) or factor <= 0:
                    return None
                current_ka *= float(circuits) * float(factor)
            return current_ka
    return None


def _transformer_end_ratings_ka(branch: dict, buses: dict):
    """Calcula rating [kA] por devanado desde potencia nominal y tensión nominal."""
    if branch.get("class") not in ("ElmTr2", "ElmTr3"):
        return {}
    params = {**branch.get("type_params", {}), **branch.get("params", {})}
    endings = ("hv", "mv", "lv") if branch.get("class") == "ElmTr3" else ("from", "to")
    result = {}
    common_mva = next((params.get(key) for key in ("strn", "sn", "snom", "s_rated_mva")
                       if isinstance(params.get(key), (int, float)) and params.get(key) > 0), None)
    for end in endings:
        suffix = end if branch.get("class") == "ElmTr3" else ("hv" if end == "from" else "lv")
        rated_mva = next((params.get(key) for key in (f"strn3_{suffix}", f"sn_{suffix}", f"snom_{suffix}")
                          if isinstance(params.get(key), (int, float)) and params.get(key) > 0), common_mva)
        bus_id = branch.get("bus_" + end)
        un_kv = buses.get(bus_id, {}).get("un_kv")
        if isinstance(rated_mva, (int, float)) and isinstance(un_kv, (int, float)) and un_kv > 0:
            result[end] = float(rated_mva) / (math.sqrt(3.0) * float(un_kv))
    return result


def _solved_island_ids(result: dict):
    return {island.get("id") for island in result.get("islands", []) if island.get("status") == "resuelto"}


def _deenergized_load_ids(status: dict):
    return set(status.get("deenergized_injections", {}).get("loads", ()))


def _duration_from_flags(flags: list[bool | None], times: list[dict]):
    """Integra intervalos reales y conserva su cobertura desconocida.

    Cada valor representa el estado en el comienzo del intervalo. Si uno de
    sus extremos no tiene solución LF, el intervalo completo queda
    desconocido, nunca se clasifica como una hora sin incumplimiento.
    """
    violation_s = known_s = unknown_s = 0.0
    for index, (left, right) in enumerate(zip(times, times[1:])):
        duration_s = right["t_s"] - left["t_s"]
        if duration_s < 0:
            raise ValueError("Los tiempos QDS deben ser crecientes")
        if flags[index] is None or flags[index + 1] is None:
            unknown_s += duration_s
        else:
            known_s += duration_s
            if flags[index]:
                violation_s += duration_s
    return {"violation_hours": violation_s / 3600.0, "known_hours": known_s / 3600.0,
            "unknown_hours": unknown_s / 3600.0,
            "coverage": "complete" if unknown_s == 0 else "partial"}


def _series_payload(model: dict, records: list[dict]):
    """Construye series QDS sin atribuir demanda o estados de islas no resueltas."""
    from power_machine.elements import load_pq_mw

    buses = {item["id"]: item for item in model.get("buses", [])}
    branches = {item["id"]: item for group in ("branches", "branches3") for item in model.get(group, [])}
    loads = {item["id"]: item for item in model.get("loads", [])}
    gens = {item["id"]: item for item in model.get("gens", [])}
    times = [{"index": row["index"], "timestamp_iso": row["timestamp_iso"], "t_s": row["t_s"]}
             for row in records]
    voltage = {bus_id: {"object_id": bus.get("object_id"), "relative_path": bus.get("relative_path"),
                        "name": bus.get("name"), "un_kv": bus.get("un_kv"), "u_pu": [], "u_kv": []}
               for bus_id, bus in buses.items()}
    branch_data = {branch_id: {"object_id": branch.get("object_id"), "relative_path": branch.get("relative_path"),
                               "name": branch.get("name"), "rating_ka": _rating_ka(branch),
                               "max_current_ka": [], "loading_pct": [], "end_loading_pct": []}
                   for branch_id, branch in branches.items()}
    dispatch = {gen_id: {"object_id": gen.get("object_id"), "relative_path": gen.get("relative_path"),
                         "name": gen.get("name"), "p_requested_mw": [], "p_realized_mw": [],
                         "q_realized_mvar": [], "apparent_exceedance": []}
                for gen_id, gen in gens.items()}
    requested_p, requested_q, supplied_p, supplied_q, unsupplied_p, unsupplied_q = [], [], [], [], [], []
    losses, total_gen, total_gen_q, balances, island_balances, interface = [], [], [], [], [], []
    interfaces = tuple(model.get("interface", ()))
    interface_links = {entry.get("link_id"): {"link_id": entry.get("link_id"), "circuit": entry.get("circuit"),
                                           "host_bus": entry.get("host_bus"), "boundary_bus": entry.get("boundary_bus"),
                                           "p_out_of_interface_mw": [], "q_out_of_interface_mvar": []}
                   for entry in interfaces if entry.get("link_id")}
    for record in records:
        result, plan = record["result"], record["plan"]
        status = record.get("energized_status") or energized_status(result)
        solved_islands = _solved_island_ids(result)
        solved_buses = {bus_id for island in result.get("islands", []) if island.get("id") in solved_islands
                        for bus_id in island.get("buses", ())}
        solved_gens_ids = {gen_id for island in result.get("islands", []) if island.get("id") in solved_islands
                           for gen_id in island.get("elements", {}).get("gens", ())}
        bus_rows = {row["id"]: row for row in result.get("buses", [])}
        for bus_id, target in voltage.items():
            row = bus_rows.get(bus_id, {})
            target["u_pu"].append(row.get("u_pu") if bus_id in solved_buses else None)
            target["u_kv"].append(row.get("u_kv") if bus_id in solved_buses else None)
        solved_gens = {row["id"]: row for row in result.get("gens", [])}
        for gen_id, target in dispatch.items():
            gen, solved = gens[gen_id], solved_gens.get(gen_id, {})
            requested_p_mw = (plan.get("reference_expected_p_mw") if gen.get("ref")
                              else plan.get("scenario", {}).get("gen_p_mw", {}).get(gen_id))
            p_mw = solved.get("p_mw") if gen_id in solved_gens_ids else None
            q_mvar = solved.get("q_mvar") if gen_id in solved_gens_ids else None
            rating_mva = gen.get("s_rated_mva")
            exceeds = (math.hypot(p_mw, q_mvar) > rating_mva if isinstance(p_mw, (int, float))
                       and isinstance(q_mvar, (int, float)) and isinstance(rating_mva, (int, float))
                       and rating_mva > 0 else None)
            target["p_requested_mw"].append(requested_p_mw)
            target["p_realized_mw"].append(p_mw)
            target["q_realized_mvar"].append(q_mvar)
            target["apparent_exceedance"].append(exceeds)
        branch_rows = {row["id"]: row for row in result.get("branches", [])}
        for branch_id, target in branch_data.items():
            branch, row = branches[branch_id], branch_rows.get(branch_id)
            row_solved = bool(row) and (row.get("island") in solved_islands or
                                        (row.get("island") is None and status["ok"]))
            if not row_solved:
                target["max_current_ka"].append(None)
                target["loading_pct"].append(None)
                target["end_loading_pct"].append({})
                continue
            endings = ("hv", "mv", "lv") if branch.get("class") == "ElmTr3" else ("from", "to")
            transformer_ratings = _transformer_end_ratings_ka(branch, buses)
            end_loading = {}
            currents = []
            for end in endings:
                current_ka = row.get("i_" + end + "_ka")
                if isinstance(current_ka, (int, float)) and math.isfinite(current_ka):
                    currents.append(float(current_ka))
                rating_ka = transformer_ratings.get(end) if transformer_ratings else target["rating_ka"]
                end_loading[end] = (100.0 * current_ka / rating_ka if isinstance(current_ka, (int, float))
                                    and isinstance(rating_ka, (int, float)) and rating_ka > 0 else None)
            known_loading = [value for value in end_loading.values() if value is not None]
            target["max_current_ka"].append(max(currents) if currents else None)
            target["loading_pct"].append(max(known_loading) if known_loading else None)
            target["end_loading_pct"].append(end_loading)
        interchange_p_mw = interchange_q_mvar = 0.0
        interface_available = False
        for interface in interfaces:
            link_id = interface.get("link_id")
            row = branch_rows.get(link_id)
            if not row or row.get("island") not in solved_islands:
                if link_id in interface_links:
                    interface_links[link_id]["p_out_of_interface_mw"].append(None)
                    interface_links[link_id]["q_out_of_interface_mvar"].append(None)
                continue
            host_bus = interface.get("host_bus")
            if row.get("bus_from") == host_bus:
                p_value, q_value = row.get("p_from_mw"), row.get("q_from_mvar")
            elif row.get("bus_to") == host_bus:
                p_value, q_value = row.get("p_to_mw"), row.get("q_to_mvar")
            else:
                if link_id in interface_links:
                    interface_links[link_id]["p_out_of_interface_mw"].append(None)
                    interface_links[link_id]["q_out_of_interface_mvar"].append(None)
                continue
            if isinstance(p_value, (int, float)) and isinstance(q_value, (int, float)):
                interchange_p_mw += p_value
                interchange_q_mvar += q_value
                interface_available = True
                if link_id in interface_links:
                    interface_links[link_id]["p_out_of_interface_mw"].append(p_value)
                    interface_links[link_id]["q_out_of_interface_mvar"].append(q_value)
            elif link_id in interface_links:
                interface_links[link_id]["p_out_of_interface_mw"].append(None)
                interface_links[link_id]["q_out_of_interface_mvar"].append(None)
        factors = record.get("factors", {})
        unserved_ids = _deenergized_load_ids(status)
        p_requested = q_requested = p_unserved = q_unserved = 0.0
        missing_demand = False
        for load_id, load in loads.items():
            if load.get("outserv", 0):
                continue
            factor = factors.get(load_id, 1.0)
            try:
                p_mw, q_mvar = load_pq_mw(load)
            except (KeyError, TypeError, ValueError):
                missing_demand = True
                continue
            p_requested += p_mw * factor
            q_requested += q_mvar * factor
            if load_id in unserved_ids:
                p_unserved += p_mw * factor
                q_unserved += q_mvar * factor
        numerical_failure = status.get("n_failed", 0) > 0 or not solved_islands
        p_served = None if numerical_failure or missing_demand else p_requested - p_unserved
        q_served = None if numerical_failure or missing_demand else q_requested - q_unserved
        totals = result.get("totals", {})
        loss_p_mw = totals.get("branch_loss_p_mw") if not numerical_failure else None
        generation_p_mw = totals.get("generation_p_mw") if not numerical_failure else None
        generation_q_mvar = totals.get("generation_q_mvar") if not numerical_failure else None
        requested_p.append(None if missing_demand else p_requested)
        requested_q.append(None if missing_demand else q_requested)
        supplied_p.append(p_served)
        supplied_q.append(q_served)
        unsupplied_p.append(None if numerical_failure or missing_demand else p_unserved)
        unsupplied_q.append(None if numerical_failure or missing_demand else q_unserved)
        losses.append(loss_p_mw)
        total_gen.append(generation_p_mw)
        total_gen_q.append(generation_q_mvar)
        balances.append(None if generation_p_mw is None or p_served is None or loss_p_mw is None
                        else generation_p_mw - p_served - loss_p_mw)
        island_rows = []
        for island in result.get("islands", []):
            if island.get("id") not in solved_islands:
                continue
            elements = island.get("elements", {})
            island_load = sum(load_pq_mw(loads[load_id])[0] * factors.get(load_id, 1.0)
                              for load_id in elements.get("loads", ()) if load_id in loads)
            island_gen = sum(solved_gens.get(gen_id, {}).get("p_mw", 0.0)
                             for gen_id in elements.get("gens", ()) if isinstance(solved_gens.get(gen_id, {}).get("p_mw"), (int, float)))
            island_loss = sum(row.get("p_loss_mw", 0.0) for row in branch_rows.values()
                              if row.get("island") == island.get("id") and isinstance(row.get("p_loss_mw"), (int, float)))
            island_rows.append({"island_id": island.get("id"), "demand_supplied_p_mw": island_load,
                                "generation_p_mw": island_gen, "loss_p_mw": island_loss,
                                "balance_p_mw": island_gen - island_load - island_loss})
        island_balances.append(island_rows)
        interface.append({"p_out_of_interface_mw": interchange_p_mw if interface_available else None,
                      "q_out_of_interface_mvar": interchange_q_mvar if interface_available else None})
    voltage_violations = {bus_id: _duration_from_flags(
        [None if value is None else not 0.95 <= value <= 1.05 for value in item["u_pu"]], times)
        for bus_id, item in voltage.items()}
    overloads = {branch_id: _duration_from_flags(
        [None if value is None else value > 100.0 for value in item["loading_pct"]], times)
        for branch_id, item in branch_data.items()}
    return {"time": times, "voltage_by_bus": list(voltage.values()), "branch_loading": list(branch_data.values()),
            "loss_p_mw": losses, "load_p_mw": supplied_p, "load_q_mvar": supplied_q,
            "demand_requested_p_mw": requested_p, "demand_requested_q_mvar": requested_q,
            "demand_supplied_p_mw": supplied_p, "demand_supplied_q_mvar": supplied_q,
            "demand_unsupplied_p_mw": unsupplied_p, "demand_unsupplied_q_mvar": unsupplied_q,
            "generation_p_mw": total_gen, "generation_q_mvar": total_gen_q, "balance_p_mw": balances,
            "balance_by_island": island_balances, "dispatch_by_generator": list(dispatch.values()),
            "interface_220kv": interface, "interface_links": list(interface_links.values()),
            "summary": {"voltage_out_of_band_hours_by_bus": voltage_violations,
                        "branch_overload_hours_by_branch": overloads,
                        "voltage_violation_bus_count": sum(item["violation_hours"] > 0 for item in voltage_violations.values()),
                        "overloaded_branch_count": sum(item["violation_hours"] > 0 for item in overloads.values()),
                        "unknown_voltage_hours": sum(item["unknown_hours"] for item in voltage_violations.values()),
                        "unknown_overload_hours": sum(item["unknown_hours"] for item in overloads.values()),
                        "branches_without_rating": sum(item["rating_ka"] is None and not _transformer_end_ratings_ka(branches[key], buses)
                                                       for key, item in branch_data.items()),
                        "loading_note": "Cargabilidad indicativa con ratings de unidad explícita, sin validar contra PF",
                        "interface_note": "P y Q positivos saliendo de interfaz hacia el equivalente, por enlace y extremo de interfaz",
                        "duration_note": "Duración integrada entre timestamps. Paso o isla no resuelta es desconocido"}}


def _write_panel_csv(target: Path, payload: dict):
    """Exporta resumen plano adicional a los CSV trazables de QSS."""
    path = target / "qds_panel_summary.csv"
    with path.open("w", encoding="utf-8", newline="") as stream:
        fields = ("step_index", "timestamp_iso", "t_s", "loss_p_mw", "load_p_mw", "load_q_mvar",
                  "generation_p_mw", "generation_q_mvar", "balance_p_mw",
                  "interface_p_out_mw", "interface_q_out_mvar")
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for index, instant in enumerate(payload["time"]):
            interface = payload["interface_220kv"][index]
            writer.writerow({"step_index": instant["index"], "timestamp_iso": instant["timestamp_iso"],
                             "t_s": instant["t_s"], "loss_p_mw": payload["loss_p_mw"][index],
                             "load_p_mw": payload["load_p_mw"][index], "load_q_mvar": payload["load_q_mvar"][index],
                             "generation_p_mw": payload["generation_p_mw"][index],
                             "generation_q_mvar": payload["generation_q_mvar"][index],
                             "balance_p_mw": payload["balance_p_mw"][index],
                             "interface_p_out_mw": interface["p_out_of_interface_mw"],
                             "interface_q_out_mvar": interface["q_out_of_interface_mvar"]})
    return path


def _energized_result(raw_result: dict):
    """Conserva el estado global LF y devuelve su criterio energizado separado."""
    return raw_result, energized_status(raw_result)


def execute_request(request: dict, *, cancel=None, progress=None) -> dict:
    """Ejecuta QSS y devuelve un sobre JSON, con cancelación entre instantes."""
    result = None
    try:
        if not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA or request.get("module") != MODULE:
            raise ValueError("Esquema o módulo de request incompatible")
        root = Path(request["root"]).resolve()
        snapshot = copy.deepcopy(request["snapshot"])
        model, study = snapshot["model"], snapshot["study"]
        if not isinstance(model, dict) or not isinstance(study, dict):
            raise ValueError("Modelo y estudio deben ser diccionarios")
        fingerprints = snapshot.get("fingerprints", {})
        if fingerprints.get("model_sha256") != fingerprint(model) or fingerprints.get("study_sha256") != fingerprint(study):
            raise ValueError("Fingerprint incompatible de modelo o estudio")
        result = _envelope(model, study)
        if study.get("scenario"):
            raise ValueError("QDS aún no aplica un escenario previo, use el caso base activo")
        inputs = request.get("input", {})
        if not isinstance(inputs, dict):
            raise ValueError("input debe ser un diccionario")
        output_base = output_root()
        selected_output = Path(inputs.get("output_dir", "quasi_dynamic"))
        output_parent = confined_output(selected_output if selected_output.is_absolute() else output_base / selected_output, output_base)
        output_parent.mkdir(parents=True, exist_ok=True)
        source_model, series, configuration = _read_profile(root, model, inputs)
        source_model, trace_aliases = _with_traceability(source_model)
        target = output_parent / result["run_id"]
        if target.exists():
            raise FileExistsError("La carpeta de salida ya existe")
        target.mkdir()
        rule = configuration.get("dispatch", "reference")
        policy = configuration.get("policy", "continue")
        warm_start = bool(configuration.get("warm_start", True))
        if rule not in ("reference", "proportional") or policy not in ("continue", "stop"):
            raise ValueError("Regla de despacho o política desconocida")
        lf_options = {**LDF_OPTIONS, "warm_start": warm_start}
        if lf_options.get("q_limits") is not False or lf_options.get("missing") != "error":
            raise ValueError("QDS requiere q_limits=False y missing='error'")
        records, deenergized_steps = [], []
        started = time.perf_counter()
        total = len(series.steps)
        base_raw = run_loadflow(source_model, options={**lf_options, "warm_start": False})
        base_result, base_status = _energized_result(base_raw)
        if not base_status["ok"]:
            raise ValueError("La LF base no converge en todas las islas energizadas")
        if base_status["n_deenergized"]:
            deenergized_steps.append({"index": "base", **base_status})
        dispatch_model = copy.deepcopy(source_model)
        excluded_load_ids = _deenergized_load_ids(base_status)
        for load in dispatch_model.get("loads", []):
            if load["id"] in excluded_load_ids:
                load["outserv"] = 1
        base = dispatch_base(dispatch_model, base_result)
        previous_result = base_result
        previous_loss_p_mw = base_result["totals"]["branch_loss_p_mw"]
        loss_source_index = -1
        metadata = {"schema": "pm-qds-panel-v1", "validation_status": "sin validar contra PF",
                    "series_name": series.name, "rule": rule, "policy": policy,
                    "warm_start": warm_start, "lf_options": lf_options,
                    "model_sha256": fingerprint(source_model), "provenance": series.provenance,
                    "dispatch_base": {"reference_id": base.reference_id, "reference_p_mw": base.reference_p_mw,
                                      "nonreference_p_mw": base.nonreference_p_mw, "loss_p_mw": base.loss_p_mw}}
        for index in range(total):
            if _cancelled(cancel):
                result["status"] = "cancelled"
                break
            step = series.steps[index]
            plan = plan_dispatch(dispatch_model, step, base, previous_loss_p_mw, rule)
            tick = time.perf_counter()
            raw_step = run_loadflow(source_model, plan["scenario"], {**lf_options, "base_result": previous_result})
            step_result, step_status = _energized_result(raw_step)
            voltages = [bus["u_pu"] for bus in step_result["buses"] if bus["u_pu"] is not None]
            collapse = bool(voltages and min(voltages) < 0.5)
            failure = not step_status["ok"] or collapse
            record = {"index": index, "timestamp_iso": step.timestamp_iso, "t_s": step.t_s,
                      "factors": step.factors, "plan": plan, "result": step_result,
                      "energized_status": step_status,
                      "loss_source_index": loss_source_index, "elapsed_s": time.perf_counter() - tick,
                      "collapse": collapse, "failure": failure,
                      "accepted": step_status["ok"] and not collapse
                      and step_result["max_mismatch_mva"] <= 1e-6}
            records.append(record)
            if step_status["n_deenergized"]:
                deenergized_steps.append({"index": index, **step_status})
            previous_result = step_result
            if step_status["ok"]:
                previous_loss_p_mw = step_result["totals"]["branch_loss_p_mw"]
                loss_source_index = index
            if progress:
                progress({"stage": "qds", "completed": index + 1, "total": total,
                          "fraction": (index + 1) / total, "t_s": series.steps[index].t_s})
            if failure and policy == "stop":
                result["status"] = "partial"
                break
        if not records:
            result["payload"] = {"completed_count": 0, "total_count": total}
            return result
        qss_summary = write_report(target, source_model, records, metadata,
                                   elapsed_s=time.perf_counter() - started)
        payload = _series_payload(source_model, records)
        summary_csv = _write_panel_csv(target, payload)
        payload.update(completed_count=len(records), total_count=total,
                       qss_summary=qss_summary, base_result=base_result,
                       deenergized_steps=deenergized_steps,
                       exports=[str(path.relative_to(root)) for path in
                                sorted(target.glob("*.csv"))] + [str(summary_csv.relative_to(root))])
        payload["exports"] = list(dict.fromkeys(payload["exports"]))
        result["payload"] = payload
        result["provenance"].update(series.provenance, output_dir=str(target.relative_to(root)))
        if trace_aliases:
            result["diagnostics"].append(_diagnostic("traceability_aliases",
                f"Se completaron {trace_aliases} aliases object_id o relative_path desde id y path exportados", level="warning"))
        if deenergized_steps:
            result["diagnostics"].append(_diagnostic("deenergized_islands",
                f"{len(deenergized_steps)} paso(s) con islas desenergizadas sin referencia", level="warning"))
        if result["status"] == "completed" and len(records) != total:
            result["status"] = "partial"
        if result["status"] == "completed" and (qss_summary.get("nonconverged_steps")
                                                   or payload["summary"]["branches_without_rating"]
                                                   or deenergized_steps):
            result["status"] = "partial"
        return result
    except UnsupportedProfile as exc:
        if result is None:
            result = _envelope({}, {})
            result["model_sha256"] = None
            result["study_sha256"] = None
        result["status"] = "unsupported"
        result["diagnostics"].append(_diagnostic("profile_unsupported", exc))
        return result
    except (ValueError, KeyError, TypeError, OSError, json.JSONDecodeError) as exc:
        if result is None:
            result = _envelope({}, {})
            result["model_sha256"] = None
            result["study_sha256"] = None
        result["status"] = "invalid_input"
        result["diagnostics"].append(_diagnostic("invalid_request", exc))
        return result
    except Exception as exc:
        if result is None:
            result = _envelope({}, {})
            result["model_sha256"] = None
            result["study_sha256"] = None
        result["status"] = "error"
        result["diagnostics"].append(_diagnostic("qds_execution_error", exc))
        return result
