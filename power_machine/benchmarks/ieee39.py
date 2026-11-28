"""Constructor IEEE 39 y adaptación mínima al flujo AC existente.

Base de sistema 100 MVA, tensión base 345 kV y frecuencia nominal 60 Hz.
P y Q de carga son consumo positivo, P y Q de generación son inyección
positiva. Las potencias de rama salen de cada extremo. No se importa ni
ejecuta el código fuente del caso, solo sus literales BUS, GEN y BR.
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
from pathlib import Path

from ..loadflow import run_loadflow as _engine_loadflow


from ..resources import resource_root, readonly_path, writable_root
WORKSPACE = resource_root()
DEFAULT_SOURCE = readonly_path("cases", "ieee39", "case39.py")
DEFAULT_MODEL = readonly_path("cases", "ieee39", "model.json")
S_BASE_MVA = 100.0
UN_KV = 345.0
F_NOM_HZ = 60.0
BUS_COLUMNS = ("bus", "type", "pd_mw", "qd_mvar", "area", "vm_pu", "va_deg")
GEN_COLUMNS = ("bus", "pg_mw", "qg_mvar", "qmax_mvar", "qmin_mvar", "vg_pu")
BR_COLUMNS = ("from_bus", "to_bus", "r_pu", "x_pu", "b_pu", "rate_a_mva", "tap")
SOURCE_NOTE = (
    "Datos del caso publicado MATPOWER case39, no del proyecto del simulador comercial de referencia. "
    "IDs y rutas de origen propios de MATPOWER, no identificadores del simulador comercial de referencia."
)


def _inside_workspace(path: str | Path) -> Path:
    """Resuelve rutas y rechaza lectura o escritura fuera del estudio."""
    path = Path(path).resolve()
    if not path.is_relative_to(WORKSPACE):
        raise ValueError("La ruta debe permanecer dentro de la carpeta de trabajo")
    return path


def read_case(source: str | Path = DEFAULT_SOURCE) -> dict:
    """Lee filas numeradas desde 1, con unidades dadas por *_COLUMNS.

    No ejecuta imports, funciones ni otras instrucciones del archivo fuente.
    Una fila incompleta o no finita produce error, nunca se rellena con cero.
    """
    path = _inside_workspace(source)
    content = path.read_bytes()
    literals = {}
    for node in ast.parse(content.decode("utf-8-sig"), filename=str(path)).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("BUS", "GEN", "BR"):
                    if target.id in literals:
                        raise ValueError(f"Tabla duplicada: {target.id}")
                    value = ast.literal_eval(node.value)
                    if not isinstance(value, str):
                        raise ValueError(f"{target.id} debe ser una cadena literal")
                    literals[target.id] = value
    tables = {}
    for name, columns, count in (("BUS", BUS_COLUMNS, 39),
                                  ("GEN", GEN_COLUMNS, 10),
                                  ("BR", BR_COLUMNS, 46)):
        if name not in literals:
            raise ValueError(f"Falta la tabla {name}")
        rows = []
        for number, line in enumerate(literals[name].splitlines(), 1):
            values = [float(value) for value in line.split()]
            if len(values) != len(columns) or not all(math.isfinite(v) for v in values):
                raise ValueError(f"{name}, fila {number}: datos incompletos o no finitos")
            rows.append(values)
        if len(rows) != count:
            raise ValueError(f"{name}: se esperaban {count} filas, hay {len(rows)}")
        tables[name] = rows
    buses = tables["BUS"]
    if [row[0] for row in buses] != list(range(1, 40)):
        raise ValueError("BUS debe enumerar las barras 1 a 39 en orden")
    if [int(row[0]) for row in buses if row[1] == 3] != [31]:
        raise ValueError("La referencia publicada debe ser la barra 31")
    gen_buses = [row[0] for row in tables["GEN"]]
    if len(set(gen_buses)) != 10 or set(gen_buses) != set(range(30, 40)):
        raise ValueError("GEN debe contener un generador en cada barra 30 a 39")
    for row in tables["GEN"]:
        if row[3] < row[4] or row[5] <= 0:
            raise ValueError("Consigna o límites de generador inválidos")
    pairs = set()
    for f, t, r, x, b, rate, tap in tables["BR"]:
        if f not in range(1, 40) or t not in range(1, 40) or f == t:
            raise ValueError("Extremos de rama inválidos")
        pair = tuple(sorted((f, t)))
        if pair in pairs:
            raise ValueError("Ramas paralelas requieren ampliar la convención de IDs")
        pairs.add(pair)
        if r < 0 or x <= 0 or rate <= 0 or tap < 0:
            raise ValueError("Impedancia, rateA o tap inválidos")
        if (tap != 0 or r == 0) and b != 0:
            raise ValueError("Transformador con carga capacitiva requiere otra adaptación")
    return {"tables": tables, "source": path.relative_to(WORKSPACE).as_posix(),
            "sha256": hashlib.sha256(content).hexdigest()}


def _entity(item_id, cls, table, number, columns, values, source):
    origin_path = f"{source}#{table}[{number}]"
    return {"id": item_id, "object_id": item_id, "class": cls, "name": item_id,
            "scope": "ieee39", "path": origin_path, "relative_path": origin_path,
            "origin": "matpower", "outserv": 0, "params": {}, "units": {},
            "type_params": {}, "type_units": {}, "type_class": None, "type_id": None,
            "source_row": {"table": table, "row_1based": number,
                           "values": dict(zip(columns, values))}}


def build_model(source: str | Path = DEFAULT_SOURCE) -> dict:
    """Convierte las tablas al esquema v1 sin usar Vm/Va como arranque.

    Líneas: Z_base = U_base²/S_base [ohm], R1 = r_pu*Z_base,
    X1 = x_pu*Z_base, B1 = b_pu/Z_base*1e6 [uS], susceptancia total.
    Transformadores: strn=rateA, r_equipo=r_pu*strn/S_base y lo mismo
    para x. uktr=100*|z_equipo| [%], pcutr=1000*r_equipo*strn [kW].
    Sin magnetización ni desfase. El tap está en el extremo desde.
    Los nombres HV/LV son puertos del esquema, ambos con base 345 kV.
    """
    case = read_case(source)
    tables = case["tables"]
    model = {"schema": "power_machine-model-v1", "meta": {
        "benchmark": "ieee39", "source": case["source"],
        "source_package": "MATPOWER case39", "source_note": SOURCE_NOTE,
        "source_sha256": case["sha256"],
        "sha256_inputs": {case["source"]: case["sha256"]},
        "s_base_mva": S_BASE_MVA, "f_nom_hz": F_NOM_HZ, "un_base_kv": UN_KV,
        "loadflow_profile": {"q_limits": False, "tap_adjustment": False,
                             "load_model": "constant_pq", "reference_bus": "B31",
                             "reference_gen": "G31", "reference_angle_deg": tables["BUS"][30][6]},
        "generator_base_note": "sgn=100 MVA solo para convertir límites Q, no potencia nominal física",
        "rating_note": "rateA conservado en MVA, carga calculada como max(|S_desde|, |S_hasta|)/rateA",
        "ref_comparison": "Sin comparación contra la referencia externa",
    }, **{key: [] for key in ("buses", "switches", "branches", "branches3", "shunts",
                              "loads", "gens", "controllers", "interface", "other_elements")},
        "warnings": [SOURCE_NOTE,
                     "Límites Q desactivados, el caso publicado viola Qmin en G37.",
                     "Solo flujo AC equilibrado, faltan datos dinámicos y de secuencia cero."]}
    for number, row in enumerate(tables["BUS"], 1):
        bus, kind, pd_mw, qd_mvar, area, vm_pu, va_deg = row
        bus_id = f"B{int(bus):02d}"
        item = _entity(bus_id, "ElmTerm", "BUS", number, BUS_COLUMNS, row, case["source"])
        item.update(un_kv=UN_KV, bus_type=int(kind), area=int(area),
                    published_vm_pu=vm_pu, published_va_deg=va_deg)
        item["params"] = {"uknom": UN_KV, "outserv": 0}
        item["units"] = {"uknom": "kV"}
        model["buses"].append(item)
        if pd_mw != 0 or qd_mvar != 0:
            load = _entity(f"LD{int(bus):02d}", "ElmLod", "BUS", number,
                           BUS_COLUMNS, row, case["source"])
            load.update(bus=bus_id, p_mw=pd_mw, q_mvar=qd_mvar, scale=1.0)
            load["params"] = {"plini": pd_mw, "qlini": qd_mvar,
                              "scale0": 1.0, "mode_inp": "PQ", "outserv": 0}
            load["units"] = {"plini": "MW", "qlini": "Mvar", "scale0": "1"}
            model["loads"].append(load)
    z_base_ohm = UN_KV ** 2 / S_BASE_MVA
    for number, row in enumerate(tables["BR"], 1):
        f, t, r_pu, x_pu, b_pu, rate_a_mva, tap = row
        transformer = tap != 0 or r_pu == 0
        item_id = f"{'T' if transformer else 'L'}{int(f):02d}-{int(t):02d}"
        item = _entity(item_id, "ElmTr2" if transformer else "ElmLne", "BR", number,
                       BR_COLUMNS, row, case["source"])
        item.update(bus_from=f"B{int(f):02d}", bus_to=f"B{int(t):02d}", rate_a_mva=rate_a_mva)
        if transformer:
            a = tap if tap != 0 else 1.0
            r_equipment_pu = r_pu * rate_a_mva / S_BASE_MVA
            x_equipment_pu = x_pu * rate_a_mva / S_BASE_MVA
            item["params"] = {"nntap": 1, "ntnum": 1, "ntrcn": 0, "outserv": 0}
            item["type_class"] = "TypTr2"
            item["type_id"] = f"{item_id}_type"
            item["type_params"] = {
                "strn": rate_a_mva, "utrn_h": UN_KV, "utrn_l": UN_KV,
                "uktr": 100 * math.hypot(r_equipment_pu, x_equipment_pu),
                "pcutr": 1000 * r_equipment_pu * rate_a_mva,
                "pfe": 0.0, "curmg": 0.0, "itrdr": 0.5, "itrdl": 0.5,
                "dutap": 100 * (a - 1), "nntap0": 0, "phitr": 0.0,
                "tap_side": 0, "nt2ag": 0.0,
            }
            item["type_units"] = {"strn": "MVA", "utrn_h": "kV", "utrn_l": "kV",
                                  "uktr": "%", "pcutr": "kW", "pfe": "kW",
                                  "curmg": "%", "itrdr": "1", "itrdl": "1",
                                  "dutap": "%", "phitr": "deg", "nt2ag": "*30deg"}
        else:
            item["params"] = {"R1": r_pu * z_base_ohm, "X1": x_pu * z_base_ohm,
                              "B1": b_pu / z_base_ohm * 1e6, "G1": 0.0,
                              "nlnum": 1, "f_nom_hz": F_NOM_HZ, "outserv": 0}
            item["units"] = {"R1": "Ohm", "X1": "Ohm", "B1": "uS", "G1": "uS",
                             "nlnum": "1", "f_nom_hz": "Hz"}
        model["branches"].append(item)
    for number, row in enumerate(tables["GEN"], 1):
        bus, pg_mw, qg_mvar, qmax_mvar, qmin_mvar, vg_pu = row
        gen_id = f"G{int(bus):02d}"
        item = _entity(gen_id, "ElmSym", "GEN", number, GEN_COLUMNS, row, case["source"])
        item.update(bus=f"B{int(bus):02d}", p_mw=pg_mw, q_mvar=qg_mvar,
                    v_set_pu=vg_pu, mode="constv", ref=bus == 31, n_units=1,
                    s_rated_mva=None, q_min_mvar=qmin_mvar, q_max_mvar=qmax_mvar)
        item["params"] = {"pgini": pg_mw, "qgini": qg_mvar, "ngnum": 1, "scale0": 1.0,
                          "usetp": vg_pu, "av_mode": "constv", "ip_ctrl": int(bus == 31),
                          "q_min": qmin_mvar / S_BASE_MVA,
                          "q_max": qmax_mvar / S_BASE_MVA, "outserv": 0}
        item["units"] = {"pgini": "MW", "qgini": "Mvar", "usetp": "p.u.",
                         "q_min": "p.u.", "q_max": "p.u.", "scale0": "1", "ngnum": "1"}
        item.update(type_class="TypSym", type_id=f"{gen_id}_type",
                    type_params={"sgn": S_BASE_MVA}, type_units={"sgn": "MVA"})
        model["gens"].append(item)
    return model


def write_model(output: str | Path | None = None, source: str | Path = DEFAULT_SOURCE) -> dict:
    """Escribe JSON reproducible UTF-8 dentro de la carpeta del estudio."""
    path = Path(output).expanduser().resolve() if output is not None else writable_root() / "ieee39" / "model.json"
    model = build_model(source)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(model, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                    encoding="utf-8", newline="\n")
    return model


def run_loadflow(model: dict | None = None, *, outages: tuple[str, ...] = ()) -> dict:
    """Usa Newton existente, Q libre, taps fijos y cargas PQ constantes.

    Una contingencia retira toda la rama y su carga capacitiva, mediante
    outserv=1. rateA se evalúa en MVA en ambos extremos, no por corriente.
    The benchmark uses only its documented case data.
    """
    if model is None:
        model = build_model()
    if not str(model.get("schema", "")).endswith("-model-v1") or model.get("meta", {}).get("benchmark") != "ieee39":
        raise ValueError("Se requiere el modelo IEEE39 en esquema power_machine-model-v1")
    if [g["id"] for g in model["gens"] if g.get("ref")] != ["G31"]:
        raise ValueError("IEEE39 requiere G31 como única referencia")
    branches = {row["id"]: row for row in model["branches"]}
    if set(outages) - branches.keys():
        raise ValueError(f"Ramas de contingencia desconocidas: {sorted(set(outages) - branches.keys())}")
    scenario = {"outserv": {item_id: 1 for item_id in outages}} if outages else None
    result = _engine_loadflow(model, scenario=scenario, options={
        "q_limits": False, "missing": "error", "tolerance_pu": 1e-10,
        "max_iter": 30, "warm_start": False,
    })
    # El solver fija la referencia angular a cero. La fuente actual también
    # usa Va31=0, el desplazamiento queda explícito para no alinear a mano.
    offset_deg = model["meta"]["loadflow_profile"]["reference_angle_deg"]
    for bus in result["buses"]:
        if bus["angle_deg"] is not None:
            bus["angle_deg"] += offset_deg
    for row in result["branches"]:
        rate_a_mva = branches[row["id"]]["rate_a_mva"]
        row["rate_a_mva"] = rate_a_mva
        row["loading_rate_a_pct"] = None
        if row.get("p_from_mw") is not None and row.get("p_to_mw") is not None:
            s_from_mva = math.hypot(row["p_from_mw"], row["q_from_mvar"])
            s_to_mva = math.hypot(row["p_to_mw"], row["q_to_mvar"])
            row.update(s_from_mva=s_from_mva, s_to_mva=s_to_mva,
                       loading_rate_a_pct=100 * max(s_from_mva, s_to_mva) / rate_a_mva)
    result["loadflow_profile"] = dict(model["meta"]["loadflow_profile"])
    result["outages"] = list(outages)
    return result


def summarize(model: dict, result: dict) -> dict:
    """Resumen en MW, Mvar, p.u., grados y porcentaje respecto de rateA."""
    published = {bus["id"]: bus for bus in model["buses"]}
    buses = [b for b in result["buses"] if b["u_pu"] is not None]
    branches = [b for b in result["branches"] if b["loading_rate_a_pct"] is not None]
    slack = next(g for g in result["gens"] if g["id"] == "G31")
    maximum = max(branches, key=lambda b: b["loading_rate_a_pct"], default=None)
    minimum_bus = min(buses, key=lambda b: b["u_pu"], default=None)
    maximum_bus = max(buses, key=lambda b: b["u_pu"], default=None)
    base_case = not result["outages"]
    return {"converged": result["converged"], "iterations": result["iterations"],
            "max_mismatch_mva": result["max_mismatch_mva"],
            "loss_p_mw": result["totals"]["branch_loss_p_mw"],
            "slack_p_mw": slack["p_mw"], "slack_q_mvar": slack["q_mvar"],
            "voltage_min_pu": minimum_bus["u_pu"] if minimum_bus else None,
            "voltage_min_bus": minimum_bus["id"] if minimum_bus else None,
            "voltage_max_pu": maximum_bus["u_pu"] if maximum_bus else None,
            "voltage_max_bus": maximum_bus["id"] if maximum_bus else None,
            "max_loading_rate_a_pct": maximum["loading_rate_a_pct"] if maximum else None,
            "max_loading_branch": maximum["id"] if maximum else None,
            "branches_over_100_pct": sum(b["loading_rate_a_pct"] > 100 for b in branches),
            "max_voltage_error_pu": max((abs(b["u_pu"] - published[b["id"]]["published_vm_pu"])
                                         for b in buses), default=None) if base_case else None,
            "max_angle_error_deg": max((abs(b["angle_deg"] - published[b["id"]]["published_va_deg"])
                                        for b in buses), default=None) if base_case else None}
