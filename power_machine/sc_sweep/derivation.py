"Explicit data, numerical methods and traceable results for sc sweep studies."
from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path


def _source(item, attribute):
    return {"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
            "source_path": "model.json#" + item["id"], "source_row": "object_id=" + item["id"],
            "source_attribute": attribute}


def _entry(item, field, attribute, value, unit, status="available", **extra):
    return {**_source(item, attribute), "field": field, "value": value, "unit": unit,
            "base": None, "status": status, **extra}


def _unit(item, field, *, typed=False, fallback="1"):
    """Return the exported unit without substituting a calculated unit."""
    units = item.get("type_units" if typed else "units", {})
    return units.get(field, fallback)


def build_preflight(model, *, model_source_path=None):
    "List available short-circuit parameters and missing data that prevent evaluation."
    records = []
    for branch in model.get("branches", ()):
        params, typed, cls = branch.get("params", {}), branch.get("type_params", {}), branch.get("class")
        if cls == "ElmLne":
            for sequence, r_key, x_key in ((1, "R1", "X1"), (0, "R0", "X0")):
                value = (params.get(r_key), params.get(x_key))
                status = "available" if None not in value else "missing"
                records.append(_entry(branch, f"z{sequence}", f"params.{r_key} + j*params.{x_key}",
                                      None if status == "missing" else {"real": value[0], "imag": value[1]},
                                      _unit(branch, r_key, fallback="Ohm"), status,
                                      component_units={"real": _unit(branch, r_key, fallback="Ohm"),
                                                       "imag": _unit(branch, x_key, fallback="Ohm")}))
            records.append(_entry(branch, "nlnum", "params.nlnum", params.get("nlnum"),
                                  _unit(branch, "nlnum", fallback="1"),
                                  "available" if params.get("nlnum") is not None else "missing",
                                  note="R1/X1 is neither multiplied nor divided without confirmed parallel-circuit semantics"))
            records.append(_entry(branch, "z2", "no exportado", None, "Ohm", "missing"))
        elif cls == "ElmTr2":
            for key in ("uktr", "pcutr", "strn", "utrn_h", "uk0tr", "zx0hl_n", "nt2ag", "dutap"):
                value = typed.get(key)
                records.append(_entry(branch, key, f"type_params.{key}", value, _unit(branch, key, typed=True),
                                      "available" if value is not None else "missing"))
            clock = typed.get("nt2ag")
            records.append(_entry(branch, "nt2ag_angle_deg", "type_params.nt2ag * 30 deg",
                                  None if clock is None else float(clock) * 30.0, "deg",
                                  "derived" if clock is not None else "missing",
                                  original_unit=_unit(branch, "nt2ag", typed=True),
                                  conversion="clock number * 30 deg"))
            records.append(_entry(branch, "ntnum", "params.ntnum", params.get("ntnum"),
                                  _unit(branch, "ntnum", fallback="1"),
                                  "available" if params.get("ntnum") is not None else "missing",
                                  note="Unit count retained without duplicating rated power"))
            needed = (typed.get("uktr"), typed.get("pcutr"), typed.get("strn"), typed.get("utrn_h"), params.get("ntnum", 1))
            if None not in needed and float(typed["strn"]) > 0 and float(typed["utrn_h"]) > 0 and float(params.get("ntnum", 1)) > 0:
                z_pu = float(typed["uktr"]) / 100.0
                r_pu = float(typed["pcutr"]) / (1000.0 * float(typed["strn"]))
                if r_pu <= z_pu:
                    z1 = complex(r_pu, math.sqrt(z_pu*z_pu-r_pu*r_pu)) * float(typed["utrn_h"])**2 / (float(typed["strn"]) * float(params.get("ntnum", 1)))
                    records.append(_entry(branch, "z1", "uktr, pcutr, strn, utrn_h and ntnum",
                                          {"real": z1.real, "imag": z1.imag}, "Ohm", "derived",
                                          formula="r_pu=pcutr/(1000*strn), z_pu=uktr/100, Zhv=Zpu*utrn_h^2/(strn*ntnum)"))
                else:
                    records.append(_entry(branch, "z1", "impedance derivation", None, "Ohm", "missing"))
            else:
                records.append(_entry(branch, "z1", "impedance derivation", None, "Ohm", "missing"))
            for field in ("z2", "z0", "zero_connection", "neutral_included", "tap_pu"):
                records.append(_entry(branch, field, "IEC connection and grounding derivation pending",
                                      None, "Ohm" if field.startswith("z") else "1", "missing"))
        elif cls == "ElmZpu":
            for key in ("Sn", "r_pu", "x_pu", "r_pu_ji", "x_pu_ji", "r0_pu", "x0_pu", "r0_pu_ji", "x0_pu_ji",
                        "uratio", "nphshift", "ag", "gi_pu", "bi_pu", "gj_pu", "bj_pu", "gi0_pu", "bi0_pu", "gj0_pu", "bj0_pu"):
                records.append(_entry(branch, key, f"params.{key}", params.get(key), _unit(branch, key),
                                      "available" if params.get(key) is not None else "missing"))
            phase = params.get("nphshift")
            records.append(_entry(branch, "nphshift_angle_deg", "params.nphshift * 30 deg",
                                  None if phase is None else float(phase) * 30.0, "deg",
                                  "derived" if phase is not None else "missing",
                                  original_unit=_unit(branch, "nphshift"), conversion="clock number * 30 deg"))
            records.append(_entry(branch, "sequence_mapping", "ElmZpu IEC convention pending", None, "1", "missing"))
    for branch in model.get("branches3", ()):
        params, typed = branch.get("params", {}), branch.get("type_params", {})
        for key in "hml":
            for field in ("strn3_" + key, "uktr3_" + key, "pcut3_" + key,
                          "pcutr3_" + key, "uktrr3_" + key, "utrn3_" + key,
                          "nt3ag_" + key, "du3tp_" + key, "n3tmn_" + key,
                          "n3tmx_" + key, "n3tp0_" + key, "ph3tr_" + key):
                records.append(_entry(branch, field, "type_params." + field, typed.get(field),
                                      _unit(branch, field, typed=True),
                                      "available" if typed.get(field) is not None else "missing"))
            tap_field = "n3tap_" + key
            records.append(_entry(branch, tap_field, "params." + tap_field, params.get(tap_field),
                                  _unit(branch, tap_field),
                                  "available" if params.get(tap_field) is not None else "missing"))
            clock = typed.get("nt3ag_" + key)
            records.append(_entry(branch, "nt3ag_" + key + "_angle_deg",
                                  "type_params.nt3ag_" + key + " * 30 deg",
                                  None if clock is None else float(clock) * 30.0, "deg",
                                  "derived" if clock is not None else "missing",
                                  original_unit=_unit(branch, "nt3ag_" + key, typed=True),
                                  conversion="clock number * 30 deg"))
        records.append(_entry(branch, "positive_sequence_star", "H-M, M-L and L-H pairs",
                              None, "pu", "available" if all(typed.get(field) is not None for field in
                              ("strn3_h", "strn3_m", "strn3_l", "uktr3_h", "uktr3_m", "uktr3_l",
                               "utrn3_h", "utrn3_m", "utrn3_l")) else "missing",
                              note="Each pair uses the smaller strn3 of its two windings. Copper losses pcut3 are required to derive resistance."))
    for source in model.get("gens", ()):
        params, typed, cls = source.get("params", {}), source.get("type_params", {}), source.get("class")
        if cls == "ElmSym":
            for key in ("xdss", "xds", "rstr", "sgn", "ugn", "cosn", "polepairs"):
                records.append(_entry(source, key, f"type_params.{key}", typed.get(key), _unit(source, key, typed=True),
                                      "available" if typed.get(key) is not None else "missing"))
            for key in ("ngnum", "qgini"):
                records.append(_entry(source, key, f"params.{key}", params.get(key), _unit(source, key),
                                      "available" if params.get(key) is not None else "missing"))
            if all(typed.get(key) is not None for key in ("xdss", "rstr", "sgn", "ugn")) and float(typed["sgn"]) > 0:
                z_base = float(typed["ugn"])**2 / float(typed["sgn"])
                records.extend((_entry(source, "z_base_ohm", "type_params.ugn^2 / type_params.sgn", z_base, "Ohm", "derived"),
                                _entry(source, "r_subtransient_ohm", "type_params.rstr * z_base_ohm",
                                       float(typed["rstr"]) * z_base, "Ohm", "derived"),
                                _entry(source, "x_subtransient_ohm", "type_params.xdss * z_base_ohm",
                                       float(typed["xdss"]) * z_base, "Ohm", "derived")))
                records.append(_entry(source, "z1", "rstr + j*xdss en base propia",
                                      {"real": float(typed["rstr"]) * z_base, "imag": float(typed["xdss"]) * z_base},
                                      "Ohm", "derived", note="Per physical unit, without K_G or accreditation as an SC equivalent"))
            else:
                records.append(_entry(source, "z1", "machine derivation", None, "Ohm", "missing"))
            for field in ("source_model", "z2", "z0", "grounding", "neutral_included"):
                records.append(_entry(source, field, "IEC machine adaptation pending", None, "1", "missing"))
        else:
            for key in ("r0", "x0", "r2", "x2", "sgini", "ngnum"):
                records.append(_entry(source, key, f"params.{key}", params.get(key), "pu",
                                      "available" if params.get(key) is not None else "missing"))
            for field in ("source_model", "z1", "z2", "z0"):
                records.append(_entry(source, field, "Equivalent source model was not exported", None, "ohm", "missing"))
    buses = [{"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
              "name": item.get("name", item["id"]), "un_kv": item.get("un_kv"),
              "source_path": "model.json#" + item["id"], "source_attribute": "un_kv",
              "status": "available" if item.get("un_kv") is not None else "missing"}
             for item in model.get("buses", ())]
    sources = []
    for item in model.get("gens", ()):
        typed = item.get("type_params", {})
        synchronous_ready = item.get("class") == "ElmSym" and all(
            typed.get(key) is not None for key in ("xdss", "rstr", "sgn", "ugn", "cosn"))
        if synchronous_ready:
            source_status, limited_status = "available", "not_applicable"
            reason = "Synchronous source available for dynamic boundary equivalent mode, not accredited as an SC equivalent"
        elif item.get("class") in ("ElmGenstat", "ElmPvsys"):
            source_status, limited_status = "not_implemented", "not_implemented"
            reason = "Current-limited converter injection model is missing and cannot be inferred from power flow"
        else:
            source_status, limited_status = "missing", "not_implemented"
            reason = "Short-circuit source model with provenance is missing"
        sources.append({"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                        "name": item.get("name", item["id"]), "class": item.get("class"), "bus_id": item.get("bus"),
                        "source_model_status": source_status, "current_limited_status": limited_status,
                        "reason": reason})
    if model_source_path is not None:
        model_sha256 = hashlib.sha256(Path(model_source_path).read_bytes()).hexdigest()
    else:
        model_sha256 = hashlib.sha256(json.dumps(model, ensure_ascii=False, sort_keys=True,
                                                 separators=(",", ":")).encode("utf-8")).hexdigest()
    for record in records:
        record["source_sha256"] = model_sha256
    for record in buses:
        record["source_sha256"] = model_sha256
    for record in sources:
        record["source_sha256"] = model_sha256
    return {"schema": "power_machine-sc-preflight-v1", "model_source": "model.json", "model_sha256": model_sha256,
            "verification": "not validated against PowerFactory", "records": records, "buses": buses, "sources": sources}


def write_preflight(model, destination, *, model_source_path=None):
    "Write a reproducible JSON inventory of the inspected supplementary data."
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(build_preflight(model, model_source_path=model_source_path), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return destination
