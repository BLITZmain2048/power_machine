'Runner support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import cmath
import copy
import csv
import json
import math
from pathlib import Path
import time

import numpy as np

from power_machine import elements
from power_machine.loadflow import _stamp
from power_machine.overrides import apply_overrides, read_overrides
from .controllers import ControlledSynchronousMachine
from .converters import StaticGeneratorRMS, WECCPVAdapter, converter_bindings
from .dsl_compile import DynamicCatalog
from .events import Event
from .loads import RMSLoad
from .measurements import PublishedFrequencyHistory
from .network import DynamicNetwork
from .schema import read_dynamic_export
from .solver import DAESolver, DAEState, PostEventSteps
from .synchronous import SynchronousMachine
from .plant import MultiportPlant


CASES = ()
NEW_CASES = ()

def _csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def load_case(root: Path, case: str) -> tuple[list[dict], dict]:
    folder = root / "pf_reference/rms" / case
    events = json.loads((folder / "events.json").read_text(encoding="utf-8"))
    report = json.loads((folder / "run_report.json").read_text(encoding="utf-8"))
    if not (report.get("sim_valid") and report.get("sim_return_code") == 0
            and report.get("inc_return_code") == 0):
        raise _coded_error(ValueError, 'invalid_runner_data', f'PF reference is unsuitable: {case}')
    return events, report


def translate_events(events: list[dict], model: dict, *,
                     reference_case: str | None = None) -> list[Event]:
    'Map exact PF target paths to events. Convert fault R/X in ohms to local p.u. reference_case selects explicit case-supplied composite switching data.'
    items = {item["path"]: item for group in ("buses", "branches", "loads", "switches", "gens")
             for item in model[group]}
    switches = {item["id"]: item for item in model["switches"]}
    mapped = []
    for raw in events:
        path, attrs, t = raw["target"], raw["attributes"], float(raw["time_s"])
        if raw["class"] == "EvtLod":
            item = items.get(path)
            if item is None or item["class"] != "ElmLod":
                raise _coded_error(ValueError, 'invalid_runner_data', f'EvtLod has no exact load target: {path}')
            mapped.append(Event(t, "load_step", item["id"],
                                (float(attrs["dP"]), float(attrs["dQ"]))))
        elif raw["class"] == "EvtSwitch":
            item = items.get(path)
            if item is not None and item['class'] in ('ElmSym', 'ElmGenstat', 'ElmPvsys'):
                if int(attrs['i_switch']) != 0:
                    raise _coded_error(ValueError, 'invalid_runner_data', 'RMS generator reconnection is not supported')
                mapped.append(Event(t, 'gen_trip', item['id']))
                continue
            if item is None or item["class"] not in ('ElmLne', 'StaSwitch', 'ElmCoup'):
                raise _coded_error(ValueError, 'invalid_runner_data', f'EvtSwitch has no exact target: {path}')
            fixed_ids = model.get('reference_element_switches', {}).get(reference_case, {}).get(item['id'])
            if fixed_ids is not None and item['class'] == 'ElmLne':
                for sid in fixed_ids:
                    switch = switches.get(sid)
                    if (switch is None or switch.get('class') not in ('StaSwitch', 'ElmCoup')
                            or switch.get('outserv', 0) == 1):
                        raise _coded_error(ValueError, 'invalid_runner_data', f'Explicit reference switch is unavailable: {sid}')
                kind = 'switch_open' if int(attrs['i_switch']) == 0 else 'switch_close'
                mapped.extend(Event(t, kind, sid) for sid in fixed_ids)
                continue
            prefix = 'element_switch_' if item['class'] == 'ElmLne' else 'switch_'
            mapped.append(Event(t, prefix + ('open' if int(attrs['i_switch']) == 0 else 'close'), item['id']))
        elif raw["class"] == "EvtShc":
            bus_path = path.split("\\Cub_", 1)[0]
            item = items.get(bus_path)
            if item is None or item["class"] != "ElmTerm":
                raise _coded_error(ValueError, 'invalid_runner_data', f'EvtShc has no exact bus target: {path}')
            if int(attrs["i_shc"]) == 4:
                mapped.append(Event(t, "fault_clear", item["id"]))
            elif int(attrs["i_shc"]) == 0:
                z_ohm = complex(float(attrs["R_f"]), float(attrs["X_f"]))
                z_base_ohm = float(item["un_kv"]) ** 2 / float(model["meta"]["s_base_mva"])
                mapped.append(Event(t, "fault_apply", item["id"], z_ohm / z_base_ohm))
            else:
                raise _coded_error(ValueError, 'invalid_runner_data', f"Unsupported i_shc: {attrs['i_shc']}")
        else:
            raise _coded_error(ValueError, 'invalid_runner_data', f"Unsupported PF event: {raw['class']}")
    return sorted(mapped, key=lambda event: event.time_s)


class RMSRunner:
    'Initialize an energized island and execute an RMS case using explicit dynamic data.'

    def __init__(self, root: Path, case: str | None = None, *, dt_s: float = 0.001,
                 events=None, monitored=None, tstart=None, operating_point=None,
                 model=None, linear_solver="compatibility", retry_steps=False,
                 min_step_s=1e-6, max_step_retries=12, boundary_voltages=None):
        """Accept external events and a load-flow operating point.

        bus_voltages uses complex p.u. and generator_powers uses MW+jMvar.
        Without an external point, retain the reference initialization.
        linear_solver selects the dense backend. Step rejection is opt-in,
        halves the attempted step down to min_step_s and rolls back runtime
        state, DSL memory and buffers before retrying.
        """
        self.root, self.case = Path(root), case or "study"
        self.external_signals = monitored is not None
        self.override_warnings = []
        if model is None:
            self.model = json.loads((self.root / "model.json").read_text(encoding="utf-8"))
            self.model, self.override_warnings = apply_overrides(
                self.model, read_overrides(self.root / "overrides.json"))
        else:
            self.model = copy.deepcopy(model)
        raw_dynamic_dir = self.model.get("_dynamic_dir")
        if not raw_dynamic_dir:
            raise _coded_error(ValueError, 'missing_dynamic_data', "Missing dynamic data. Specify the case dynamic directory in model['_dynamic_dir']")
        dynamic_dir = Path(raw_dynamic_dir).expanduser()
        self.dynamic_dir = (dynamic_dir if dynamic_dir.is_absolute() else self.root / dynamic_dir).resolve()
        if not (self.dynamic_dir / "manifest.json").is_file():
            raise _coded_error(ValueError, 'missing_dynamic_data', f'Missing dynamic data. No manifest.json in {self.dynamic_dir}')
        if case is not None:
            self.raw_events, self.reference_report = load_case(self.root, case)
            with (self.root / "pf_reference/rms" / case / "signals.csv").open(
                    encoding="utf-8-sig", newline="") as handle:
                self.monitored = set(next(csv.reader(handle)))
        else:
            self.raw_events = []
            self.reference_report = {"rms_options": {"tstart": -0.1}, "tstop_s": 10.0}
            self.monitored = set()
        if monitored is not None:
            self.monitored = set(monitored)
        self.events = (translate_events(self.raw_events, self.model, reference_case=case) if events is None
                       else sorted(events, key=lambda e: e.time_s))
        if operating_point is not None:
            self.lf_v = dict(operating_point["bus_voltages"])
            self.pf_lf_v = {}
        else:
            bus_rows = {r["object_id"]: r for r in _csv(self.root / "pf_reference/ldf_buses.csv")}
            self.pf_lf_v = {bid: float(r["m:u"]) * cmath.exp(1j * math.radians(float(r["m:phiu"])))
                            for bid, r in bus_rows.items() if r["energizado"] == "1" and r["m:u"]}
            solved = _csv(self.root / "results/ldf_w2e_base/buses.csv")
            self.lf_v = {row["id"]: float(row["u_pu"]) *
                         cmath.exp(1j * math.radians(float(row["angle_deg"])))
                         for row in solved if row["status"] == "resuelto"}
            if set(self.lf_v) != set(self.pf_lf_v):
                raise _coded_error(ValueError, 'invalid_runner_data', 'Accepted load flow and PF reference differ in energized buses')
        # La red LF incluye islas pasivas a V=0; el DAE solo contiene la isla PF energizada.
        for bus in self.model["buses"]:
            if bus["id"] not in self.lf_v:
                bus["outserv"] = 1
        self.network = DynamicNetwork(self.model, self.lf_v)
        self.s_base_mva = float(self.model["meta"]["s_base_mva"])
        self.buses = {row["id"]: row for row in self.model["buses"]}
        self.by_path = {row["path"]: row for group in ("buses", "branches", "branches3", "loads", "gens")
                        for row in self.model[group]}
        export = read_dynamic_export(self.dynamic_dir)
        self.catalog = DynamicCatalog(self.dynamic_dir)
        selectors = {row["object_id"]: {} for row in export["objects"] if row["class"] == "TypSym"}
        for row in export["attributes"]:
            if row["object_id"] in selectors and row["status"] == "PRESENT":
                if row["attribute"] in ("model_inp", "iturbo", "isat", "isatSalient",
                                        "i_speedVar", "i_exactConv"):
                    selectors[row["object_id"]][row["attribute"]] = json.loads(row["value_json"])
        bindings = converter_bindings(export)
        if operating_point is not None:
            solved_gen = {gid: {"p_mw": complex(s).real, "q_mvar": complex(s).imag}
                          for gid, s in operating_point["generator_powers"].items()}
            inj = {g["id"]: {"object_id": g["id"], "relative_path": g["path"],
                   "m:P:bus1": str(solved_gen[g["id"]]["p_mw"]),
                   "m:Q:bus1": str(solved_gen[g["id"]]["q_mvar"]),
                   "m:P:bus1_unit": "MW", "m:Q:bus1_unit": "Mvar"}
                   for g in self.model["gens"] if g["id"] in solved_gen}
        else:
            inj = {r["object_id"]: r for r in _csv(self.root / "pf_reference/ldf_injections.csv")}
            solved_gen = {r["id"]: r for r in _csv(self.root / "results/ldf_w2e_base/gens.csv")
                          if r["status"] == "resuelto"}
        self.devices = []
        self.initial_values = {}
        self.coverage = []
        active = set(self.network.topology["active"]["gens"])
        for gen in self.model["gens"]:
            if gen["id"] not in active:
                continue
            row = inj.get(gen["id"])
            if row is None or not row["m:P:bus1"] or not row["m:Q:bus1"]:
                raise _coded_error(ValueError, 'missing_runner_data', f"Missing load-flow injection: {gen['path']}")
            own = solved_gen.get(gen["id"])
            if own is None:
                raise _coded_error(ValueError, 'missing_runner_data', f"Generator is missing from accepted load flow: {gen['path']}")
            power = complex(float(own["p_mw"]), float(own["q_mvar"]))
            if gen["class"] == "ElmXnet":
                from .external_grid import ExternalGridRMS
                device=ExternalGridRMS({**gen,'f_nom_hz':self.model['meta']['f_nom_hz']},bus_un_kv=self.buses[gen['bus']]['un_kv'],s_base_mva=self.s_base_mva)
            elif gen["class"] == "ElmSym":
                machine = SynchronousMachine(gen, bus_un_kv=self.buses[gen["bus"]]["un_kv"],
                                              s_base_mva=self.s_base_mva,
                                              dynamic_attributes=selectors.get(gen["type_id"], {}),
                                              f_nom_hz=float(self.model["meta"]["f_nom_hz"]))
                device = ControlledSynchronousMachine(machine, self.catalog,
                    configuration=self.model.get("dynamic_controllers", {}).get(gen["id"]))
                cov = device.discrete_state["coverage"]
                if cov.status != "replicado" and not device.frame_blocks:
                    self.coverage.append({"object_id": gen["id"], "path": gen["path"],
                                          "status": 'Frozen ve/pt', "blocks": cov.blocks})
            elif gen["class"] == "ElmGenstat":
                controlled = gen["id"] in bindings["controlled_genstat"]
                device = StaticGeneratorRMS.from_model(gen, s_base_mva=self.s_base_mva,
                                                       controlled=controlled)
                if controlled:
                    self.coverage.append({"object_id": gen["id"], "path": gen["path"],
                                          "status": 'Frozen load-flow command'})
            elif gen["class"] == "ElmPvsys":
                binding = bindings["wecc_pvsys"].get(gen["id"])
                if binding is None:
                    raise _coded_error(ValueError, 'invalid_runner_data', f"ElmPvsys has no exact WECC pair: {gen['path']}")
                device = WECCPVAdapter.from_binding(gen, binding,
                                                    s_base_mva=self.s_base_mva)
                power = device.bind_exported_controller(gen, binding, self.catalog, row)
                if operating_point is not None:
                    device.discrete_state["lf_provenance"] = "study.run_loadflow: MW+jMvar"
            else:
                raise _coded_error(ValueError, 'invalid_runner_data', f"Unknown RMS generator: {gen['class']}")
            self.devices.append(device)
            self.initial_values[device.object_id] = power
        self.loads = {}
        plant_generators = [g for g in self.model['gens'] if g['id'] in bindings['controlled_genstat']
                 and g['id'] in active]
        if plant_generators:
            plant = MultiportPlant(self.root.resolve(), self.model, self.catalog, plant_generators)
            identifiers = {g['id'] for g in plant_generators}
            self.devices = [d for d in self.devices if d.object_id not in identifiers]
            self.devices.append(plant)
            self.coverage = [row for row in self.coverage if row['object_id'] not in identifiers]
        active = set(self.network.topology["active"]["loads"])
        for load in self.model["loads"]:
            if load["id"] in active:
                device = RMSLoad.from_model(load, s_base_mva=self.s_base_mva)
                self.devices.append(device)
                self.loads[load["id"]] = device
                self.initial_values[load["id"]] = complex(*elements.load_pq_mw(load))
        self.dsl_coverage = self._build_dsl_coverage(bindings)
        self.solver = DAESolver(self.network, self.devices, step_s=dt_s, restart_euler=False,
                                linear_solver=linear_solver, retry_steps=retry_steps,
                                min_step_s=min_step_s, max_step_retries=max_step_retries)
        refs = [(dev, xs) for dev, xs in zip(self.devices, self.solver.x_slices)
                if isinstance(dev, ControlledSynchronousMachine) and int(dev.machine.elm.get('ip_ctrl', 0)) == 1]
        if boundary_voltages is not None:
            self.solver.voltage_provider = lambda t: boundary_voltages
        if len(refs) != 1 and boundary_voltages is None:
            raise _coded_error(ValueError, 'invalid_runner_data', 'An unambiguous RMS reference machine is required')
        self.reference_speed_index = (refs[0][1].start+refs[0][0].machine.n_states-2
                                      if refs else 0)
        self.reference_angle_index = self.reference_speed_index+1
        self.solver.reference_speed_index = self.reference_speed_index if refs else None
        self.pf_post_event_steps = True
        states = []
        for dev in self.devices:
            if isinstance(dev, MultiportPlant):
                initialized = dev.initialize_ports(self.lf_v, self.initial_values,
                    self.solver._ports(dev, self.network.voltage_vector(self.lf_v)))
            elif callable(getattr(dev, "init_multi", None)):
                initialized = dev.init_multi(self.lf_v, self.initial_values[dev.object_id])
            else:
                initialized = dev.init(self.lf_v[dev.bus_id],
                                       self.initial_values[dev.object_id])
            states.append(np.asarray(initialized, dtype=float))
            if isinstance(dev, StaticGeneratorRMS) and dev.controlled:
                dev.set_current_reference(dev._id_ref, dev._iq_ref)
        self.start_s = float(self.reference_report["rms_options"]["tstart"] if tstart is None else tstart)
        initial_z = np.concatenate([getattr(d, 'initial_z', np.zeros(d.n_alg)) for d in self.devices])
        trial = DAEState(self.start_s, np.concatenate(states), initial_z,
                         self.network.voltage_vector(self.lf_v))
        self.raw_initial_derivative, _, self.raw_initial_network_residual = self.solver._evaluate(
            trial.t, trial.x, trial.z, trial.v)
        self.state, self.init_metrics = self.solver.solve_algebraic(trial)
        self.state, self.equilibrium_report = self.solver.solve_equilibrium(self.state)
        self.initial_derivative, _, self.initial_network_residual = self.solver._evaluate(
            self.state.t, self.state.x, self.state.z, self.state.v)
        self.initial_derivative_by_device = sorted(
            ({"object_id": dev.object_id, "path": dev.relative_path,
              "max_abs": float(np.max(abs(self.initial_derivative[xs]))) if xs.stop > xs.start else 0.0}
             for dev, xs in zip(self.devices, self.solver.x_slices)),
            key=lambda row: row["max_abs"], reverse=True)
        self.raw_initial_network_by_node = sorted(
            ({"node_id": node, "abs_pu": float(abs(complex(self.raw_initial_network_residual[i],
                                                                 self.raw_initial_network_residual[i+len(self.network.node_ids)])))}
             for i, node in enumerate(self.network.node_ids)),
            key=lambda row: row["abs_pu"], reverse=True)
        self._accept_controls(self.state, initializing=True)

    def _build_dsl_coverage(self, bindings: dict) -> list[dict]:
        """Inventario por ID PF: solo se marca acoplado si el DAE lo ejecuta."""
        coupled = set()
        for device in self.devices:
            if isinstance(device, ControlledSynchronousMachine):
                coupled.update(dsl.object_id for dsl in (device.avr, device.governor)
                               if dsl is not None)
                coupled.update(dsl.object_id for dsl in device.frame_blocks)
            elif isinstance(device, WECCPVAdapter):
                controller = device.controller
                coupled.update(dsl.object_id for dsl in (
                    controller.reec, controller.regc, controller.protection))
            elif isinstance(device, MultiportPlant):
                coupled.update(b.object_id for b in device.blocks.values())
        without_generator = set()
        for comp_id in bindings["wecc_without_generator"]:
            comp = self.catalog.by_id[comp_id]
            for element in self.catalog.attrs(comp).get("pelm", ()):
                if isinstance(element, dict) and element.get("class") == "ElmDsl":
                    row = self.catalog.objects.get(element.get("relative_path"))
                    if row is not None:
                        without_generator.add(row["object_id"])
        result = []
        for row in self.catalog.objects.values():
            if (row["class"] != "ElmDsl" or not row["relative_path"].startswith("Network Model.")
                    or int(self.catalog.attrs(row).get("outserv", 0)) != 0):
                continue
            identifier = row["object_id"]
            if identifier in coupled:
                status, cause = 'coupled', 'Executed in DynamicDevice'
            elif identifier in without_generator:
                status, cause = 'No PF generator', "ElmComp.pelm[0]=null"
            else:
                status, cause = 'pending', 'Frame still lacks a complete DAE port'
            result.append({"object_id": identifier, "relative_path": row["relative_path"],
                           "status": status, "cause": cause})
        return sorted(result, key=lambda item: item["relative_path"])

    def _accept_controls(self, state: DAEState, *, initializing=False) -> bool:
        """Confirma memoria DSL solo en estados aceptados del DAE."""
        changed = False
        for dev, xs, zs in zip(self.devices, self.solver.x_slices, self.solver.z_slices):
            if not self.solver.in_service[dev.object_id]:
                continue
            accept = getattr(dev, "accept_step", None)
            accept_multi = getattr(dev, "accept_step_multi", None)
            if isinstance(dev, MultiportPlant):
                changed |= accept_multi(state.t, state.x[xs], self.solver._ports(dev, state.v),
                                        state.z[zs], initializing=initializing)
            elif accept_multi is not None:
                accept_multi(state.t, state.x[xs], self.solver._ports(dev, state.v))
            elif accept is not None:
                bus_v = complex(state.v[self.network.index_of_bus(dev.bus_id)])
                # Native hybrid devices can explicitly request a KCL refresh
                # after an accepted relay/FRT transition. Legacy None stays false.
                changed |= accept(state.t, state.x[xs], bus_v) is True
        return changed

    def _next_control_event(self, after_s: float) -> float | None:
        """Próximo vencimiento de memoria DSL, si existe."""
        times = []
        for dev in self.devices:
            if not self.solver.in_service[dev.object_id]:
                continue
            next_time = getattr(dev, "next_event_time", None)
            if next_time is not None:
                value = next_time(after_s)
                if value is not None and value > after_s + 1e-12:
                    times.append(float(value))
        return min(times, default=None)

    def _apply(self, event: Event) -> None:
        if event.kind == 'gen_trip':
            self.solver.trip_generator(event.target_id)
        elif event.kind == "load_step":
            dp, dq = event.value
            self.loads[event.target_id].set_step_percent(dp_percent=dp, dq_percent=dq)
        elif event.kind in ('element_switch_open', 'element_switch_close'):
            self.network.set_element_switching(event.target_id, event.kind.endswith('_open'))
        elif event.kind in ('switch_open', 'switch_close'):
            self.network.set_switch_open(event.target_id, event.kind.endswith('_open'))
        elif event.kind in ('branch_open', 'branch_close'):
            self.network.set_branch_open(event.target_id, event.kind.endswith('_open'))
        elif event.kind == "fault_apply":
            self.network.set_fault(event.target_id, complex(event.value))
        elif event.kind == "fault_clear":
            self.network.set_fault(event.target_id, None)
        else:
            raise _coded_error(ValueError, 'invalid_runner_data', f'Unsupported event type: {event.kind}')

    def _signals(self, state: DAEState, previous: DAEState | None) -> dict[str, float]:
        result = {"time|b:tnow|s": state.t}
        bus_v = self.network.bus_voltages(state.v)
        prev_v = self.network.bus_voltages(previous.v) if previous is not None else None
        for bid, v in bus_v.items():
            path = self.buses[bid]["path"]
            u_key = f"{path}|m:u|p.u."
            f_key = f"{path}|m:fehz|Hz"
            angle_key = f"{path}|m:phiu|deg"
            if abs(v) < 1e-12:
                # Voltage magnitude remains defined. Phase and frequency do not.
                for key in (angle_key, f_key):
                    if key in self.monitored:
                        result[key] = float('nan')
                if u_key in self.monitored:
                    result[u_key] = abs(v)
                continue
            if angle_key in self.monitored:
                result[angle_key] = math.degrees(cmath.phase(v*cmath.exp(
                    -1j*(state.x[self.reference_angle_index]-self.state.x[self.reference_angle_index]))))
            if u_key in self.monitored:
                result[u_key] = abs(v)
            if f_key not in self.monitored:
                continue
            history = getattr(self, '_frequency_history', None)
            if history is not None and abs(history.time_s-state.t) < 1e-12:
                f_hz = history.frequency[bid]
            elif prev_v is None or previous.t == state.t:
                f_hz = float(self.model["meta"]["f_nom_hz"])
            else:
                angle = cmath.phase(v / prev_v[bid]) if abs(prev_v[bid]) and abs(v) else 0.0
                f_hz = float(self.model["meta"]["f_nom_hz"]) + angle / (2 * math.pi * (state.t - previous.t))
            result[f_key] = f_hz
        for dev, xs, zs in zip(self.devices, self.solver.x_slices, self.solver.z_slices):
            if isinstance(dev, ControlledSynchronousMachine):
                key = f'{dev.relative_path}|s:xspeed|p.u.'
                if key in self.monitored:
                    result[key] = float(state.x[xs.start+dev.machine.n_states-2])
                angle_key = f'{dev.relative_path}|s:phi|deg'
                if angle_key in self.monitored:
                    result[angle_key] = math.degrees(float(state.x[xs.start+dev.machine.n_states-1]))
                control_keys = [f'{dev.relative_path}|s:{var}|p.u.' for var in ('ve', 'pt')]
                if any(key in self.monitored for key in control_keys):
                    # Evaluar en el tiempo real, outputs() usa t=0 por diseño.
                    saved_controls = dev.machine.ve, dev.machine.pt
                    try:
                        if self.solver.in_service[dev.object_id]:
                            dev._controls(state.t, state.x[xs], bus_v[dev.bus_id])
                            dev._frame_controls(state.t, state.x[xs], bus_v[dev.bus_id])
                        for key, value in zip(control_keys, (dev.machine.ve, dev.machine.pt)):
                            if key in self.monitored:
                                result[key] = float(value)
                    finally:
                        dev.machine.ve, dev.machine.pt = saved_controls
            if isinstance(dev, RMSLoad):
                keys = [f'{dev.relative_path}|m:{var}:bus1|{unit}'
                        for var, unit in (('P', 'MW'), ('Q', 'Mvar'))]
                if any(key in self.monitored for key in keys):
                    s = (0j if dev.object_id in self.network.deenergized_device_ids else
                         dev.consumed_power_mva(bus_v[dev.bus_id]))
                    for key, value in zip(keys, (s.real, s.imag)):
                        if key in self.monitored:
                            result[key] = float(value)
                continue
            if isinstance(dev, MultiportPlant):
                currents = (dev._currents(state.x[xs], state.z[zs], self.solver._ports(dev, state.v))
                            if self.solver.in_service[dev.object_id] else
                            {g.relative_path: 0j for g in dev.generators})
                for gen in dev.generators:
                    power = bus_v[gen.bus_id]*currents[gen.relative_path].conjugate()*self.s_base_mva
                    for variable, unit, value in (('P', 'MW', power.real), ('Q', 'Mvar', power.imag)):
                        key = f'{gen.relative_path}|m:{variable}:bus1|{unit}'
                        if key in self.monitored:
                            result[key] = value
                continue
            v = bus_v[dev.bus_id]
            p_key = f"{dev.relative_path}|m:P:bus1|MW"
            q_key = f"{dev.relative_path}|m:Q:bus1|Mvar"
            if p_key not in self.monitored and q_key not in self.monitored:
                continue
            s = (v * dev.current_injection(state.x[xs], v).conjugate() * self.s_base_mva
                 if self.solver.in_service[dev.object_id] else 0j)
            if p_key in self.monitored:
                result[p_key] = s.real
            if q_key in self.monitored:
                result[q_key] = s.imag
        active_branches = set(self.network.topology["active"]["branches"])
        for branch in self.model["branches"]:
            if branch["id"] not in active_branches:
                continue
            path = branch["path"]
            keys = tuple(f"{path}|m:{kind}:bus{side}|{unit}" for side in (1, 2)
                         for kind, unit in (("P", "MW"), ("Q", "Mvar")))
            if not any(key in self.monitored for key in keys):
                continue
            ids, _, matrix = _stamp(branch, self.buses, self.s_base_mva)
            voltage = np.asarray([bus_v[bid] for bid in ids], dtype=complex)
            current = matrix @ voltage
            power = voltage[0] * current[0].conjugate() * self.s_base_mva
            if keys[0] in self.monitored:
                result[keys[0]] = power.real
            if keys[1] in self.monitored:
                result[keys[1]] = power.imag
            power2 = voltage[1] * current[1].conjugate() * self.s_base_mva
            if keys[2] in self.monitored:
                result[keys[2]] = power2.real
            if keys[3] in self.monitored:
                result[keys[3]] = power2.imag
        if self.external_signals:
            self._additional_branch_signals(state, bus_v, result)
            # Elementos fuera de servicio conservan columnas y valor cero.
            for key in sorted(self.monitored):
                result.setdefault(key, 0.0)
        return result

    def _additional_branch_signals(self, state, bus_v, result):
        """Flujos 3W, extremos abiertos e ideales, sin alterar señales históricas."""
        from power_machine.results import branch_result, reconstruct_ideal_flows

        topology = self.network.topology
        items = {b['id']: b for group in ('branches', 'branches3') for b in self.model[group]}
        missing_paths = {key.rsplit('|', 2)[0] for key in self.monitored if key not in result}
        if not any(item['path'] in missing_paths for item in items.values()):
            return
        ideal = topology['ideal_edges']
        wanted_ideal = any(any(key.startswith(items[e['id']]['path']+'|')
                              for key in self.monitored)
                           for e in ideal if e['id'] in items)
        rows = []
        for group in ('branches', 'branches3'):
            ids = set(topology['active'][group])
            if group == 'branches':
                ids.update(topology['dangling_branches'])
            for bid in sorted(ids):
                item = items[bid]
                if not wanted_ideal and not any(k.startswith(item['path']+'|') for k in self.monitored):
                    continue
                terminals, un, matrix = _stamp(item, self.buses, self.s_base_mva)
                voltage = [bus_v.get(b, 0j) for b in terminals]
                if bid in topology['dangling_branches']:
                    connected = topology['dangling_branches'][bid]
                    other = 1-connected
                    voltage[other] = -matrix[other, connected]/matrix[other, other]*voltage[connected]
                rows.append(branch_result(item, voltage, matrix, un, self.s_base_mva, 0))
        if wanted_ideal:
            gens, loads, shunts = [], [], {}
            for dev, xs, zs in zip(self.devices, self.solver.x_slices, self.solver.z_slices):
                if not self.solver.in_service[dev.object_id]:
                    continue
                if isinstance(dev, RMSLoad):
                    s = (0j if dev.object_id in self.network.deenergized_device_ids else
                         dev.consumed_power_mva(bus_v[dev.bus_id]))
                    loads.append({'bus': dev.bus_id, 'p_mw': s.real, 'q_mvar': s.imag})
                elif isinstance(dev, MultiportPlant):
                    currents = dev._currents(state.x[xs], state.z[zs], self.solver._ports(dev, state.v))
                    for gen in dev.generators:
                        s = bus_v[gen.bus_id]*currents[gen.relative_path].conjugate()*self.s_base_mva
                        gens.append({'bus': gen.bus_id, 'p_mw': s.real, 'q_mvar': s.imag})
                else:
                    v = bus_v[dev.bus_id]
                    s = v*dev.current_injection(state.x[xs], v).conjugate()*self.s_base_mva
                    gens.append({'bus': dev.bus_id, 'p_mw': s.real, 'q_mvar': s.imag})
            for item in self.model['shunts']:
                if item['id'] in topology['active']['shunts']:
                    bid = item['bus']
                    y = elements.y_shunt(item, self.buses[bid]['un_kv'], self.s_base_mva)
                    shunts[bid] = shunts.get(bid, 0j)+abs(bus_v[bid])**2*y.conjugate()*self.s_base_mva
            for bid, z in self.network.faults.items():
                shunts[bid] = shunts.get(bid, 0j)+abs(bus_v[bid])**2*(1/z).conjugate()*self.s_base_mva
            ideal_rows, _ = reconstruct_ideal_flows(
                ideal, topology['bus_to_node'], topology['node_to_island'],
                dict(zip(self.network.node_ids, state.v)), self.buses, rows, gens, loads, shunts)
            rows.extend(ideal_rows)
        for row in rows:
            item = items.get(row['id'])
            if item is None:
                continue
            labels = ('hv', 'mv', 'lv') if item['class'] == 'ElmTr3' else ('from', 'to')
            ports = ('bushv', 'busmv', 'buslv') if item['class'] == 'ElmTr3' else ('bus1', 'bus2')
            for label, port in zip(labels, ports):
                for var, unit, suffix in (('P', 'MW', 'mw'), ('Q', 'Mvar', 'mvar')):
                    key = f"{item['path']}|m:{var}:{port}|{unit}"
                    if key in self.monitored and key not in result:
                        result[key] = float(row[f'{var.lower()}_{label}_{suffix}'])

    def _accept_frequency(self, state: DAEState, *, restart=False) -> None:
        """Convierte fasores nominales al marco del rotor de referencia solo para m:fehz."""
        rotation = cmath.exp(-1j*state.x[self.reference_angle_index])
        voltage = {bid:v*rotation for bid,v in self.network.bus_voltages(state.v).items()}
        self._frequency_history.accept(state.t, voltage, restart=restart,
            f_ref_pu=float(state.x[self.reference_speed_index]))

    def run(self, *, end_s: float | None = None, output: Path | None = None,
            output_every: int = 1, on_header=None, on_samples=None, on_progress=None,
            cancel=None, batch: int = 50) -> dict:
        'Integrate accepted steps and write CSV and diagnostics even when a step fails. Output cadence is independent of nominal integration step.'
        if output_every < 1:
            raise _coded_error(ValueError, 'invalid_runner_data', 'output_every must be a positive integer')
        if type(batch) is not int or batch < 1:
            raise _coded_error(ValueError, 'invalid_runner_data', 'batch must be a positive integer')
        final_s = float(self.reference_report["tstop_s"] if end_s is None else end_s)
        if final_s < self.start_s or any(e.time_s > final_s for e in self.events if end_s is None):
            raise _coded_error(ValueError, 'invalid_runner_data', 'End time does not cover the events')
        from power_machine.resources import work_path
        out = Path(output) if output is not None else work_path("rms") / self.case
        out.mkdir(parents=True, exist_ok=True)
        start = time.perf_counter()
        state = self.state.copy()
        # Separar el equilibrio de ComInc del primer flanco inc->h(tstart).
        # No volver a imponer f=0 después de un evento discreto identificable.
        startup_changed = self._accept_controls(state)
        if startup_changed:
            state, _ = self.solver.solve_algebraic(state)
            self.solver.restart_discrete()
        self._frequency_history = PublishedFrequencyHistory(
            f_nom_hz=float(self.model['meta']['f_nom_hz']))
        self._accept_frequency(state, restart=True)
        post_event_steps = PostEventSteps(self.solver.step_s)
        startup_f, startup_g, startup_kcl = self.solver._evaluate(
            state.t, state.x, state.z, state.v)
        startup = {'time_s': state.t, 'sample_hold_changed': startup_changed,
                   'max_derivative_after': float(np.max(abs(startup_f), initial=0.)),
                   'max_algebraic_residual_after': float(np.max(abs(startup_g), initial=0.)),
                   'max_network_residual_after_pu': float(np.max(abs(startup_kcl), initial=0.))}
        events = [e for e in self.events if e.time_s <= final_s]
        position, steps, sample_count, discontinuities = 0, 0, 0, []
        previous = None
        failure = None
        failure_code = None
        method_counts = {}
        integration_method = "trapezoidal"
        pre_event_shortcut = False
        first = self._signals(state, None)
        fields = list(first)
        cancelled = False
        pending = []
        last_progress = start

        def stop_requested():
            nonlocal cancelled, last_progress
            now = time.perf_counter()
            if on_progress is not None and now-last_progress >= 0.25:
                on_progress(float(state.t), final_s, (now-start)*1000/max(steps, 1))
                last_progress = now
            cancelled = cancel is not None and cancel.is_set()
            return cancelled

        with (out / "signals.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            if on_header is not None:
                on_header(list(fields))

            def write_row(row):
                writer.writerow(row)
                if on_samples is not None:
                    pending.append([float(row.get(k, 0.0)) for k in fields])
                    if len(pending) >= batch:
                        handle.flush()
                        on_samples(pending[:])
                        pending.clear()

            try:
                # En un caso autónomo sin evento, el equilibrio es solución exacta.
                # Verificarlo también al final antes de evitar pasos de Newton.
                if (not events and self._next_control_event(state.t) is None
                        and max(abs(startup_f), default=0.0) < 1e-8):
                    end_derivative, _, _ = self.solver._evaluate(final_s, state.x, state.z, state.v)
                    if max(abs(end_derivative), default=0.0) < 1e-8:
                        integration_method = "equilibrio_estacionario"
                        template = dict(first)
                        while state.t < final_s-1e-12:
                            if stop_requested():
                                break
                            if steps % output_every == 0:
                                write_row(template)
                                sample_count += 1
                            state.t = min(final_s, state.t+self.solver.step_s)
                            template["time|b:tnow|s"] = state.t
                            steps += 1
                        write_row(template)
                        sample_count += 1
                if integration_method == "trapezoidal":
                    if (events and (self._next_control_event(state.t) is None or
                                    self._next_control_event(state.t) > events[0].time_s)
                            and max(abs(startup_f), default=0.0) < 1e-8):
                        pre_derivative, _, _ = self.solver._evaluate(
                            events[0].time_s, state.x, state.z, state.v)
                        if max(abs(pre_derivative), default=0.0) < 1e-8:
                            pre_event_shortcut = True
                            template = dict(first)
                            while state.t < events[0].time_s-1e-12:
                                if stop_requested():
                                    break
                                if steps % output_every == 0:
                                    write_row(template)
                                    sample_count += 1
                                state.t = min(events[0].time_s, state.t+self.solver.step_s)
                                template["time|b:tnow|s"] = state.t
                                steps += 1
                    while True:
                        if stop_requested():
                            # Conserva también el último estado aceptado.
                            write_row(self._signals(state, previous))
                            sample_count += 1
                            break
                        at_event = (position < len(events)
                                    and abs(events[position].time_s-state.t) < 1e-12)
                        if (steps % output_every == 0 or at_event
                                or state.t >= final_s-1e-12 or previous is None):
                            row = first if sample_count == 0 else self._signals(state, previous)
                            write_row({key: row.get(key, 0.0) for key in fields})
                            sample_count += 1
                        if position < len(events) and abs(events[position].time_s-state.t) < 1e-12:
                            old_v = self.network.bus_voltages(state.v)
                            at = state.t
                            while position < len(events) and abs(events[position].time_s-at) < 1e-12:
                                self._apply(events[position])
                                position += 1
                            self.solver.refresh_network_indexes()
                            trial = DAEState(at, state.x.copy(), state.z.copy(),
                                             self.network.voltage_vector(old_v))
                            state, _ = self.solver.solve_algebraic(trial)
                            if self._accept_controls(state):
                                state, _ = self.solver.solve_algebraic(state)
                            self.solver.restart_discrete()
                            self._accept_frequency(state, restart=True)
                            if self.pf_post_event_steps:
                                post_event_steps.restart()
                            discontinuities.append({"time_s": at, "before_index": sample_count-1,
                                                    "after_index": sample_count,
                                                    "deenergized_bus_ids": sorted(self.network.deenergized_bus_ids)})
                            previous = None
                            continue
                        if state.t >= final_s-1e-12:
                            break
                        target = min(final_s, events[position].time_s if position < len(events) else final_s)
                        control_event = self._next_control_event(state.t)
                        if control_event is not None:
                            target = min(target, control_event)
                        h = min(post_event_steps.next(), target-state.t)
                        previous = state
                        internal_before = len(self.solver.internal_events)
                        state, metric = self.solver.step(state, h)
                        method_counts[metric.method] = method_counts.get(metric.method, 0)+1
                        if abs(state.t-target) < 1e-12:
                            state.t = target
                        # Medir el tramo continuo ANTES de cualquier captura de
                        # controles. La reinicialización posterior solo rebasa V.
                        self._accept_frequency(state)
                        sampled = self._accept_controls(state)
                        internal_crossing = len(self.solver.internal_events) > internal_before
                        if internal_crossing:
                            post_event_steps.restart()
                        if sampled or internal_crossing or (control_event is not None and abs(state.t-control_event) < 1e-12):
                            _, local_residual, network_residual = self.solver._evaluate(
                                state.t, state.x, state.z, state.v)
                            if max(np.max(abs(local_residual), initial=0.),
                                   np.max(abs(network_residual), initial=0.)) >= 1e-8:
                                state, _ = self.solver.solve_algebraic(state)
                            self.solver.restart_discrete()
                            self._accept_frequency(state, restart=True)
                        steps += 1
            except Exception as exc:
                failure = f"{type(exc).__name__}: {exc}"
                failure_code = getattr(exc, "code", "rms_runtime_error")
            if pending and on_samples is not None:
                on_samples(pending[:])
        elapsed = time.perf_counter()-start
        if on_progress is not None:
            on_progress(float(state.t), final_s, elapsed*1000/max(steps, 1))
        report = {"case": self.case, "dt_s": self.solver.step_s,
                  "linear_solver": self.solver.linear_solver,
                  "step_retries": list(self.solver.step_retries),
                  "completed": failure is None and not cancelled and state.t >= final_s-1e-12,
                  "cancelled": bool(cancelled), "wall_s": elapsed,
                  "ms_per_step": elapsed*1000/max(steps, 1),
                  "output_every": output_every, "requested_end_s": final_s,
                  "last_time_s": state.t, "steps": steps, "samples": sample_count,
                  "integration_method": integration_method,
                  "step_method_counts": method_counts,
                  "frequency_discontinuity_rule": 'PF-04: 50/50 filter, g[n-1], pre-event memory and reference rotor frame',
                  "post_event_steps": "h/2,2h/3,h" if self.pf_post_event_steps else "h",
                  "norton_frame": 'Reference rotor-frame lag transformed to nominal coordinates',
                  "pre_event_equilibrium_shortcut": pre_event_shortcut,
                  "compute_time_s": elapsed, "failure": failure, "failure_code": failure_code,
                  "startup": startup,
                  "init_max_derivative": float(np.max(abs(self.initial_derivative))),
                  "init_max_network_residual_pu": float(np.max(abs(self.initial_network_residual))),
                  "raw_lf_max_network_residual_pu": float(np.max(abs(self.raw_initial_network_residual))),
                  "raw_lf_network_by_node": self.raw_initial_network_by_node[:10],
                  "init_derivative_by_device": self.initial_derivative_by_device,
                  "init_newton_scaled_residual": self.init_metrics.scaled_residual,
                  "equilibrium_refinement": self.equilibrium_report,
                  "raw_inc_max_derivative": float(np.max(abs(self.raw_initial_derivative))),
                  "coverage_gaps": self.coverage, "dsl_coverage": self.dsl_coverage,
                  "coupled_dsl_count": sum(row["status"] == 'coupled' for row in self.dsl_coverage),
                  "wecc_lf_provenance": [
                      {"object_id": dev.object_id, "relative_path": dev.relative_path,
                       "power_mva": [dev.discrete_state["lf_power_mva"].real,
                                     dev.discrete_state["lf_power_mva"].imag],
                       "source": dev.discrete_state["lf_provenance"]}
                      for dev in self.devices if isinstance(dev, WECCPVAdapter)],
                  "discontinuities": discontinuities}
        report['disconnected_devices'] = [
            {'object_id': d.object_id, 'relative_path': d.relative_path}
            for d in self.devices if not self.solver.in_service[d.object_id]]
        report['internal_events'] = self.solver.internal_events
        report["override_warnings"] = self.override_warnings
        (out / "run_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False),
                                               encoding="utf-8")
        return report
