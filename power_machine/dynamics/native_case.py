'Native case support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error
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

from ..benchmarks.ieee39_ref import DEFAULT_REF_DIR, WORKSPACE, LDF_OPTIONS
from .machine_init import initialize_machine
from .network import DynamicNetwork
from .synchronous import SynchronousMachine
from ..loadflow import _stamp, run_loadflow

CASES = ("E39-DIN-01", "E39-DIN-02", "E39-DIN-03")
LABEL = 'Partial comparison due to model differences'
MAX_ERROR_LIMIT = 0.02
ANGLE_PEAK_LIMIT = 0.01
INITIAL_LIMITS = {"p.u.": 1e-8, "rad": 1e-8, "deg": 1e-6, "Hz": 1e-8, "MW": 1e-5, "Mvar": 1e-5}
MODEL_FILE = required_readonly_path("cases", "ieee39_ref", "model.json")
OUTPUT_DIR = output_root() / "comparison_results/ieee39_ref"
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
    "SynchronousMachine": 'Rotor fluxes, 6 turbo or 5 salient states. No imposed classical model',
    "machine_initialization": 'Load flow, sgn/ugn/ngnum bases, isat saturation and i_speedVar',
    "ControlledSynchronousMachine": 'AVR and governor with explicit case ports',
    "IEEET1_IEEEG1_IEEEG3": 'No generic IEEE39 frame coupling',
    "DynamicCatalog": 'Requires ref-dynamic-v1 export, parnam, BlkDef equations, limits and connections',
    "PSS_CONV": 'outserv=1 in all nine instances. Execution is unnecessary for these trials',
    "RMSLoad": 'Static correction without t1. Missing kpu1/kqu1 aliases and low-voltage regime',
    "network": 'DynamicNetwork Z matches only load flow and is not used to simulate trials',
    "integrator": 'Trapezoidal DAESolver is available. Model gaps prevent coupling the full case',
}


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def identity(path):
    'Return a deterministic REF object ID and the path relative to IntPrj.'
    relative = path.split(".IntPrj\\", 1)[1]
    return {"object_id": "pf_" + hashlib.sha256(relative.encode()).hexdigest()[:20],
            "relative_path": relative}


def projected_fields(obj, names):
    fields = obj.get("attributes", obj.get("fields", {}))
    return {key: fields[key] for key in names if key in fields}


def values(fields):
    'Preserve missing values as missing and never convert NULL into zero.'
    return {k: v["value"] for k, v in fields.items() if v["value"] is not None}


def load_reference(case_dir):
    'Resolve ComRes columns using both channel manifest and headers. Time uses seconds. Manifest column indices exclude the first time column. Preserve empty samples as internal NaN.'
    case_dir = Path(case_dir)
    channels = read_json(case_dir / "channel_units.json")
    with (case_dir / "signals_native.csv").open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle, delimiter=";")
        objects, variables = next(reader), next(reader)
        if variables[0] != "b:tnow in s":
            raise _coded_error(ValueError, 'invalid_native_case_data', 'ComRes time must use seconds')
        if len(channels) != len(variables)-1 or len(objects) != len(variables):
            raise _coded_error(ValueError, 'invalid_native_case_data', 'Incomplete channel manifest')
        columns = []
        for channel in channels:
            index = int(channel["column"])+1
            variable, unit = variables[index].split(" in ", 1)
            if objects[index] != channel["object"]["name"] or variable != channel["variable"]:
                raise _coded_error(ValueError, 'invalid_native_case_data', 'Cabecera y canal no corresponden: ' + channel['signal'])
            channel.update(identity(channel["object"]["path"]), unit=unit)
            columns.append(index)
        if len(set(columns)) != len(columns):
            raise _coded_error(ValueError, 'invalid_native_case_data', 'Duplicate columns')
        times, rows = [], []
        for row in reader:
            if not row:
                continue
            if len(row) != len(variables):
                raise _coded_error(ValueError, 'invalid_native_case_data', 'Incomplete ComRes row')
            times.append(float(row[0]))
            rows.append([float(row[i]) if row[i].strip() else np.nan for i in columns])
    time_s = np.asarray(times)
    if len(time_s) < 2 or np.any(np.diff(time_s) <= 0):
        raise _coded_error(ValueError, 'invalid_native_case_data', 'ComRes schedule must be increasing')
    matrix = np.asarray(rows)
    if np.isinf(matrix).any():
        raise _coded_error(ValueError, 'invalid_native_case_data', 'Reference contains infinities')
    return time_s, {c["signal"]: matrix[:, i] for i, c in enumerate(channels)}, channels


def native_inventory(ref_dir):
    'Extract field-level data and provenance without copying large source JSON files.'
    ref_dir = Path(ref_dir)
    types = {}
    for obj in read_json(ref_dir / "modelo/types.json"):
        names = MACHINE_FIELDS if obj["cls"] == "TypSym" else LOAD_FIELDS
        if obj["cls"] in ("TypSym", "TypLod"):
            types[obj["path"]] = {**identity(obj["path"]),
                "path": obj["path"], "name": obj["name"], "class": obj["cls"],
                "fields": projected_fields(obj, names)}
    controllers = []
    for obj in read_json(ref_dir / "modelo/controllers.json"):
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
        controller["execution_status"] = ('out_of_service' if values(controller["fields"])["outserv"]
            else 'Cannot represent without BlkDef graphs and limit names')
        controllers.append(controller)
    composites = []
    for obj in read_json(ref_dir / "modelo/composites.json"):
        composites.append({**identity(obj["path"]), "path": obj["path"],
                           "fields": projected_fields(obj, ("pelm", "typ_id", "outserv"))})
    generator_attributes = {obj["path"]: projected_fields(obj,
        ("ngnum", "ip_ctrl", "efdBaseRatio", "ifdBase", "addmt", "typ_id", "bus1"))
        for obj in read_json(ref_dir / "modelo/generators.json")}
    trials = {}
    for case in CASES:
        directory = ref_dir / case
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


class NativeDynamicCase:
    'Native initial operating point. Do not substitute missing RMS loads or controls. The DynamicNetwork Z stamp checks only the load-flow KCL point.'

    def __init__(self, ref_dir=DEFAULT_REF_DIR, model=None):
        self.ref_dir = Path(ref_dir)
        self.model = copy.deepcopy(model if model is not None else read_json(self.ref_dir / "model.json"))
        self.inventory = native_inventory(self.ref_dir)
        with (self.ref_dir / "E39-LF-01/buses.csv").open(encoding="utf-8-sig", newline="") as handle:
            buses_ref = {r["path"]: r for r in csv.DictReader(handle)}
        with (self.ref_dir / "E39-LF-01/generators.csv").open(encoding="utf-8-sig", newline="") as handle:
            power_ref = {r["path"]: r for r in csv.DictReader(handle)}
        self.voltages = {b["id"]: float(buses_ref[b["path"]]["u_pu"])*np.exp(
            1j*np.deg2rad(float(buses_ref[b["path"]]["angle_deg"]))) for b in self.model["buses"]}
        buses = {b["id"]: b for b in self.model["buses"]}
        self.machines, self.states, self.initializations = {}, {}, {}
        self.machine_by_path = {}
        self.derivative_max = 0.0
        for gen in self.model["gens"]:
            native = self.inventory["types"][gen["type_id"]]
            gen["type_params"].update(values(native["fields"]))
            gen["params"].update(values(self.inventory["generator_attributes"][gen["path"]]))
            machine = SynchronousMachine(gen, bus_un_kv=buses[gen["bus"]]["un_kv"],
                                         s_base_mva=100.0, f_nom_hz=60.0)
            machine.relative_path = gen["relative_path"]
            row = power_ref[gen["path"]]
            init = initialize_machine(machine, self.voltages[gen["bus"]],
                                      complex(float(row["P_MW"]), float(row["Q_Mvar"])))
            self.machines[gen["id"]], self.states[gen["id"]] = machine, init.state
            self.initializations[gen["id"]] = init
            self.machine_by_path[gen["path"]] = machine
            self.derivative_max = max(self.derivative_max, float(np.max(abs(
                machine.f(0.0, init.state, np.empty(0), self.voltages[gen["bus"]])))))
        self.network = DynamicNetwork(self.model, self.voltages)
        vector = self.network.voltage_vector(self.voltages)
        injection = np.zeros(len(vector), complex)
        for key, machine in self.machines.items():
            i = self.network.index_of_bus(machine.bus_id)
            injection[i] += machine.current_injection(self.states[key], vector[i])
        self.kcl_max_pu = float(np.max(abs(injection-self.network.ybus@vector)))
        # La referencia externa y las estampas guardadas difieren en sus últimos
        # decimales. Se proyecta el punto REF al LF consistente de PFC-MODEL,
        # sin cambiar datos de equipo ni conservar un residual de arranque.
        source_voltages = dict(self.voltages)
        source_kcl = self.kcl_max_pu
        lf = run_loadflow(self.model, options=LDF_OPTIONS)
        if not lf["converged"]:
            raise _coded_error(ValueError, 'invalid_native_case_data', 'Native load-flow projection did not converge')
        self.voltages = {b["id"]: b["u_pu"]*np.exp(1j*np.deg2rad(b["angle_deg"])) for b in lf["buses"]}
        projected_power = {g["id"]: complex(g["p_mw"], g["q_mvar"]) for g in lf["gens"]}
        power_corrections = []
        self.derivative_max = 0.0
        for gen in self.model["gens"]:
            key, machine = gen["id"], self.machines[gen["id"]]
            source = power_ref[gen["path"]]
            power_corrections.append(abs(projected_power[key]-complex(float(source["P_MW"]), float(source["Q_Mvar"]))))
            init = initialize_machine(machine, self.voltages[machine.bus_id], projected_power[key])
            self.states[key], self.initializations[key] = init.state, init
            self.derivative_max = max(self.derivative_max, float(np.max(abs(
                machine.f(0, init.state, np.empty(0), self.voltages[machine.bus_id])))))
        self.lf_projection = {"source_ref_kcl_max_pu": source_kcl,
            "max_voltage_phasor_correction_pu": max(abs(self.voltages[k]-v) for k, v in source_voltages.items()),
            "max_generator_power_correction_mva": max(power_corrections),
            "method": 'PFC-MODEL load flow, unchanged parameters and explicit consistency correction'}
        if (self.lf_projection["max_voltage_phasor_correction_pu"] >= 1e-8
                or self.lf_projection["max_generator_power_correction_mva"] >= 1e-5):
            raise _coded_error(ValueError, 'invalid_native_case_data', 'Load-flow correction exceeds the expected PFC-MODEL decimal precision')
        self.network = DynamicNetwork(self.model, self.voltages)
        vector = self.network.voltage_vector(self.voltages)
        injection = np.zeros(len(vector), complex)
        for key, machine in self.machines.items():
            i = self.network.index_of_bus(machine.bus_id)
            injection[i] += machine.current_injection(self.states[key], vector[i])
        self.kcl_max_pu = float(np.max(abs(injection-self.network.ybus@vector)))
        if max(self.derivative_max, self.kcl_max_pu) >= 1e-8:
            raise _coded_error(ValueError, 'invalid_native_case_data', f'Native subset is not initially at equilibrium, dx={self.derivative_max:.6g}, KCL={self.kcl_max_pu:.6g}')

    def simulate_full(self):
        raise _coded_error(NotImplementedError, 'unsupported_native_case_data', 'Missing IEEET1, IEEEG1, IEEEG3, frame or TypLod load with t1 and limits')

    def initial_signals(self, channels):
        'Return the 29 initial signals without using an RMS trajectory as an initializer.'
        entities = {e["path"]: e for group in ("buses", "branches", "loads", "gens")
                    for e in self.model[group]}
        buses = {b["id"]: b for b in self.model["buses"]}
        result = {}
        for channel in channels:
            entity = entities[channel["object"]["path"]]
            variable, key = channel["variable"], entity["id"]
            if entity["class"] == "ElmTerm":
                v = self.voltages[key]
                value = {"m:u": abs(v), "m:phiu": np.rad2deg(np.angle(v)), "m:fehz": 60.0}[variable]
            elif entity["class"] == "ElmSym":
                machine = self.machines[key]
                output = machine.outputs(self.states[key], self.voltages[machine.bus_id])
                value = {"s:speed": output["xspeed"], "s:phi": output["phi"],
                         "s:ve": machine.ve, "m:P:bus1": output["P1_mw"],
                         "m:Q:bus1": output["Q1_mvar"]}[variable]
            elif entity["class"] == "ElmLod":
                value = entity["p_mw"] if variable == "m:P:bus1" else entity["q_mvar"]
            else:
                ids, _, stamp = _stamp(entity, buses, 100.0)
                v = np.asarray([self.voltages[b] for b in ids])
                power = v*np.conj(stamp@v)*100.0
                value = power[0 if variable.endswith("bus1") else 1].real
            result[channel["signal"]] = float(value)
        return result


