"""IEEE39 nativo REF exportado, LF de secuencia positiva a 100 MVA y 60 Hz.

Lectura offline, sin importar simulador comercial de referencia. P/Q de carga son consumo positivo,
generación positiva inyectada y flujos positivos saliendo de cada extremo.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from ..loadflow import run_loadflow
from power_machine.refcompare.lf import read_csv, number

from ..resources import resource_root, readonly_path, writable_root
WORKSPACE = resource_root()
DEFAULT_REF_DIR = readonly_path("cases", "ieee39_ref")
LDF_OPTIONS = {"q_limits": False, "missing": "error", "tolerance_pu": 1e-10,
               "max_iter": 40, "warm_start": False}


def build_rms_case(ref_dir=DEFAULT_REF_DIR, *, step_s=.001, stage="full", avr_profile="dyn_a"):
    """Construye DAE cerrado para cases/ieee39_ref con perfil AVR explícito.

    dyn_a conserva la curva cuadrática. ref_identificado usa la exponencial
    y xr congelado con Tr=0, sin modificar parámetros ni el benchmark LF.
    """
    from ..dynamics.ieee39_native import IEEE39NativeRMS
    return IEEE39NativeRMS(ref_dir, step_s=step_s, stage=stage, avr_profile=avr_profile)


FIELDS = {
    "buses": "uknom outserv",
    "lines": "bus1 bus2 typ_id outserv R1 X1 B1 C1 G1 dline nlnum fline i_dist i_model",
    "transformers": "bushv buslv typ_id outserv nntap ntrcn ntnum",
    "generators": "bus1 typ_id outserv pgini qgini usetp ngnum ip_ctrl av_mode scale0",
    "loads": "bus1 typ_id outserv plini qlini scale0",
    "types": "rline xline bline cline gline frnom sline strn utrn_h utrn_l uktr pcutr curmg pfe itrdr itrdl dutap nntap0 phitr tap_side nt2ag ntpmn ntpmx vecgrp sgn",
}


def _read_objects(path, fields):
    """Proyecta solo campos requeridos, no imprime ni conserva atributos ajenos."""
    objects = json.loads(path.read_text(encoding="utf-8-sig"))
    return [{**{k: obj[k] for k in ("name", "cls", "path")},
             "attributes": {k: a for k, a in obj["attributes"].items() if k in fields}}
            for obj in objects]


def _entity(obj, types):
    path = obj["path"]
    relative = path.split(".IntPrj\\", 1)[1]
    key = "pf_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:20]
    attrs = obj["attributes"]
    values = {k: a["value"] for k, a in attrs.items() if a["value"] is not None}
    units = {k: a["unit"] for k, a in attrs.items() if k in values}
    typ = types.get(values.get("typ_id"))
    tp = {k: a["value"] for k, a in typ["attributes"].items()
          if a["value"] is not None} if typ else {}
    return {"id": key, "object_id": key, "name": obj["name"], "class": obj["cls"],
            "scope": "ieee39_ref", "path": path, "relative_path": relative,
            "outserv": values.get("outserv", 0), "params": values, "units": units,
            "type_params": tp,
            "type_units": {k: a["unit"] for k, a in typ["attributes"].items() if k in tp} if typ else {},
            "type_class": typ["cls"] if typ else None, "type_id": typ["path"] if typ else None,
            "provenance": {"params": "modelo: " + path, "type_params": typ["path"] if typ else None}}


def build_model(ref_dir=DEFAULT_REF_DIR):
    """Construye v1 desde rutas REF, cargas efectivas CSV y tipos nativos.

    R/X totales [ohm], G/B totales [uS]. Tipos por km multiplicados por dline
    [km], cotejados con totales exportados. No usa tensiones resueltas para
    inicializar Newton. Tap y unidades paralelas se conservan sin ajustar.
    """
    # La identidad de origen conserva el localizador del usuario. Resolver un
    # alias para leer sus archivos no debe reescribir esa procedencia histórica.
    source_identity = str(Path(ref_dir).expanduser().absolute())
    ref_dir = Path(ref_dir).resolve()
    objects = {g: _read_objects(ref_dir / "modelo" / (g + ".json"), fields.split())
               for g, fields in FIELDS.items()}
    types = {obj["path"]: obj for obj in objects["types"]}
    model = {"schema": "power_machine-model-v1", "meta": {
        "benchmark": "ieee39_ref", "source_package": source_identity,
        "s_base_mva": 100.0, "f_nom_hz": 60.0,
        "sha256_inputs": {f"modelo/{g}.json": hashlib.sha256(
            (ref_dir / "modelo" / (g + ".json")).read_bytes()).hexdigest() for g in FIELDS},
        "loadflow_profile": {"q_limits": False, "tap_adjustment": False,
                             "load_model": "constant_pq"}},
        **{k: [] for k in ("buses", "branches", "branches3", "switches", "gens",
                          "loads", "shunts", "controllers", "interface")},
        "warnings": ["Modelo exclusivo LF, no incluye dinámica."]}
    terminals = {}
    for obj in objects["buses"]:
        item = _entity(obj, types)
        item.update(un_kv=item["params"]["uknom"])
        item["provenance"]["un_kv"] = "ElmTerm.uknom [kV]"
        terminals[obj["path"]] = item["id"]
        model["buses"].append(item)

    def bus(cubicle):
        return terminals[cubicle.rsplit("\\", 1)[0]]

    for group in ("lines", "transformers"):
        for obj in objects[group]:
            item = _entity(obj, types)
            p, tp = item["params"], item["type_params"]
            ports = ("bus1", "bus2") if group == "lines" else ("bushv", "buslv")
            item.update(bus_from=bus(p[ports[0]]), bus_to=bus(p[ports[1]]))
            item["provenance"]["ports"] = " / ".join(ports) + " -> StaCubic -> ElmTerm"
            if group == "lines":
                if p["i_dist"] != 0:
                    raise ValueError("Línea distribuida no soportada: " + obj["name"])
                checks = {}
                for total, per_km in (("R1", "rline"), ("X1", "xline"), ("B1", "bline"), ("G1", "gline")):
                    effective = tp[per_km] * p["dline"]
                    exported = p.get(total)
                    if exported is not None and not math.isclose(effective, exported, rel_tol=2e-6, abs_tol=1e-6):
                        raise ValueError(f"{obj['name']}: {total} no coincide con tipo por longitud")
                    checks[total] = {"exported": exported, "type_times_length": effective,
                                     "delta": effective - exported if exported is not None else None}
                    p[total] = effective
                    item["provenance"][total] = f"TypLne.{per_km} * ElmLne.dline, cotejado con ElmLne.{total}"
                p["f_nom_hz"] = 60.0
                if "C1" in p:
                    b_from_c_us = 2 * math.pi * 60.0 * p["C1"]
                    if not math.isclose(p["B1"], b_from_c_us, rel_tol=2e-6, abs_tol=1e-6):
                        raise ValueError("B1 y C1 a 60 Hz incompatibles: " + obj["name"])
                    checks["B1_from_C1"] = {"b_us": b_from_c_us, "delta_us": b_from_c_us - p["B1"]}
                item["units"].update(R1="Ohm", X1="Ohm", B1="uS", G1="uS", f_nom_hz="Hz")
                item["line_parameter_checks"] = checks
            else:
                item["provenance"]["transformer"] = "TypTr2: strn[MVA], uktr[%], pcutr[kW], utrn_h/l[kV], vecgrp, nt2ag*30[deg], dutap[%], tap_side, nntap0. ElmTr2: nntap fijo, ntnum"
            model["branches"].append(item)
    effective_loads = {r["path"]: r for r in read_csv(ref_dir / "E39-LF-01/loads.csv")}
    references = {r["path"]: int(r["reference"]) for r in read_csv(ref_dir / "E39-LF-01/generators.csv")}
    for obj in objects["loads"]:
        item = _entity(obj, types)
        row = effective_loads[obj["path"]]
        p_mw, q_mvar = number(row["P_MW"]), number(row["Q_Mvar"])
        if p_mw is None or q_mvar is None:
            raise ValueError("Carga sin potencia efectiva: " + obj["name"])
        item["native_params"] = dict(item["params"])
        item["params"].update(plini=p_mw, qlini=q_mvar, scale0=1.0)
        item.update(bus=bus(item["params"]["bus1"]), p_mw=p_mw, q_mvar=q_mvar, scale=1.0)
        item["provenance"]["load_pq"] = "E39-LF-01/loads.csv: P_MW, Q_Mvar efectivos, scale0 normalizado a 1, originales en native_params"
        model["loads"].append(item)
    for obj in objects["generators"]:
        item = _entity(obj, types)
        p = item["params"]
        item.update(bus=bus(p["bus1"]), p_mw=p["pgini"] * p["ngnum"],
                    q_mvar=p["qgini"] * p["ngnum"], v_set_pu=p["usetp"],
                    mode=p["av_mode"], ref=bool(references[obj["path"]]), n_units=p["ngnum"],
                    s_rated_mva=item["type_params"]["sgn"] * p["ngnum"],
                    q_min_mvar=None, q_max_mvar=None)
        item["provenance"]["generator"] = "ElmSym.pgini/qgini * ngnum, usetp, av_mode, referencia desde E39-LF-01/generators.csv.reference"
        model["gens"].append(item)
    if len([g for g in model["gens"] if g["ref"]]) != 1:
        raise ValueError("Se requiere una referencia única")
    for name in ("loads", "generators"):
        rel = f"E39-LF-01/{name}.csv"
        model["meta"]["sha256_inputs"][rel] = hashlib.sha256((ref_dir / rel).read_bytes()).hexdigest()
    return model


def write_model(ref_dir=DEFAULT_REF_DIR, *, output_dir=None):
    """Escribe el modelo construido en la carpeta de resultados indicada."""
    model = build_model(ref_dir)
    target = Path(output_dir) if output_dir is not None else writable_root() / "ieee39_ref"
    target.mkdir(parents=True, exist_ok=True)
    (target / "model.json").write_text(json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    lines = ["IEEE39 nativo del simulador comercial de referencia (2024), 100 MVA, 60 Hz.",
             "Fuente de solo lectura: " + str(Path(ref_dir).resolve()),
             "P/Q de carga positivos consumidos, generación positiva inyectada.",
             "Flujos de rama positivos saliendo del extremo, ángulos en grados.",
             "IDs deterministas desde la ruta relativa al proyecto REF.",
             "Supuestos: secuencia positiva, pi concentrado, cargas PQ constantes.",
             "Sin límites Q, sin ajuste automático de taps, sin promoción de referencia.",
             "Atributo outserv ausente implica en servicio en este inventario LF.",
             "Escala de generación ausente implica 1, pgini se multiplica por ngnum.",
             "B1 [uS] también se verifica contra 2*pi*60*C1 [uF].",
             "Los totales de línea redondeados por REF se cotejan con tipo por longitud.",
             "Se utilizan los productos de tipo por longitud sin redondeo intermedio.",
             "Controladores y composites dinámicos no intervienen en este LF.",
             "No se afirma validación dinámica ni paridad total.", "", "Trazabilidad por elemento:"]
    for group in ("buses", "branches", "loads", "gens"):
        for item in model[group]:
            lines.append(item["name"] + " | " + item["relative_path"])
            lines.extend("  " + k + ": " + str(v) for k, v in item["provenance"].items())
            lines.extend(f"  params.{k} [{item['units'].get(k, '')}]: {v}" for k, v in item["params"].items())
            lines.extend(f"  type_params.{k} [{item['type_units'].get(k, '')}]: {v}" for k, v in item["type_params"].items())
    (target / "LEEME.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return model
