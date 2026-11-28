"""Cotejo offline IEEE39 IEC 2016, kA y grados, entradas REF de solo lectura.

Usa el modelo LF nativo ya entregado. Proyecta atributos SC de tipos REF,
no usa resultados de falla para parametrizar la red.
"""
from __future__ import annotations

import csv
import hashlib
import json
import math
from pathlib import Path
from ..resources import confined_output, output_root
from ..resources import readonly_path

from ..benchmarks.ieee39_ref import DEFAULT_REF_DIR
from .lf import WORKSPACE, number, read_csv
from ..shortcircuit import FaultSpec, ShortCircuitData, ShortCircuitOptions, run_shortcircuit

# Criterios fijados antes del cálculo, nunca se estiman de los residuos.
TOLERANCES = {"Ikss_relative": 0.005, "angle_deg": 0.5,
              "ip_relative": 0.01, "Ib_relative": 0.01,
              "contribution_fraction_bus_Ikss": 0.01, "kcl_relative": 1e-6}


def file_hash(path):
    """SHA256 por bloques, no vuelca archivos ni requiere cargarlos al contexto."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def project_objects(path, fields):
    """Extrae atributos requeridos sin volcar instantáneas completas al contexto."""
    return {o["path"]: {k: v["value"] for k, v in o["attributes"].items() if k in fields}
            for o in read_json(path)}


def build_supplement(model, ref_dir):
    """Datos por unidad propia, Ω, MVA y kV. IEC usa xd'' saturada.

El piso rstr=0,01 pu es comportamiento REF registrado en fault_B01.log.
Se registra como supuesto REF, separado de la resistencia ficticia de pico.
"""
    root = Path(ref_dir)
    types = project_objects(root / "modelo/types.json", {"sgn", "ugn", "cosn", "xdss", "xdsss", "rstr", "oltc"})
    gens = project_objects(root / "modelo/generators.json", {"pG"})
    elements, sources = {}, {}
    buses = {b["id"]: b for b in model["buses"]}

    def datum(value, unit, item, attr):
        if value is None:
            raise ValueError("Dato SC ausente: " + item["name"] + " " + attr)
        return {"value": value, "unit": unit, "source_path": item.get("type_id") or item["path"],
                "source_attribute": attr}

    for branch in model["branches"]:
        p, tp = branch["params"], branch["type_params"]
        if branch["class"] == "ElmLne":
            parameters = {"z1": datum({"real": p["R1"], "imag": p["X1"]}, "ohm", branch, "R1,X1: tipo por longitud")}
            record = {"parameters": parameters}
        else:
            sn, ur = tp["strn"], tp["utrn_h"]
            r_pu = tp["pcutr"] / (1000 * sn)
            x_pu = math.sqrt((tp["uktr"] / 100)**2 - r_pu**2)
            ratio = (tp["utrn_h"] / buses[branch["bus_from"]]["un_kv"]) / (tp["utrn_l"] / buses[branch["bus_to"]]["un_kv"])
            phase = math.radians(tp["nt2ag"] * 30)
            parameters = {
                "z1": datum({"real": r_pu*ur**2/sn, "imag": x_pu*ur**2/sn}, "ohm", branch, "uktr,pcutr,strn,utrn_h"),
                "sn_mva": datum(sn, "MVA", branch, "strn"),
                "ur_kv": datum(ur, "kV", branch, "utrn_h"),
                "vector_group": datum(tp["vecgrp"], "1", branch, "vecgrp"),
                "tap_pu": datum({"real": ratio*math.cos(phase), "imag": ratio*math.sin(phase)}, "pu", branch, "utrn_h/l,nt2ag, tap neutro IEC"),
            }
            record = {"parameters": parameters, "transformer_role": "network"}
            local = [g for g in model["gens"] if g["bus"] == branch["bus_to"] and buses[g["bus"]]["un_kv"] < 80]
            if len(local) == 1:
                if types[branch["type_id"]]["oltc"] != 0:
                    raise ValueError("Unidad con OLTC fuera de este cotejo")
                g = local[0]
                if g["n_units"] != p["ntnum"]:
                    raise ValueError("Unidad generadora con cantidades distintas")
                p_t = max(abs(tp["ntpmn"] - tp["nntap0"]), abs(tp["ntpmx"] - tp["nntap0"])) * tp["dutap"] / 100
                record.update(station_source_id=g["id"], station_bus_id=g["bus"],
                              station_p_t=p_t, station_p_g=gens[g["path"]]["pG"] / 100)
        elements[branch["id"]] = record
    for g in model["gens"]:
        tp = types[g["type_id"]]
        sn, ur, xd = tp["sgn"], tp["ugn"], tp["xdsss"]
        zbase_ohm = ur**2/sn
        r_pu = max(tp["rstr"], 0.01)
        sources[g["id"]] = {"kind": "synchronous", "parameters": {
            "x_d_subtransient_pu": datum(xd, "pu", g, "xdsss (IEC, no xdss RMS)"),
            "sn_mva": datum(sn, "MVA", g, "sgn por máquina"),
            "ur_kv": datum(ur, "kV", g, "ugn"),
            "r_generator": datum(r_pu*zbase_ohm, "ohm", g, "rstr, piso REF 0.01 pu según log B01"),
            "r_kappa_ohm": datum(0.05*xd*zbase_ohm, "ohm", g, "IEC R ficticia 0.05 X, Ur>1kV y Sn>=100MVA"),
            "cos_phi_rated": datum(tp["cosn"], "1", g, "cosn"),
            "sin_phi_sign": datum(1, "1", g, "IEC nominal sobreexcitado"),
            "ir_ka": datum(sn/(math.sqrt(3)*ur), "kA", g, "sgn/(sqrt(3)*ugn) por máquina"),
        }}
    return ShortCircuitData("power_machine-sc-data-v1", elements, sources,
        ({"code": "ref_stator_floor", "r_pu": 0.01, "evidence": "E39-CC-01/fault_B01.json: log"},))


def incident_currents(result, fault_bus):
    """Fasores fase a [kA] hacia la falla, fuentes + y ramas - por convención."""
    rows = {}
    for c in result.contributions:
        if c["kind"] == "source" and c["bus_id"] == fault_bus:
            rows[c["object_id"]] = c["phase_currents_ka"]["a"]
        elif c["kind"] == "branch":
            for t in c["terminals"].values():
                if t["bus_id"] == fault_bus:
                    rows[c["object_id"]] = -t["phase_currents_ka"]["a"]
    return rows


def kcl_check(result, fault_bus):
    currents = incident_currents(result, fault_bus)
    total = result.quantities["phase_currents_ka"]["a"]
    incoming = sum(currents.values(), 0j)
    error = abs(incoming-total)
    balances, scales = {}, {}
    for c in result.contributions:
        entries = [(c["bus_id"], c["phase_currents_ka"]["a"])] if c["kind"] == "source" else [
            (t["bus_id"], -t["phase_currents_ka"]["a"]) for t in c["terminals"].values()]
        for bus_id, current in entries:
            balances[bus_id] = balances.get(bus_id, 0j)+current
            scales[bus_id] = scales.get(bus_id, 0.0)+abs(current)
    balances[fault_bus] -= total
    nodal = {b: abs(v)/max(scales[b], 1e-30) for b, v in balances.items()}
    worst_bus = max(nodal, key=nodal.get)
    return {"incident_count": len(currents), "sum_re_kA": incoming.real, "sum_im_kA": incoming.imag,
            "sum_magnitudes_kA": sum(map(abs, currents.values())), "total_re_kA": total.real,
            "total_im_kA": total.imag, "residual_abs_kA": error, "residual_relative": error/abs(total),
            "max_nodal_residual_kA": max(map(abs, balances.values())), "max_nodal_relative": nodal[worst_bus],
            "worst_nodal_bus_id": worst_bus, "nodal_bus_count": len(balances),
            "pass": max(error/abs(total), nodal[worst_bus]) <= TOLERANCES["kcl_relative"]}


def calculate_faults(model, data, profile="iec_ref2016_c1_v1"):
    options = ShortCircuitOptions("iec60909", "maximum", frequency_hz=60, min_break_time_s=0.1, profile=profile)
    return {int(b["name"].removeprefix("Bus ")): run_shortcircuit(model, FaultSpec(b["id"], "3ph"), options, data)
            for b in model["buses"]}


def compare_bus(ref_entry, result, bus):
    """Errores relativos de magnitud, ángulo circular. R/X y kappa sin umbral inventado."""
    q = result.quantities
    current = (q.get("phase_currents_ka") or {}).get("a")
    values = {"Ikss_kA": q["Ik_initial_sym_ka"], "ip_kA": q["ip_peak_ka"], "Ib_kA": q["Ib_sym_ka"],
              "angle_deg": math.degrees(math.atan2(current.imag, current.real)) if current is not None else None,
              "RtoX": q.get("RtoX"), "kappa_effective_derived": q.get("kappa")}
    fault_bus = ref_entry.get("fault_bus", bus["id"])
    if str(fault_bus).isdigit():
        fault_bus = int(fault_bus)
    row = {"fault_bus": fault_bus, "object_id": bus.get("object_id", bus["id"]), "relative_path": bus.get("relative_path", bus.get("path")),
           "power_machine_status": result.status}
    for field, value in values.items():
        reference = number(ref_entry.get(field))
        delta = value-reference if value is not None and reference is not None else None
        if delta is not None and field == "angle_deg":
            delta = (delta+180) % 360-180
        row.update({"ref_"+field: reference, "power_machine_"+field: value, "delta_"+field: delta})
        if field != "angle_deg":
            row["relative_error_"+field] = abs(delta/reference) if delta is not None and reference != 0 else None
    for field, tolerance in (("Ikss_kA", TOLERANCES["Ikss_relative"]), ("ip_kA", TOLERANCES["ip_relative"]),
                             ("Ib_kA", TOLERANCES["Ib_relative"])):
        e = row["relative_error_"+field]
        row["pass_"+field] = e is not None and e <= tolerance
    row["pass_angle"] = row["delta_angle_deg"] is not None and abs(row["delta_angle_deg"]) <= TOLERANCES["angle_deg"]
    row["pass"] = all(row[k] for k in ("pass_Ikss_kA", "pass_ip_kA", "pass_Ib_kA", "pass_angle"))
    return row


def read_incident_reference(path):
    """CSV por filas, conserva solo columnas y extremos incidentes."""
    columns = ("fault_bus", "name", "class", "path", "terminal_bus", "Ikss_kA", "angle_deg",
               "ip_kA", "Ib_kA", "into_fault_re_kA", "into_fault_im_kA")
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["incident_to_fault"] == "True":
                yield {k: row[k] for k in columns}


def compare_contributions(model, runs, references, bus_rows):
    """Todos los incidentes, norma de fasor y módulos acotados por 1 % Ik de barra.

ip usa el kappa global de la falla. Ib por extremo es la envolvente global
Ib/Ik aplicada a la contribución, semántica observada en la exportación REF.
No se atribuye Ib por fuente como trayectoria física independiente.
"""
    paths = {i["path"]: i for group in ("branches", "gens") for i in model[group]}
    by_bus = {r["fault_bus"]: r for r in bus_rows}
    rows, seen = [], set()
    for ref_entry in references:
        fault_bus = int(ref_entry["fault_bus"])
        if int(ref_entry["terminal_bus"]) != fault_bus:
            raise ValueError("Extremo incidente distinto de la falla")
        item = paths[ref_entry["path"]]
        if (fault_bus, item["id"]) in seen:
            raise ValueError("Contribución REF duplicada")
        seen.add((fault_bus, item["id"]))
        bus_id = runs[fault_bus].fault["bus_id"]
        current = incident_currents(runs[fault_bus], bus_id)[item["id"]]
        re, im = number(ref_entry["into_fault_re_kA"]), number(ref_entry["into_fault_im_kA"])
        reference = complex(re, im) if re is not None and im is not None else None
        q = runs[fault_bus].quantities
        native_angle = math.degrees(math.atan2(current.imag, current.real)) + (180 if item["class"] != "ElmSym" else 0)
        angle_ref = number(ref_entry["angle_deg"])
        row = {"fault_bus": fault_bus, "name": ref_entry["name"], "terminal_bus": int(ref_entry["terminal_bus"]),
               "object_id": item["object_id"], "relative_path": item["relative_path"],
               "ref_into_fault_re_kA": re, "ref_into_fault_im_kA": im,
               "power_machine_into_fault_re_kA": current.real, "power_machine_into_fault_im_kA": current.imag,
               "phasor_error_kA": abs(current-reference) if reference is not None else None,
               "delta_angle_deg": (native_angle-angle_ref+180) % 360-180 if angle_ref is not None else None,
               "limit_kA": TOLERANCES["contribution_fraction_bus_Ikss"]*by_bus[fault_bus]["ref_Ikss_kA"]}
        for field, value in (("Ikss_kA", abs(current)),
                             ("ip_kA", math.sqrt(2)*q["kappa"]*abs(current)),
                             ("Ib_kA", abs(current)*q["Ib_sym_ka"]/q["Ik_initial_sym_ka"] if q["Ib_sym_ka"] is not None else None)):
            ref = number(ref_entry[field])
            delta = value-ref if value is not None and ref is not None else None
            row.update({"ref_"+field: ref, "power_machine_"+field: value, "delta_"+field: delta,
                        "pass_"+field: delta is not None and abs(delta) <= row["limit_kA"]})
        row["pass_phasor"] = row["phasor_error_kA"] is not None and row["phasor_error_kA"] <= row["limit_kA"]
        row["pass"] = all(row[k] for k in ("pass_phasor", "pass_Ikss_kA", "pass_ip_kA", "pass_Ib_kA"))
        rows.append(row)
    expected = {(b, i) for b, r in runs.items() for i in incident_currents(r, r.fault["bus_id"])}
    if seen != expected:
        raise ValueError("Cobertura incompleta de contribuciones incidentes REF/power_machine")
    return rows


def write_table(path, rows, *, output_dir=None):
    """Write a CSV confined to the configured or explicitly supplied output root."""
    base = Path(output_dir).expanduser().resolve() if output_dir is not None else output_root()
    target = confined_output(Path(path).expanduser(), base)
    if not rows:
        raise ValueError("Tabla vacía")
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def summarize(bus_rows, contribution_rows, kcl_rows):
    """Máximo y mediana, ausentes se cuentan y no pasan aceptación."""
    import statistics
    rows = []
    for field in ("Ikss_kA", "ip_kA", "Ib_kA", "RtoX", "kappa_effective_derived"):
        values = [r["relative_error_"+field] for r in bus_rows if r["relative_error_"+field] is not None]
        rows.append({"metric": field+"_relative", "maximum": max(values) if values else None,
                     "median": statistics.median(values) if values else None, "measured": len(values),
                     "passed": sum(r.get("pass_"+field, False) for r in bus_rows) if "pass_"+field in bus_rows[0] else None,
                     "total": len(bus_rows)})
    angles = [abs(r["delta_angle_deg"]) for r in bus_rows if r["delta_angle_deg"] is not None]
    rows.append({"metric": "angle_abs_deg", "maximum": max(angles), "median": statistics.median(angles),
                 "measured": len(angles), "passed": sum(r["pass_angle"] for r in bus_rows), "total": len(bus_rows)})
    for label, field in (("contribution_phasor_fraction_bus", "phasor_error_kA"),
                         ("contribution_ip_fraction_bus", "delta_ip_kA"), ("contribution_Ib_fraction_bus", "delta_Ib_kA")):
        values = [abs(r[field])/(r["limit_kA"]/TOLERANCES["contribution_fraction_bus_Ikss"]) for r in contribution_rows if r[field] is not None]
        rows.append({"metric": label, "maximum": max(values), "median": statistics.median(values),
                     "measured": len(values), "passed": sum(v <= 0.01 for v in values), "total": len(contribution_rows)})
    values = [r["residual_relative"] for r in kcl_rows]
    rows.append({"metric": "kcl_relative", "maximum": max(values), "median": statistics.median(values),
                 "measured": len(values), "passed": sum(r["pass"] for r in kcl_rows), "total": len(kcl_rows)})
    return rows


def run_comparisons(ref_dir=DEFAULT_REF_DIR, output_dir=None, *, model=None):
    """Compare 39 three-phase faults, KCL and B20 with correction enabled.

    An explicit output_dir selects the allowed output root. Otherwise use
    POWER_MACHINE_OUTPUT_DIR or the resources default. Every table remains
    confined to that root. The unavailable correction-off result stays null.
    """
    root = Path(ref_dir)
    allowed_root = Path(output_dir).expanduser().resolve() if output_dir is not None else output_root()
    target = confined_output(Path(output_dir).expanduser() if output_dir is not None else allowed_root / "comparacion_ref" / "cortocircuito", allowed_root)

    def write(path, rows):
        write_table(path, rows, output_dir=allowed_root)
    model = model if model is not None else read_json(root / "model.json")
    for relative, expected_hash in model["meta"]["sha256_inputs"].items():
        if file_hash(root / relative) != expected_hash:
            raise ValueError("Fuente distinta del modelo PFC-MODEL: " + relative)
    settings = read_json(root / "E39-CC-01/settings.json")
    required = {"iopt_mde": 1, "iec_pub": 2016, "iopt_cur": 0, "iopt_shc": "3psc", "iopt_peak": "C(1)"}
    if any(settings["actual"][k]["value"] != v for k, v in required.items()):
        raise ValueError("Configuración REF fuera del perfil soportado")
    if settings["requested"]["Ta"] != 0.1 or settings["requested"]["cfac"] != 1.1:
        raise ValueError("Tiempo/factor REF distinto del ensayo")
    data = build_supplement(model, root)
    runs = calculate_faults(model, data)
    bus_map = {int(b["name"].removeprefix("Bus ")): b for b in model["buses"]}
    references = read_csv(root / "E39-CC-01/fault_summary.csv")
    if {int(r["fault_bus"]) for r in references} != set(bus_map):
        raise ValueError("Referencia de barras incompleta")
    for result in runs.values():
        if result.quantities["Ik_initial_sym_ka"] is None:
            raise ValueError(str(result.diagnostics))
    buses = [compare_bus(r, runs[int(r["fault_bus"])], bus_map[int(r["fault_bus"])]) for r in references]
    contributions = compare_contributions(model, runs, read_incident_reference(root / "E39-CC-01/all_element_end_currents.csv"), buses)
    kcl = [{"fault_bus": b, "object_id": bus_map[b]["id"], "relative_path": bus_map[b]["relative_path"],
            **kcl_check(r, r.fault["bus_id"])} for b, r in runs.items()]
    worst = sorted(buses, key=lambda r: max(r["relative_error_Ikss_kA"]/0.005,
                   r["relative_error_ip_kA"]/0.01, r["relative_error_Ib_kA"]/0.01,
                   abs(r["delta_angle_deg"])/0.5), reverse=True)[:10]
    summary = summarize(buses, contributions, kcl)
    on = read_json(root / "E39-CC-03/correction_test.json")
    cc03 = compare_bus(on["correction_on"], runs[20], bus_map[20])
    cc03.update(ref_correction_off=None, ratio_on_off=None, coverage="Solo B20 ON, OFF pendiente")
    for name, rows in (("buses", buses), ("contributions", contributions), ("worst10", worst), ("summary", summary)):
        write(target / "E39-CC-01" / (name+".csv"), rows)
    write(target / "CC-02/kcl.csv", kcl)
    write(target / "CC-03/B20_on.csv", [cc03])
    write(target / "E39-CC-01/tolerances.csv", [{"metric": k, "tolerance": v} for k, v in TOLERANCES.items()])
    write(target / "E39-CC-01/options.csv", option_rows(settings))
    inputs = [readonly_path("cases", "ieee39_ref", "model.json")] + [root / r for r in (
        "modelo/types.json", "modelo/generators.json", "E39-CC-01/settings.json", "E39-CC-01/LEEME.txt",
        "E39-CC-01/fault_summary.csv", "E39-CC-01/all_element_end_currents.csv",
        "E39-CC-02/validation.json", "E39-CC-03/correction_audit.json", "E39-CC-03/correction_test.json")]
    sources = [Path(__file__).resolve()] + list((Path(__file__).resolve().parents[1] / "shortcircuit").glob("*.py"))
    write(target / "E39-CC-01/provenance.csv", [{"path": str(p.resolve()), "sha256": file_hash(p)} for p in inputs+sources])
    diagnostics = [{"fault_bus": b, **d} for b, r in runs.items() for d in r.diagnostics]
    write(target / "E39-CC-01/diagnostics.csv", diagnostics)
    decay = []
    by_id = {g["id"]: g for g in model["gens"]}
    for b, r in runs.items():
        for c in r.contributions:
            if c["kind"] == "source":
                g = by_id[c["object_id"]]
                ir = data.sources[g["id"]]["parameters"]["ir_ka"]["value"]
                ik_g = abs(c["phase_currents_ka"]["a"])/c["unit_count"]
                decay.append({"fault_bus": b, "object_id": g["id"], "relative_path": g["relative_path"],
                              "name": g["name"], "Ik_G_per_machine_kA": ik_g, "Ir_per_machine_kA": ir,
                              "ratio_Ik_G_Ir": ik_g/ir, "power_machine_mu": c["mu"], "ref_mu": None,
                              "ref_mu_status": "No exportado"})
    write(target / "E39-CC-01/decay_sources.csv", decay)
    return {"model": model, "data": data, "runs": runs, "buses": buses, "contributions": contributions,
            "kcl": kcl, "worst": worst, "summary": summary, "cc03": cc03}


def option_rows(settings):
    """Correspondencia de opciones REF, incluidos controles inactivos en IEC."""
    choices = {
        "iopt_mde": "iec60909, fuente equivalente de tensión en falla",
        "iec_pub": "IEC 60909-0:2016, perfil iec_ref2016_c1_v1",
        "iopt_cur": "maximum",
        "iopt_shc": "3ph, secuencia positiva",
        "iopt_peak": "C(1), red a 0.4*f_n con correcciones IEC",
        "iopt_tap": "posición neutra IEC, conserva relación nominal y grupo",
        "iIgnLoad": "No gobierna IEC, cargas ordinarias omitidas por el método",
        "iIgnLneCap": "No gobierna IEC, capacitancias de secuencia positiva omitidas",
        "iIgnShnt": "Magnetización omitida, no hay shunts independientes en el modelo IEEE39",
        "ildfinit": "No gobierna la fuente equivalente IEC, no usa tensiones LF",
        "iopt_preload": "No usa P/Q previos",
        "iBrkTime": "Global, min_break_time_s=0.1 desde Ta solicitado y LEEME",
        "iopt_cdef": "Tabla IEC 2016, cmax=1.1 en todas las barras >1kV",
        "maxgenvol": "Detección de unidades directamente conectadas, Un_G<80kV",
        "maxlenpsu": "Sin líneas entre generador y su trafo en IEEE39, búsqueda 0.1km no afecta",
        "iopt_prot": "Sin evaluación de protección",
        "iopt_brc": "No se comparan resultados m:max*, solo extremos del fallo individual",
        "Rf": "r_fault_ohm=0",
        "iEnhancedZf": "x_fault_ohm=0, falla sólida",
    }
    rows = [{"ref_option": k, "ref_value": settings["actual"][k]["value"], "power_machine_equivalent": v,
             "evidence": "E39-CC-01/settings.json actual y LEEME"} for k, v in choices.items()]
    rows += [{"ref_option": k, "ref_value": v, "power_machine_equivalent": text, "evidence": evidence}
             for k, v, text, evidence in (
                 ("Ta", 0.1, "Tiempo global de apertura 0.1s, no tiempo de despeje térmico", "settings.requested y LEEME, Ta no figura en actual"),
                 ("i_autopsu", 1, "Pares gen-LV/trafo detectados por topología, K_SO sin OLTC", "CC-03 correction_audit, metadata ComShc"),
                 ("K_T", "Calculado", "0.95*cmax/(1+0.6*x_T), x_T en base propia", "IEC 2016 ec.(12a), TechRef trafo p39"),
                 ("K_G", "Calculado", "Un/Ur*cmax/(1+xdsss*sin_phi_rated)", "IEC 2016 ec.(15), TechRef máquina p15"),
                 ("K_SO", "Calculado", "K_G/abs(t_nom_pu)^2*(1-p_T)/(1+p_G), falla LV usa K_G y K_T=1", "IEC 2016 ec.(24),(21), factores REF corroborados"),
                 ("xdsss", "0.20000000298 pu", "Reactancia saturada en base propia por máquina", "modelo/types.json, TechRef máquina sec3.2"),
                 ("rstr", "0 pu -> 0.01 pu", "Piso REF explícito, no se escribe al tipo original", "fault_B01.json log, comportamiento REF"),
                 ("Ib", "Nativo", "Red mallada, decremento complejo n*(1-mu)*Z_G*I_G^2/E_G referido a la falla", "TechRef_CommonResultVariables_ShortCircuit simulador de referencia (2024) sec2.1.1.1 p4, coeficientes mu de shortcircuit"),
             )]
    return rows


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref-dir", type=Path, default=DEFAULT_REF_DIR)
    parser.add_argument("--output-dir", type=Path, default=None)
    args = parser.parse_args()
    comparison = run_comparisons(args.ref_dir, args.output_dir)
    for row in comparison["summary"]:
        print(row)


def compare_shortcircuit(model, results, ref_results_dir):
    """Compara resultados SC por ID o ruta exacta con fault_summary.csv."""
    folder = Path(ref_results_dir)
    references = read_csv(folder / "fault_summary.csv")
    runs = list(results.values()) if isinstance(results, dict) else list(results)
    by_bus = {result.fault["bus_id"]: result for result in runs}
    buses = {bus["id"]: bus for bus in model["buses"]}
    rows = []
    for reference in references:
        identity = reference.get("bus_id") or reference.get("object_id") or reference.get("relative_path") or reference.get("path")
        if identity is None and str(reference.get("fault_bus", "")).isdigit():
            number = int(reference["fault_bus"])
            matches = [bus for bus in buses.values()
                       if bus.get("name", "").startswith("Bus ")
                       and bus["name"].removeprefix("Bus ").isdigit()
                       and int(bus["name"].removeprefix("Bus ")) == number]
        else:
            matches = [bus for bus in buses.values() if identity in (bus["id"], bus.get("relative_path"), bus.get("path"))]
        if len(matches) != 1:
            raise ValueError("La referencia SC requiere ID o ruta de barra inequívoca")
        bus = matches[0]
        if bus["id"] not in by_bus:
            raise ValueError("Falta el resultado SC de la barra " + bus["id"])
        rows.append(compare_bus(reference, by_bus[bus["id"]], bus))
    from ..shortcircuit.results import json_value
    return json_value(rows)
