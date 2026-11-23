"""Build the electrical inventory without changing original PF parameters."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import math
import warnings as python_warnings

from .io_pf import Package, load_package


_PORTS = {
    "ElmLne": ("bus1", "bus2"),
    "ElmTr2": ("bushv", "buslv"),
    "ElmTr3": ("bushv", "busmv", "buslv"),
    "ElmZpu": ("bus1", "bus2"),
    "ElmScap": ("bus1", "bus2"),
    "ElmSind": ("bus1", "bus2"),
    "ElmCoup": ("bus1", "bus2"),
    "ElmLod": ("bus1",),
    "ElmXnet": ("bus1",),
    "ElmSym": ("bus1",),
    "ElmGenstat": ("bus1",),
    "ElmPvsys": ("bus1",),
    "ElmShnt": ("bus1",),
    "ElmNec": ("bus1",),
}


def _source_relative(path: str) -> str:
    """Remove only the absolute PF project prefix from the interface map."""
    marker = "Network Model.IntPrjfolder"
    index = path.find(marker)
    return path[index:] if index >= 0 else path


def _tables(package: Package):
    params = defaultdict(dict)
    units = defaultdict(dict)
    refs = defaultdict(list)
    ports = defaultdict(dict)
    for tables in package.scopes.values():
        for row in tables["parameters"]:
            params[row["object_id"]][row["attribute"]] = row["value"]
            if row["unit"]:
                units[row["object_id"]][row["attribute"]] = row["unit"]
        for row in tables["references"]:
            refs[row["source_id"]].append(row)
        for row in tables["ports"]:
            if row["status"] == "PRESENT":
                ports[row["element_id"]][row["port"]] = row
    return params, units, refs, ports


def build_model(package_root: str | Path, enrich_from: str | Path | list[str | Path] | None = None, *, scopes=None, interface_map=None, s_base_mva=100.0, f_nom_hz=None) -> dict:
    """Build power_machine-model-v1 from portable export scopes."""
    package = load_package(package_root, scopes=scopes, interface_map=interface_map)
    params, units, refs, ports = _tables(package)
    warnings = set()
    if f_nom_hz is None:
        message = ("Nominal frequency is not supplied by this portable import. "
                   "Using the legacy 50 Hz default. Pass f_nom_hz explicitly.")
        python_warnings.warn(message, UserWarning, stacklevel=2)
        warnings.add(message)
        f_nom_hz = 50.0
    f_nom_hz = float(f_nom_hz)
    if not math.isfinite(f_nom_hz) or f_nom_hz <= 0:
        raise ValueError("f_nom_hz must be positive and finite")
    result = {
        "schema": "power_machine-model-v1",
        "meta": {
            "source_package": package.root.name,
            "sha256_inputs": package.sha256_inputs,
            "s_base_mva": float(s_base_mva),
            "f_nom_hz": float(f_nom_hz),
        },
        "buses": [], "switches": [], "branches": [], "branches3": [],
        "shunts": [], "loads": [], "gens": [], "controllers": [],
        "interface": [], "other_elements": [], "warnings": [],
    }

    def base(obj: dict) -> dict:
        object_id = obj["object_id"]
        data = {
            "id": object_id, "class": obj["class"], "name": obj["name"],
            "scope": obj["scope"], "path": obj["relative_path"],
            "outserv": params[object_id].get("outserv", 0),
            "params": params[object_id], "units": units[object_id],
            "type_params": {}, "type_units": {}, "type_class": None, "type_id": None,
        }
        type_refs = [r for r in refs[object_id] if r["attribute"] == "typ_id" and r["target_id"]]
        if type_refs:
            type_id = type_refs[0]["target_id"]
            type_obj = package.by_id.get(type_id)
            if type_obj:
                data.update(type_id=type_id, type_class=type_obj["class"],
                            type_params=params[type_id], type_units=units[type_id])
            else:
                warnings.add(f"Unresolved type reference: {object_id} -> {type_id}")
        return data

    def bus(obj: dict, port: str) -> str | None:
        row = ports[obj["object_id"]].get(port)
        if not row or not row["terminal_relative_path"]:
            warnings.add(f"Port without a bus: {obj['object_id']} {port}")
            return None
        terminal = package.by_path.get(row["terminal_relative_path"])
        if not terminal or terminal["class"] != "ElmTerm":
            warnings.add(f"Unresolved bus: {obj['object_id']} {port} -> {row['terminal_relative_path']}")
            return None
        if params[terminal["object_id"]].get("outserv", 0) == 1 and params[obj["object_id"]].get("outserv", 0) != 1:
            warnings.add(f"Element connected to an out-of-service bus: {obj['object_id']} {port} -> {terminal['object_id']}")
        return terminal["object_id"]

    objects = sorted(package.by_id.values(), key=lambda item: item["object_id"])
    for obj in objects:
        cls = obj["class"]
        data = base(obj)
        if cls == "ElmTerm":
            data.update(un_kv=params[obj["object_id"]].get("uknom"))
            result["buses"].append(data)
        elif cls in ("ElmLne", "ElmTr2", "ElmZpu", "ElmScap", "ElmSind"):
            first, second = _PORTS[cls]
            data.update(bus_from=bus(obj, first), bus_to=bus(obj, second))
            result["branches"].append(data)
        elif cls == "ElmTr3":
            data.update(bus_hv=bus(obj, "bushv"), bus_mv=bus(obj, "busmv"),
                        bus_lv=bus(obj, "buslv"))
            result["branches3"].append(data)
        elif cls == "ElmCoup":
            data.update(bus1=bus(obj, "bus1"), bus2=bus(obj, "bus2"),
                        closed=params[obj["object_id"]].get("on_off") == 1,
                        element_id=None)
            result["switches"].append(data)
        elif cls in ("ElmShnt", "ElmNec"):
            data.update(bus=bus(obj, "bus1"))
            if cls == "ElmNec":
                data["seq0_only"] = True
            result["shunts"].append(data)
        elif cls == "ElmLod":
            p = params[obj["object_id"]]
            data.update(bus=bus(obj, "bus1"),
                        p_mw=p.get("plini") if units[obj["object_id"]].get("plini") == "MW" else None,
                        q_mvar=p.get("qlini") if units[obj["object_id"]].get("qlini") == "Mvar" else None,
                        scale=p.get("scale0"))
            result["loads"].append(data)
        elif cls == "ElmXnet":
            from .external_grid import normalize_external
            data.update(bus=bus(obj, "bus1"))
            normalize_external(data)
            result["gens"].append(data)
        elif cls in ("ElmSym", "ElmGenstat", "ElmPvsys"):
            p = params[obj["object_id"]]
            n_units = p.get("ngnum", 1)
            def rated_power(attribute: str, unit: str):
                raw = p.get(attribute)
                return raw * n_units if raw is not None and units[obj["object_id"]].get(attribute) == unit else None
            rated_mva = p.get("sgn")
            rated_unit = units[obj["object_id"]].get("sgn")
            if rated_mva is None:
                rated_mva = data["type_params"].get("sgn")
                rated_unit = data["type_units"].get("sgn")
            data.update(bus=bus(obj, "bus1"),
                        p_mw=rated_power("pgini", "MW"),
                        q_mvar=rated_power("qgini", "Mvar"),
                        v_set_pu=p.get("usetp"), mode=p.get("av_mode"),
                        ref=p.get("ip_ctrl") == 1, n_units=n_units,
                        s_rated_mva=rated_mva * n_units if rated_mva is not None and rated_unit == "MVA" else None,
                        q_min_mvar=None, q_max_mvar=None)
            result["gens"].append(data)
        elif cls in ("ElmStactrl", "ElmComp", "ElmDsl"):
            members = []
            relations = []
            for ref in refs[obj["object_id"]]:
                target_id = ref["target_id"]
                if target_id:
                    if target_id not in package.by_id:
                        warnings.add(f"Unresolved reference: {obj['object_id']} {ref['attribute']} -> {target_id}")
                    elif ref["attribute"] != "typ_id":
                        members.append(target_id)
                        relations.append({"attribute": ref["attribute"], "target_id": target_id,
                                          "reference_index": ref["reference_index"]})
            data.update(members=list(dict.fromkeys(members)), relations=relations)
            result["controllers"].append(data)
        elif cls == "StaSwitch":
            parent_id = obj["parent_id"]
            cubicle = package.by_id.get(parent_id) or package.by_path.get(obj["parent_relative_path"])
            if not cubicle:
                warnings.add(f"Unresolved StaSwitch cubicle: {obj['object_id']} -> {obj['parent_relative_path']}")
            terminal_path = cubicle["parent_relative_path"] if cubicle else obj["parent_relative_path"].rsplit("\\", 1)[0]
            terminal = package.by_path.get(terminal_path)
            attached = [r for table in package.scopes.values() for r in table["ports"]
                        if cubicle and r["cubicle_relative_path"] == cubicle["relative_path"] and r["status"] == "PRESENT"]
            if not terminal or terminal["class"] != "ElmTerm":
                warnings.add(f"Unresolved StaSwitch bus: {obj['object_id']} -> {terminal_path}")
            if not attached:
                warnings.add(f"StaSwitch without an associated element: {obj['object_id']}")
            for row in attached or [None]:
                entry = data.copy()
                entry.update(bus1=terminal["object_id"] if terminal else None, bus2=None,
                             closed=params[obj["object_id"]].get("on_off") == 1,
                             element_id=row["element_id"] if row else None,
                             port=row["port"] if row else None)
                result["switches"].append(entry)
        elif cls in ("ElmRelay", "RelFrq", "RelFmeas", "RelLogic", "RelMeasure", "RelUlim",
                     "StaPqmea", "StaVmea", "StaVt", "ElmSamp", "ElmClock"):
            result["other_elements"].append(data)

    for row in package.interface_map:
        def lookup(column: str) -> str | None:
            obj = package.by_path.get(_source_relative(row[column]))
            if not obj:
                warnings.add(f"Interface {row['circuit']}: unresolved {column}")
            return obj["object_id"] if obj else None
        result["interface"].append({
            "circuit": row["circuit"], "link_id": lookup("link_path"),
            "boundary_bus": lookup("boundary_terminal_path"),
            "host_bus": lookup("host_terminal_path"),
        })
    result["warnings"] = sorted(warnings)
    if enrich_from is not None:
        from .pf_enrich import enrich_model
        sources = enrich_from if isinstance(enrich_from, list) else [enrich_from]
        result["meta"]["enrichment"] = {}
        for source in sources:
            result, enrichment = enrich_model(result, source)
            result["meta"]["enrichment"][str(source)] = enrichment
    return result
