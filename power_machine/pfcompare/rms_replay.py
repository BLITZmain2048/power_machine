"""Cotejo RMS offline IEEE39 PF, con frontera explícita de representabilidad.

No ejecuta PowerFactory. Inicializa diez máquinas nativas desde el LF PF.
El caso completo se rechaza si faltan controles o cargas. El cotejo parcial
integra flujos de rotor y velocidad absoluta de G39, con U, ángulo de barra y
ángulo relativo del rotor impuestos por PF. P, Q, velocidad y excitación son
predicciones independientes. Esta reproducción de un subsistema NO es una
simulación de la red IEEE39 ni una validación de su respuesta a contingencias.
"""
from __future__ import annotations
from power_machine.resources import output_root
from power_machine.resources import required_readonly_path

import argparse
import copy
import csv
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np

from ..benchmarks.ieee39_pf import DEFAULT_PF_DIR, WORKSPACE, LDF_OPTIONS
from ..dynamics.machine_init import initialize_machine
from ..dynamics.network import DynamicNetwork
from ..dynamics.synchronous import SynchronousMachine
from ..loadflow import _stamp, run_loadflow

CASES = ("E39-DIN-01", "E39-DIN-02", "E39-DIN-03")
LABEL = "comparación parcial por diferencia de modelo"
MAX_ERROR_LIMIT = 0.02
ANGLE_PEAK_LIMIT = 0.01
INITIAL_LIMITS = {"p.u.": 1e-8, "rad": 1e-8, "deg": 1e-6, "Hz": 1e-8, "MW": 1e-5, "Mvar": 1e-5}
MODEL_FILE = required_readonly_path("cases", "ieee39_pf", "model.json")
OUTPUT_DIR = output_root() / "comparison_results/ieee39_pf"
MACHINE_FIELDS = """sgn ugn cosn h tag iner_inp iturbo model_inp i_exactConv
xd xq xds xqs xdss xqss tds0 tqs0 tdss0 tqss0 tds tqs tdss tqss xl xrl xrlq
rstr dpu dkd dpe isat isatSalient iusesat sg10 sg12 sg10q sg12q satv satse
satvq satseq i_speedVar i_speedD""".split()
LOAD_FIELDS = """loddy cloddy i_nln i_pure i_csrc aP bP cP aQ bQ cQ kpu0
kpu1 kpu kqu0 kqu1 kqu kpf kqf t1 tpu tqu tpf tqf udmin udmax""".split()
EVENT_FIELDS = "time outserv p_target i_shc R_f X_f shcLocation i_switch dP dQ iopt_type".split()
INC_FIELDS = """dtgrd dtout tstart syncperiod syncresult iopt_adapt iopt_sample
iopt_sim iopt_net iopt_lt alpha_rms iopt_coiref iopt_locref order""".split()
PREDICTED = ("G39_speed_pu", "G39_P_MW", "G39_Q_Mvar", "G39_excitation_pu")
ENGINE_COVERAGE = {
    "SynchronousMachine": "flujos de rotor, 6 estados turbo o 5 salientes, sin modelo clásico impuesto",
    "machine_initialization": "LF, bases sgn/ugn/ngnum, saturación isat e i_speedVar",
    "ControlledSynchronousMachine": "AVR y governor con puertos expl?citos del caso",
    "IEEET1_IEEEG1_IEEEG3": "sin acoplamiento genérico del marco IEEE39",
    "DynamicCatalog": "requiere export pf-dynamic-v1, parnam, ecuaciones BlkDef, límites y conexiones",
    "PSS_CONV": "outserv=1 en las nueve instancias, no necesita ejecución en estos ensayos",
    "RMSLoad": "corrección estática sin t1, faltan alias kpu1/kqu1 y régimen de baja tensión",
    "network": "DynamicNetwork Z coincide solo en LF, no se usa para simular los ensayos",
    "integrator": "DAESolver trapezoidal disponible, no conectado al caso completo por huecos de modelo",
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def identity(path):
    """ID determinista igual al PFC-MODEL y ruta relativa al IntPrj."""
    relative = path.split(".IntPrj\\", 1)[1]
    return {"object_id": "pf_" + hashlib.sha256(relative.encode()).hexdigest()[:20],
            "relative_path": relative}


def projected_fields(obj, names):
    fields = obj.get("attributes", obj.get("fields", {}))
    return {key: fields[key] for key in names if key in fields}


def values(fields):
    """Conserva ausencias como ausencias, nunca convierte NULL en cero."""
    return {k: v["value"] for k, v in fields.items() if v["value"] is not None}


def load_reference(case_dir):
    """Resuelve columnas por manifiesto Y cabeceras, no por nombre supuesto.

    ComRes usa una columna inicial de tiempo. ``column`` del manifiesto es
    índice de ElmRes, sin esa columna. Vacíos se conservan como NaN internos.
    Retorna segundos y señales en las unidades literales de ComRes.
    """
    case_dir = Path(case_dir)
    channels = read_json(case_dir / "channel_units.json")
    with (case_dir / "signals_native.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        objects, variables = next(reader), next(reader)
        if variables[0] != "b:tnow in s":
            raise ValueError("Tiempo ComRes sin unidad segundos")
        if len(channels) != len(variables)-1 or len(objects) != len(variables):
            raise ValueError("Manifiesto de canales incompleto")
        columns = []
        for channel in channels:
            index = int(channel["column"])+1
            variable, unit = variables[index].split(" in ", 1)
            if objects[index] != channel["object"]["name"] or variable != channel["variable"]:
                raise ValueError("Cabecera y canal no corresponden: " + channel["signal"])
            channel.update(identity(channel["object"]["path"]), unit=unit)
            columns.append(index)
        if len(set(columns)) != len(columns):
            raise ValueError("Columnas duplicadas")
        times, rows = [], []
        for row in reader:
            if not row:
                continue
            if len(row) != len(variables):
                raise ValueError("Fila ComRes incompleta")
            times.append(float(row[0]))
            rows.append([float(row[i]) if row[i].strip() else np.nan for i in columns])
    time_s = np.asarray(times)
    if len(time_s) < 2 or np.any(np.diff(time_s) <= 0):
        raise ValueError("Calendario ComRes no creciente")
    matrix = np.asarray(rows)
    if np.isinf(matrix).any():
        raise ValueError("Referencia con infinitos")
    return time_s, {c["signal"]: matrix[:, i] for i, c in enumerate(channels)}, channels


def native_inventory(pf_dir):
    """Extrae datos y procedencia campo por campo sin copiar JSON voluminosos."""
    pf_dir = Path(pf_dir)
    types = {}
    for obj in read_json(pf_dir / "modelo/types.json"):
        names = MACHINE_FIELDS if obj["cls"] == "TypSym" else LOAD_FIELDS
        if obj["cls"] in ("TypSym", "TypLod"):
            types[obj["path"]] = {**identity(obj["path"]),
                "path": obj["path"], "name": obj["name"], "class": obj["cls"],
                "fields": projected_fields(obj, names)}
    controllers = []
    for obj in read_json(pf_dir / "modelo/controllers.json"):
        controller = {**identity(obj["path"]), "name": obj["name"], "path": obj["path"],
                      "class": obj["cls"], "fields": projected_fields(obj, ("params", "typ_id", "outserv")),
                      "type_fields": obj["type_fields"]}
        params = controller["fields"]["params"]["value"]
        declarations = obj["type_fields"]["sParams"]["value"]
        names = [n.strip() for line in declarations for n in line.split(",") if n.strip()]
        controller["parameter_names_declared"] = names
        controller["parameter_count"] = len(params)
        controller["unnamed_tail_count"] = len(params)-len(names)
        # Los valores finales podrían ser límites, no se inventan sus nombres.
        controller["execution_status"] = ("fuera de servicio" if values(controller["fields"])["outserv"]
            else "no representable, faltan grafos BlkDef y nombres de límites")
        controllers.append(controller)
    composites = []
    for obj in read_json(pf_dir / "modelo/composites.json"):
        composites.append({**identity(obj["path"]), "path": obj["path"],
                           "fields": projected_fields(obj, ("pelm", "typ_id", "outserv"))})
    generator_attributes = {obj["path"]: projected_fields(obj,
        ("ngnum", "ip_ctrl", "efdBaseRatio", "ifdBase", "addmt", "typ_id", "bus1"))
        for obj in read_json(pf_dir / "modelo/generators.json")}
    trials = {}
    for case in CASES:
        directory = pf_dir / case
        config = read_json(directory / "configuration.json")
        options = read_json(directory / "effective_command_options.json")
        report = read_json(directory / "report.json")
        trials[case] = {"assessment": read_json(directory / "assessment.json"),
            "commands": {"ComInc": {k: options["ComInc"][k] for k in INC_FIELDS if k in options["ComInc"]},
                         "ComSim": {k: options["ComSim"][k] for k in ("tstop",) if k in options["ComSim"]}},
            "events": [{**identity(e["path"]), "path": e["path"], "class": e["cls"],
                        "name": e["name"], "fields": projected_fields(e, EVENT_FIELDS)} for e in config["events"]],
            "execution_codes": {k: report[k] for k in ("ldf_code", "inc_code", "sim_code", "is_sim_valid", "export_code")},
            "fault_interpretation": config.get("fault_interpretation")}
    return {"types": types, "controllers": controllers, "composites": composites,
            "generator_attributes": generator_attributes, "trials": trials}


from ..dynamics.native_case import NativeDynamicCase

def first_peak(time_s, signal, event_s=1.0):
    """Primer extremo de excursión absoluto, sin contar el salto del evento."""
    t, y = np.asarray(time_s), np.asarray(signal)
    valid = np.isfinite(y) & (t > event_s+0.01000001)
    indexes = np.flatnonzero(valid)
    if len(indexes) < 3:
        return None
    span = float(np.ptp(y[indexes]))
    if span < 1e-10:
        return None
    for i in indexes[1:-1]:
        if not (valid[i-1] and valid[i+1]):
            continue
        left, right = y[i]-y[i-1], y[i+1]-y[i]
        if left*right < 0 and min(abs(left), abs(right)) > span*1e-6:
            # Ajuste parabólico de tres muestras, restringido a medio paso.
            offset = .5*(y[i-1]-y[i+1])/(y[i-1]-2*y[i]+y[i+1])
            return float(t[i]+np.clip(offset, -.5, .5)*(t[i+1]-t[i]))
    return None


def dominant_mode(time_s, signal):
    """Estimación informativa por pencil matricial, ventana 3..10 s.

    Elimina tendencia lineal, usa hasta 6 polos y selección por amplitud.
    Rechaza ajuste >25 %, dominancia <60 % o menos de dos ciclos. No llama
    'modo del sistema' al estimador de una sola señal ni fuerza zeta=0.
    """
    t, y = np.asarray(time_s), np.asarray(signal)
    mask = (t >= 3.0) & np.isfinite(y)
    t, y = t[mask][::2], y[mask][::2]
    empty = {"frequency_hz": None, "damping_ratio": None, "status": "no identificable"}
    if len(t) < 80 or not np.allclose(np.diff(t), np.diff(t)[0], atol=1e-8):
        return empty
    t = t-t[0]
    trend = np.column_stack((np.ones(len(t)), t))
    y = y-trend@np.linalg.lstsq(trend, y, rcond=None)[0]
    if np.linalg.norm(y) < 1e-8:
        return empty
    width = min(60, len(y)//3)
    h0 = np.column_stack([y[i:i+width] for i in range(len(y)-width)])
    h1 = np.column_stack([y[i+1:i+width+1] for i in range(len(y)-width)])
    u, singular, vh = np.linalg.svd(h0, full_matrices=False)
    rank = min(6, int(np.count_nonzero(singular > singular[0]*1e-5)))
    if rank < 2:
        return empty
    reduced = u[:, :rank].T@h1@vh[:rank].T/singular[:rank][None, :]
    poles = np.linalg.eigvals(reduced).astype(complex)
    if np.any(abs(poles) < 1e-12):
        return empty
    exponent = np.log(poles)/float(t[1]-t[0])
    if np.any(exponent.real > 1.0):
        return empty
    basis = np.exp(t[:, None]*exponent[None, :])
    amplitude = np.linalg.lstsq(basis, y, rcond=None)[0]
    fit_error = float(np.linalg.norm((basis@amplitude).real-y)/np.linalg.norm(y))
    candidates = [i for i, s in enumerate(exponent) if .15 <= s.imag/(2*np.pi) <= 5.0]
    if not candidates:
        return empty
    i = max(candidates, key=lambda j: abs(amplitude[j]))
    dominance = float(abs(amplitude[i])/sum(abs(amplitude[j]) for j in candidates))
    frequency = float(exponent[i].imag/(2*np.pi))
    if fit_error > .25 or dominance < .6 or frequency*t[-1] < 2:
        return {**empty, "fit_relative_error": fit_error, "dominance": dominance}
    return {"frequency_hz": frequency, "damping_ratio": float(-exponent[i].real/abs(exponent[i])),
            "status": "estimación de señal, no modal del sistema",
            "fit_relative_error": fit_error, "dominance": dominance}


def signal_metrics(time_s, reference, predicted):
    """ECM relativo = mean(error²)/mean(PF²), normalización máxima por ptp(PF)."""
    t, ref, pred = map(np.asarray, (time_s, reference, predicted))
    mask = np.isfinite(ref) & np.isfinite(pred)
    if not mask.any():
        return {"matched_samples": 0}
    error = pred[mask]-ref[mask]
    span, rms_level = float(np.ptp(ref[mask])), float(np.mean(ref[mask]**2))
    mse, maximum = float(np.mean(error**2)), float(np.max(abs(error)))
    pf_peak, pm_peak = first_peak(t, ref), first_peak(t, pred)
    return {"matched_samples": int(mask.sum()), "pf_excursion": span,
            "relative_mse": mse/rms_level if rms_level > 1e-24 else None,
            "rmse_absolute": math.sqrt(mse), "max_absolute": maximum,
            "nrmse_excursion": math.sqrt(mse)/span if span > 1e-10 else None,
            "max_normalized": maximum/span if span > 1e-10 else None,
            "normalization_status": "zero or near-zero excursion" if span <= 1e-10 else "ptp(PF)",
            "pf_first_peak_s": pf_peak, "pm_first_peak_s": pm_peak,
            "first_peak_difference_s": pm_peak-pf_peak if pf_peak is not None and pm_peak is not None else None}


class ReplayInputs:
    """Interpolación explícita de entradas, nunca a través de saltos de U.

    PF exporta el lado previo en 1 y 1,1 s. El límite derecho no está medido.
    Se extrapolan las primeras dos muestras del segmento nuevo hasta el evento.
    No se usa P/Q ni velocidad PF como excitación ni como ajuste de parámetros.
    """

    def __init__(self, time_s, signals, events):
        self.time_s = np.asarray(time_s)
        self.voltage_magnitude = signals["B39_U_pu"]
        self.voltage_angle = np.unwrap(np.deg2rad(signals["B39_angle_deg"]))
        self.rotor_angle = np.unwrap(signals["G39_rotor_rad"])
        self.events = sorted(set(events))
        self.bounds = [float(time_s[0]), *self.events, float(time_s[-1])]
        self.segments = []
        for left, right in zip(self.bounds[:-1], self.bounds[1:]):
            mask = (self.time_s > left+1e-9) & (self.time_s <= right+1e-9)
            if left == self.time_s[0]:
                mask |= self.time_s == left
            t = self.time_s[mask]
            u, a = self.voltage_magnitude[mask], self.voltage_angle[mask]
            if len(t) < 2:
                raise ValueError("Segmento de entrada sin dos muestras")
            if t[0] > left:
                fraction = (left-t[0])/(t[1]-t[0])
                u = np.r_[u[0]+fraction*(u[1]-u[0]), u]
                a = np.r_[a[0]+fraction*(a[1]-a[0]), a]
                t = np.r_[left, t]
            self.segments.append((t, u, a))

    def at(self, time_s, segment):
        t, u, a = self.segments[segment]
        voltage = np.interp(time_s, t, u)*np.exp(1j*np.interp(time_s, t, a))
        phi = float(np.interp(time_s, self.time_s, self.rotor_angle))
        return complex(voltage), phi


def simulate_g39_replay(case, time_s, signals, event_times, *, step_s=.001):
    """Cinco estados: cuatro flujos y velocidad, phi externa [rad].

    Usa máquina nativa de seis estados existente. Su ecuación cinemática no se
    evalúa como predicción porque no se exportó la frecuencia de referencia G31.
    Trapezoidal implícito, Newton central, paso 1 ms con aterrizaje en eventos.
    Excitación y potencia mecánica constantes desde LF, G39 carece de ElmComp.
    """
    if not 0 < step_s <= .01:
        raise ValueError("Paso de integración inválido")
    machine = next(m for m in case.machines.values() if m.name == "G 01")
    # G39 tiene un marco vacío, sin ElmDsl. Un controlador activo lo invalida.
    generator_path = next(g["path"] for g in case.model["gens"] if g["id"] == machine.object_id)
    active_controls = {c["path"] for c in case.inventory["controllers"]
                       if values(c["fields"])["outserv"] == 0}
    for composite in case.inventory["composites"]:
        members = values(composite["fields"]).get("pelm", ())
        if generator_path in members and any(path in active_controls for path in members):
            raise ValueError("G39 tiene controles, replay de ve/pt constantes no representable")
    x = case.states[machine.object_id][:-1].copy()
    initial = x.copy()
    inputs = ReplayInputs(time_s, signals, event_times)
    result = {key: np.full(len(time_s), np.nan) for key in PREDICTED}
    max_residual, segment, now, steps = 0.0, 0, float(time_s[0]), 0

    def full_state(y, t, segment_index):
        voltage, phi = inputs.at(t, segment_index)
        return np.r_[y, phi], voltage

    def rhs(y, t, segment_index):
        state, voltage = full_state(y, t, segment_index)
        return machine.f(t, state, np.empty(0), voltage)[:-1]

    for index, target in enumerate(time_s):
        while now < target-1e-10:
            # La salida en el instante del evento conserva el lado izquierdo.
            if segment < len(inputs.segments)-1 and now >= inputs.bounds[segment+1]-1e-10:
                segment += 1
            end = min(float(target), now+step_s, inputs.bounds[segment+1])
            h = end-now
            old_f = rhs(x, now, segment)
            guess = x+h*old_f

            def residual(y):
                return y-x-h*.5*(old_f+rhs(y, end, segment))

            for _ in range(12):
                residual_value = residual(guess)
                if np.max(abs(residual_value)) <= 1e-11:
                    break
                increments = 1e-6*np.maximum(1.0, abs(guess))
                jac = np.column_stack([(residual(guess+np.eye(len(x))[j]*increments[j])-
                    residual(guess-np.eye(len(x))[j]*increments[j]))/(2*increments[j]) for j in range(len(x))])
                guess -= np.linalg.solve(jac, residual_value)
            else:
                raise RuntimeError("Newton del replay no converge")
            max_residual = max(max_residual, float(np.max(abs(residual(guess)))))
            x, now = guess, end
            steps += 1
        full, voltage = full_state(x, float(target), segment)
        output = machine.outputs(full, voltage)
        for key, value in zip(PREDICTED, (output["xspeed"], output["P1_mw"], output["Q1_mvar"], machine.ve)):
            result[key][index] = value
    return result, {"integration_method": "trapezoidal implícito", "step_s": step_s,
                    "integration_steps": steps, "max_step_residual": max_residual,
                    "initial_dynamic_state": initial.tolist(), "predicted_state_count": len(x),
                    "boundary_inputs_pf": ["B39_U_pu", "B39_angle_deg", "G39_rotor_rad"],
                    "held_from_native_lf": {"ve_pu": machine.ve, "pt_pu_Pgn": machine.pt},
                    "output_side_at_events": "pre_event", "full_network_simulation": False}


def inside_workspace(path):
    from ..resources import confined_output
    target = Path(path).expanduser().resolve()
    base = output_root() if target.is_relative_to(output_root()) else Path.cwd()
    return confined_output(target, base)


def write_json(path, data):
    path = inside_workspace(path)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")


def write_csv(path, rows, fields=None):
    path = inside_workspace(path)
    rows = list(rows)
    fields = fields or list(dict.fromkeys(k for row in rows for k in row))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: (None if isinstance(v, (float, np.floating)) and not np.isfinite(v) else v)
                             for k, v in row.items()})


def field_correspondence(case):
    """Filas largas con identidad, campo PF, unidad y destino pm."""
    rows = []
    aliases = {"h": "h [s], tag prevalece en SynchronousMachine", "rstr": "ra, circuit.rstr",
        "xds": "xd prima", "xqs": "xq prima", "xdss": "xd segunda", "xqss": "xq segunda",
        "tds0": "Td0 prima", "tqs0": "Tq0 prima", "tdss0": "Td0 segunda", "tqss0": "Tq0 segunda"}
    for obj in case.inventory["types"].values():
        for field, value in obj["fields"].items():
            rows.append({"object_id": obj["object_id"], "relative_path": obj["relative_path"],
                "name": obj["name"], "pf_class": obj["class"], "pf_field": field,
                "value_json": json.dumps(value["value"], ensure_ascii=False), "unit": value["unit"],
                "description": value["description"],
                "pm_target": aliases.get(field, "SynchronousMachine.typ."+field) if obj["class"] == "TypSym"
                    else "solo inventario, carga RMS completa no implementada",
                "source": "modelo/types.json:"+obj["path"]+".attributes."+field})
    for obj in case.inventory["controllers"]:
        for field, value in {**obj["fields"], **obj["type_fields"]}.items():
            if field == "params":
                continue
            rows.append({"object_id": obj["object_id"], "relative_path": obj["relative_path"],
                "name": obj["name"], "pf_class": "ElmDsl/BlkDef", "pf_field": field,
                "value_json": json.dumps(value["value"], ensure_ascii=False), "unit": value["unit"],
                "description": value["description"], "pm_target": obj["execution_status"],
                "source": "modelo/controllers.json:"+obj["path"]})
        names = obj["parameter_names_declared"]
        for index, value in enumerate(obj["fields"]["params"]["value"]):
            rows.append({"object_id": obj["object_id"], "relative_path": obj["relative_path"],
                "name": obj["name"], "pf_class": "ElmDsl", "pf_field": "params["+str(index)+"]",
                "value_json": json.dumps(value), "unit": "no exportada por parámetro",
                "description": names[index] if index < len(names) else "nombre no exportado, no se infiere límite",
                "pm_target": obj["execution_status"],
                "source": "modelo/controllers.json:"+obj["path"]+".fields.params / type_fields.sParams"})
    for gen in case.model["gens"]:
        for field, value in case.inventory["generator_attributes"][gen["path"]].items():
            rows.append({"object_id": gen["id"], "relative_path": gen["relative_path"], "name": gen["name"],
                "pf_class": "ElmSym", "pf_field": field, "value_json": json.dumps(value["value"]),
                "unit": value["unit"], "description": value["description"],
                "pm_target": "SynchronousMachine.elm."+field,
                "source": "modelo/generators.json:"+gen["path"]+".attributes."+field})
    for trial, config in case.inventory["trials"].items():
        for event in config["events"]:
            for field, value in event["fields"].items():
                rows.append({**identity(event["path"]), "name": trial+" / "+event["name"],
                    "pf_class": event["class"], "pf_field": field, "value_json": json.dumps(value["value"]),
                    "unit": value["unit"], "description": value["description"],
                    "pm_target": "evento completo no ejecutado, frontera PF en replay",
                    "source": trial+"/configuration.json:events"})
    return rows


def export_case(case, trial, output_dir, *, plots=True):
    """Exporta cotejo parcial con máscaras, canales omitidos y hashes."""
    output_dir = inside_workspace(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    time_s, signals, channels = load_reference(case.pf_dir / trial)
    if len(time_s) != 1001 or not np.allclose(time_s, np.linspace(0, 10, 1001), atol=1e-10):
        raise ValueError("El ensayo no cumple las 1001 muestras 0..10 s")
    events = sorted(set(values(e["fields"])["time"] for e in case.inventory["trials"][trial]["events"]
                        if values(e["fields"]).get("outserv") == 0))
    initial = case.initial_signals(channels)
    initialization = [{"signal": c["signal"], "unit": c["unit"], "object_id": c["object_id"],
        "relative_path": c["relative_path"], "pf_t0": float(signals[c["signal"]][0]),
        "pm_t0": initial[c["signal"]], "delta_pm_minus_pf": initial[c["signal"]]-signals[c["signal"]][0],
        "initial_absolute_limit": INITIAL_LIMITS[c["unit"]],
        "scope": "equilibrio de máquinas y red en LF, controles y estados de cargas no inicializados"}
        for c in channels]
    for row in initialization:
        if not math.isfinite(row["pf_t0"]) or abs(row["delta_pm_minus_pf"]) > row["initial_absolute_limit"]:
            raise ValueError("Condición inicial RMS no corresponde al caso nativo: "+row["signal"])
    predicted, replay = simulate_g39_replay(case, time_s, signals, events)
    metrics, modes, windows = [], [], []
    for channel in channels:
        key = channel["signal"]
        base = {"signal": key, "unit": channel["unit"], "object_id": channel["object_id"],
                "relative_path": channel["relative_path"], "label": LABEL,
                "full_model_parity": False, "acceptance": "no aplicable",
                "declared_max_normalized_limit": MAX_ERROR_LIMIT,
                "declared_angle_peak_delay_relative_limit": ANGLE_PEAK_LIMIT}
        ref = signals[key]
        pf_mode = dominant_mode(time_s, ref)
        modes.append({"signal": key, "origin": "PF", **pf_mode})
        if key in predicted:
            pm_mode = dominant_mode(time_s, predicted[key])
            metrics.append({**base, "scope": "subsistema G39, phi y U impuestas por PF",
                **signal_metrics(time_s, ref, predicted[key]),
                "pf_dominant_frequency_hz": pf_mode["frequency_hz"],
                "pf_damping_ratio": pf_mode["damping_ratio"], "pf_mode_status": pf_mode["status"],
                "pm_dominant_frequency_hz": pm_mode["frequency_hz"],
                "pm_damping_ratio": pm_mode["damping_ratio"], "pm_mode_status": pm_mode["status"],
                "damping_ratio_difference": pm_mode["damping_ratio"]-pf_mode["damping_ratio"]
                    if pm_mode["damping_ratio"] is not None and pf_mode["damping_ratio"] is not None else None})
            modes.append({"signal": key, "origin": "pm replay", **pm_mode})
            for window, mask in [("pre_event", time_s < 1),
                ("transient", (time_s >= 1) & (time_s < 3)), ("post_event", time_s >= 3),
                ("first_interval", (time_s >= 1) & (time_s <= 1.01))]:
                windows.append({"signal": key, "window": window, **signal_metrics(
                    time_s[mask], ref[mask], predicted[key][mask])})
        else:
            reason = ("entrada de frontera PF, no predicción" if key in replay["boundary_inputs_pf"]
                else "requiere controles, carga y red cerrada o medición PF no representada")
            metrics.append({**base, "scope": reason, "matched_samples": 0,
                            "pf_excursion": float(np.ptp(ref[np.isfinite(ref)])),
                            "pf_first_peak_s": first_peak(time_s, ref),
                            "pf_dominant_frequency_hz": pf_mode["frequency_hz"],
                            "pf_damping_ratio": pf_mode["damping_ratio"], "pf_mode_status": pf_mode["status"]})
    write_csv(output_dir / "initialization.csv", initialization)
    write_csv(output_dir / "metrics.csv", metrics)
    write_csv(output_dir / "window_metrics.csv", windows)
    write_csv(output_dir / "dominant_modes.csv", modes)
    write_csv(output_dir / "field_correspondence.csv", field_correspondence(case))
    write_csv(output_dir / "channel_units.csv", [{"signal": c["signal"], "unit": c["unit"],
        "variable_pf": c["variable"], "column_elmres": c["column"], "column_comres": c["column"]+1,
        "object_id": c["object_id"], "relative_path": c["relative_path"],
        "status": "predicción de subsistema" if c["signal"] in predicted else "sin predicción"} for c in channels])
    write_csv(output_dir / "signals_comparison.csv", [{"time_s": float(t),
        **{"PF_"+key: float(series[i]) for key, series in signals.items()},
        **{"pm_"+key: float(predicted[key][i]) if key in predicted else None
           for key in signals}} for i, t in enumerate(time_s)])
    terminal = next(m for m in case.machines.values() if m.name == "G 01")
    write_csv(output_dir / "terminal_voltage_initial.csv", [{"generator": m.name,
        "object_id": m.object_id, "relative_path": m.relative_path,
        "terminal_u_pu": abs(case.voltages[m.bus_id]),
        "terminal_angle_deg": np.rad2deg(np.angle(case.voltages[m.bus_id])),
        "pf_rms_terminal_channel": "B39_U_pu" if m is terminal else "no exportado",
        "order": m.order, "ve_pu": m.ve, "pt_pu_Pgn": m.pt,
        "h_machine_s": m.typ["h"], "sgn_unit_mva": m.sgn_mva, "n_units": m.n_units,
        "h_system_s": m.typ["h"]*m.sgn_mva*m.n_units/100.0,
        "initial_derivative_max": float(np.max(abs(m.f(0, case.states[m.object_id],
            np.empty(0), case.voltages[m.bus_id]))))} for m in case.machines.values()])
    config = case.inventory["trials"][trial]
    summary = {"case": trial, "label": LABEL, "accepted_full_model": False,
        "channel_count_actual": len(channels), "channel_count_claimed_leeme": 25,
        "sample_count": len(time_s), "predicted_channels": list(PREDICTED),
        "unpredicted_channels": [c["signal"] for c in channels if c["signal"] not in PREDICTED],
        "max_machine_derivative_initial": case.derivative_max, "kcl_initial_max_pu": case.kcl_max_pu,
        "lf_consistency_projection": case.lf_projection,
        "native_machine_states_initialized": sum(m.order for m in case.machines.values()),
        "complete_dynamic_equilibrium_verified": False,
        "all_exported_initial_signals_within_limits": True,
        "initial_absolute_limits_by_unit": INITIAL_LIMITS,
        "initial_missing_states": "AVR, governors y retardo de cargas",
        "tolerances_declared_before_comparison": {"max_error_over_pf_excursion": MAX_ERROR_LIMIT,
            "angle_first_peak_delay_relative_difference": ANGLE_PEAK_LIMIT,
            "apply_only_with_complete_model_parity": True},
        "native_configuration": config, "replay": replay,
        "engine_coverage": ENGINE_COVERAGE,
        "assumptions_not_exported": {"addmt": "0, entrada mecánica adicional no exportada",
                                    "ifdBase": "1, normalización no exportada, ve0 cotejada con PF"},
        "boundary_uncertainty": "Límite derecho de U en eventos extrapolado desde salida de 10 ms, no medido",
        "native_method_interpretation": "iopt_lt=2 y alpha_rms=1, nombre del algoritmo no exportado",
        "sha256_inputs": {str(p.relative_to(case.pf_dir)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [case.pf_dir / "modelo/types.json", case.pf_dir / "modelo/generators.json",
                      case.pf_dir / "modelo/controllers.json", case.pf_dir / "modelo/composites.json",
                      case.pf_dir / "E39-LF-01/buses.csv", case.pf_dir / "E39-LF-01/generators.csv",
                      *sorted((case.pf_dir / trial).glob("*.json")), case.pf_dir / trial / "signals_native.csv"]},
        "lf_model_sha256": hashlib.sha256(MODEL_FILE.read_bytes()).hexdigest(),
        "comparison_code_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    write_json(output_dir / "dynamic_case.json", {"machines": case.model["gens"],
        "native_inventory": case.inventory, "full_case_supported": False,
        "static_model_ref": {"workspace_path": "data/ieee39_pf/model.json",
                             "sha256": summary["lf_model_sha256"]},
        "bases": {"s_base_mva": 100.0, "f_nom_hz": 60.0,
                  "machine_parameters": "TypSym.sgn, ugn por unidad, ngnum explícito"},
        "lf_voltages": {key: [v.real, v.imag] for key, v in case.voltages.items()},
        "initial_states": {key: state.tolist() for key, state in case.states.items()}})
    summary["plot_status"] = make_plots(output_dir, time_s, signals, predicted, trial) if plots else "omitidos por opción"
    summary["sha256_outputs"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(output_dir.iterdir()) if p.is_file() and p.name != "summary.json"}
    write_json(output_dir / "summary.json", summary)
    return summary


def make_plots(output_dir, time_s, signals, predicted, trial):
    # Matplotlib también debe escribir su caché dentro del workspace.
    os.environ["MPLCONFIGDIR"] = str(inside_workspace(output_dir / "mplconfig"))
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return "matplotlib no disponible, omitidos"
    with plt.rc_context({"font.size": 9}):
        fig, axes = plt.subplots(3, 1, figsize=(8, 8), layout="constrained")
        for ax, key, unit in zip(axes, PREDICTED[:3], ("pu", "MW", "Mvar")):
            ax.plot(time_s, signals[key], color="black", label="PF")
            ax.plot(time_s, predicted[key], color="gray", linestyle="--", label="pm, subsistema con frontera PF")
            ax.axvline(1, color="black", linestyle=":", linewidth=.7)
            if trial == CASES[0]:
                ax.axvline(1.1, color="black", linestyle=":", linewidth=.7)
            ax.set(ylabel=key+" ["+unit+"]", xlabel="Tiempo [s]")
            ax.legend(fontsize=8)
        fig.suptitle(trial+"\nComparación parcial por diferencia de modelo")
        fig.savefig(output_dir / "g39_partial.png", dpi=160, facecolor="white")
        plt.close(fig)
    return "PNG parcial, blanco y negro, sin interpolación de salidas comparadas"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pf-dir", type=Path, default=DEFAULT_PF_DIR)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    inside_workspace(args.output_dir)
    case = NativeDynamicCase(args.pf_dir)
    for trial in CASES:
        summary = export_case(case, trial, args.output_dir / trial, plots=not args.no_plots)
        print(trial, summary["label"], "29 canales iniciales, 4 predicciones del subsistema G39")


if __name__ == "__main__":
    main()


def compare_signals(time_s, reference, predicted):
    """Compara todas las señales comunes en un calendario explícito."""
    if set(reference) != set(predicted):
        raise ValueError("Las señales de referencia y cálculo deben coincidir")
    return {key: signal_metrics(time_s, reference[key], predicted[key]) for key in reference}
