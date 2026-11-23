"""Invariantes de identidad, unidades, puertos y dominio calculable."""

import math

from .. import elements
from ..topology import ideal_line_candidate, ideal_zpu_candidate
from .commands import index_model, port_field
from .schema import GROUPS, PORTS, VARIANT_SCHEMA, VariantCheck, VariantError, canonical_json, model_sha256


def finite_number(value, positive=False):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and (not positive or value > 0))


def validate_model(model, base_model, variant):
    checks = []
    def add(code, item, message, severity="error"):
        checks.append(VariantCheck(severity, code, item.get("id"), message))
    seen, paths = set(), set()
    base_index = index_model(base_model)
    index = index_model(model)
    buses = {b["id"]: b for b in model.get("buses", [])}
    touched = {c.object_id for c in variant.commands if c.op != "move"}
    created = {c.object_id for c in variant.commands if c.op == "create"}
    changed_bases = {b["id"] for b in model.get("buses", []) if b["id"] in base_index
                     and b.get("un_kv") != base_index[b["id"]][1].get("un_kv")}
    original_created = {c.object_id: c.data for c in variant.commands if c.op == "create" and c.group == "buses"}
    changed_bases.update(b["id"] for b in model.get("buses", []) if b["id"] in original_created
                         and b.get("un_kv") != original_created[b["id"]].get("un_kv"))
    for group in GROUPS:
        for item in model.get(group, []):
            iid = item.get("id")
            if not isinstance(iid, str) or not iid or iid in seen:
                add("duplicate_id", item, "Identidad ausente o repetida")
            seen.add(iid)
            if not isinstance(item.get("path"), str) or not item["path"]:
                add("missing_path", item, "Ruta de origen ausente")
            elif item["path"] in paths and iid in created:
                add("duplicate_path", item, "Ruta de origen repetida")
            paths.add(item.get("path"))
            if item.get("outserv", 0) not in (0, 1, False, True):
                add("invalid_status", item, "outserv debe ser 0 o 1")
            active = item.get("outserv", 0) != 1
            connected = []
            item_ports = ("bus1",) if item.get("class") == "StaSwitch" else PORTS[group]
            for field in item_ports:
                bid = item.get(field)
                if bid is None:
                    if active:
                        add("disconnected_port", item, "Puerto desconectado: " + field, "warning")
                elif bid not in buses:
                    add("unknown_bus", item, "Barra inexistente: " + str(bid))
                else:
                    connected.append(bid)
                    if active and buses[bid].get("outserv", 0) == 1:
                        # No se penaliza una condición histórica intacta.
                        historical = iid in base_index and base_index[iid][1].get(field) == bid and base_index.get(bid, (None, {}))[1].get("outserv") == 1
                        add("retired_bus_dependency", item, "Referencia activa a barra retirada: " + bid,
                            "warning" if historical else "error")
            needs_check = iid in touched or any(bid in changed_bases for bid in connected)
            if group == "buses":
                if not finite_number(item.get("un_kv"), True):
                    add("missing_value", item, "un_kv debe ser positivo en kV")
            if item.get("class") == "StaSwitch":
                target = index.get(item.get("element_id"))
                if target is None:
                    original = base_index.get(iid, (None, {}))[1]
                    unchanged = iid not in touched and item == original
                    add("unknown_switch_target", item, "Cubículo sin elemento identificable",
                        "warning" if unchanged else "error")
                else:
                    try:
                        field = port_field(target[0], target[1], item.get("port"))
                        if item.get("bus1") != target[1].get(field):
                            add("switch_port_mismatch", item, "Cubículo y puerto refieren barras distintas")
                    except ValueError as exc:
                        add("invalid_port", item, str(exc))
            if item.get("class") == "ElmCoup" and active and item.get("closed") and len(connected) == 2:
                uv = [buses[b].get("un_kv") for b in connected]
                if all(finite_number(v, True) for v in uv) and not math.isclose(*uv, rel_tol=1e-9):
                    original = base_index.get(iid, (None, {}))[1]
                    unchanged = all(item.get(k) == original.get(k) for k in ("bus1", "bus2", "closed", "outserv")) and not any(b in changed_bases for b in connected)
                    if unchanged:
                        add("historical_voltage_mismatch", item, "Acoplador original con nominales distintas, política histórica preservada", "warning")
                    else:
                        add("voltage_mismatch", item, "Acoplador ideal conecta tensiones nominales distintas")
            if not needs_check or (not active and iid not in created):
                continue
            if iid in created and (not isinstance(item.get("name"), str) or not item["name"].strip()):
                add("missing_value", item, "Nombre visible explícito ausente")
            # Unidades nuevas o modificadas no pueden contradecir los nombres nativos.
            native_units = {"R1": ("Ohm", "ohm", "Ω"), "X1": ("Ohm", "ohm", "Ω"),
                            "B1": ("uS", "µS"), "G1": ("uS", "µS"), "un_kv": ("kV",),
                            "p_mw": ("MW",), "q_mvar": ("Mvar",), "v_set_pu": ("p.u.",),
                            "strn": ("MVA",), "utrn_h": ("kV",), "utrn_l": ("kV",),
                            "pcutr": ("kW",), "pfe": ("kW",), "uktr": ("%",),
                            "curmg": ("%",), "dutap": ("%",), "phitr": ("deg",),
                            "qcapn": ("Mvar",), "qrean": ("Mvar",), "ushnm": ("kV",), "Sn": ("MVA",)}
            for unit_map in ("units", "type_units"):
                for key, unit in item.get(unit_map, {}).items():
                    if key in native_units and unit not in native_units[key]:
                        add("incompatible_unit", item, f"Unidad incompatible en {key}: {unit}")
            if item.get("draft"):
                add("incomplete_draft", item, "Borrador visible, bloqueado para cálculo")
            if group in ("loads", "gens"):
                for key in ("p_mw", "q_mvar"):
                    if not finite_number(item.get(key)):
                        add("missing_value", item, "Potencia ausente o no finita: " + key)
                if group == "loads" and not finite_number(item.get("scale")):
                    add("missing_value", item, "Escala de carga explícita ausente")
                if group == "loads":
                    try:
                        elements.load_pq_mw(item)
                    except (ValueError, TypeError) as exc:
                        add("invalid_electrical_data", item, str(exc))
                if group == "gens":
                    try:
                        elements.gen_pq_mw(item)
                    except (ValueError, TypeError) as exc:
                        add("invalid_electrical_data", item, str(exc))
                    if item.get("mode") not in ("constq", "constv") or not isinstance(item.get("ref"), bool):
                        add("invalid_generator_mode", item, "Modo LF y referencia deben ser explícitos")
                    if not finite_number(item.get("v_set_pu"), True):
                        add("missing_value", item, "Consigna de tensión ausente")
                    low, high = item.get("q_min_mvar"), item.get("q_max_mvar")
                    if low is not None and high is not None and low > high:
                        add("invalid_limits", item, "Límites Q invertidos")
                    if iid in created:
                        add("lf_only_generator", item, "Generador nuevo apto solo para LF, RMS y CC requieren datos propios", "warning")
            # Las estampas existentes comprueban los parámetros, sin reimplementar ecuaciones.
            try:
                sb = model["meta"]["s_base_mva"]
                if group == "branches" and len(connected) == 2:
                    uv = [buses[b]["un_kv"] for b in connected]
                    if item["class"] == "ElmLne":
                        if not ideal_line_candidate(item) or iid in created:
                            elements.y_line(item, *uv, sb)
                    elif item["class"] == "ElmTr2":
                        elements.y_tr2(item, *uv, sb)
                    elif item["class"] == "ElmZpu" and not ideal_zpu_candidate(item):
                        elements.y_zpu(item, *uv, sb)
                elif group == "branches3" and len(connected) == 3:
                    elements.y_tr3(item, *(buses[item[k]]["un_kv"] for k in ("bus_hv","bus_mv","bus_lv")), sb)
                elif group == "shunts" and connected:
                    elements.y_shunt(item, buses[connected[0]]["un_kv"], sb)
            except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
                add("invalid_electrical_data", item, str(exc))
            if any(bid in changed_bases for bid in connected):
                add("nominal_rechecked", item, "Base nominal modificada, estampa comprobada con nuevas bases", "warning")
                conversion = item.get("provenance", {}).get("conversions", [])
                if any(c.get("input_unit") == "p.u." for c in conversion):
                    add("stale_conversion", item, "Recrear conversión p.u. tras cambiar la tensión nominal")
            if iid in created:
                if item["class"] == "ElmLne":
                    for key in ("R1", "X1", "G1", "B1", "nlnum"):
                        if not finite_number(item.get("params", {}).get(key)):
                            add("missing_value", item, "Parámetro de línea ausente o no finito: " + key)
                elif item["class"] == "ElmZpu":
                    for key, unit in {"Sn": "MVA", "r_pu": "p.u.", "x_pu": "p.u.",
                                      "r_pu_ji": "p.u.", "x_pu_ji": "p.u.", "gi_pu": "p.u.",
                                      "bi_pu": "p.u.", "gj_pu": "p.u.", "bj_pu": "p.u.",
                                      "uratio": "1", "nphshift": "1", "ag": "deg"}.items():
                        if not finite_number(item.get("params", {}).get(key)):
                            add("missing_value", item, "Parámetro de impedancia ausente: " + key)
                        if item.get("units", {}).get(key) != unit:
                            add("incompatible_unit", item, f"{key} requiere unidad {unit}")
                elif item["class"] == "ElmShnt":
                    for key, unit in {"shtype": "1", "qcapn": "Mvar", "qrean": "Mvar", "ushnm": "kV", "ncapa": "1", "ncapx": "1"}.items():
                        if not finite_number(item.get("params", {}).get(key)):
                            add("missing_value", item, "Parámetro de shunt ausente: " + key)
                        if item.get("units", {}).get(key) != unit:
                            add("incompatible_unit", item, f"{key} requiere unidad {unit}")
                elif group == "switches" and not isinstance(item.get("closed"), bool):
                    add("missing_value", item, "Estado del interruptor explícito ausente")
                required_units = {"buses": {"un_kv": "kV"},
                                  "loads": {"p_mw": "MW", "q_mvar": "Mvar", "scale": "1"},
                                  "gens": {"p_mw": "MW", "q_mvar": "Mvar", "v_set_pu": "p.u."}}
                for key, unit in required_units.get(group, {}).items():
                    if item.get("units", {}).get(key) != unit:
                        add("incompatible_unit", item, f"{key} requiere unidad {unit}")
                if item.get("class") == "ElmLne":
                    for key, unit in {"R1": "Ohm", "X1": "Ohm", "B1": "uS", "G1": "uS", "nlnum": "1"}.items():
                        if item.get("units", {}).get(key) != unit:
                            add("incompatible_unit", item, f"{key} requiere unidad {unit}")
                elif item.get("class") == "ElmTr2":
                    for key, unit in {"strn": "MVA", "utrn_h": "kV", "utrn_l": "kV", "uktr": "%", "pcutr": "kW", "pfe": "kW", "curmg": "%", "nt2ag": "1", "dutap": "%", "phitr": "deg", "nntap0": "1", "tap_side": "1", "itrdr": "1", "itrdl": "1"}.items():
                        if item.get("type_params", {}).get(key) is None:
                            add("missing_value", item, "Parámetro de transformador explícito ausente: " + key)
                        if item.get("type_units", {}).get(key) != unit:
                            add("incompatible_unit", item, f"{key} requiere unidad {unit}")
                    for key in ("nntap", "ntnum"):
                        if item.get("params", {}).get(key) is None:
                            add("missing_value", item, "Parámetro de transformador ausente: " + key)
                elif item.get("class") in ("ElmZpu", "ElmShnt"):
                    if not item.get("units") and not item.get("type_units"):
                        add("incompatible_unit", item, "Datos nuevos requieren unidades explícitas")
    return tuple(checks)


def check_variant(base_model, variant):
    """Comprueba la transacción completa sin mutar el modelo ni la variante."""
    from .commands import replay_commands
    try:
        canonical_json(base_model)
        if variant.schema != VARIANT_SCHEMA:
            return (VariantCheck("error", "variant_schema", None, "Esquema incompatible"),)
        if variant.base_model_sha256 != model_sha256(base_model):
            return (VariantCheck("error", "base_hash_mismatch", None, "El modelo base cambió, no se rebasa por nombre"),)
        canonical_json(variant.layout)
        model, _ = replay_commands(base_model, variant)
        canonical_json(model)
        return validate_model(model, base_model, variant)
    except VariantError as exc:
        return exc.checks
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError) as exc:
        return (VariantCheck("error", "invalid_input", None, str(exc)),)
