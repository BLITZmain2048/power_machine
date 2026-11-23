'Adapter support for power-system modal analysis.'
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
from pathlib import Path
import numpy as np

from .schema import OperatingPoint, StateRef, diagnostic, fail, real_vector


WORKSPACE = Path(__file__).resolve().parents[2]
MEMORY_FUNCTIONS = {"delay", "movingavg", "lastvalue", "picdro", "picdro_const",
                    "flipflop", "aflipflop", "gradlim_const", "sapprox", "event", "output", "time"}


class LinearDAEAdapter:
    'Explicit real matrix DAE for analytical tests, with f in state units per second.'
    def __init__(self, fx, fz, gx, gz):
        matrices = []
        for name, value in zip(("fx", "fz", "gx", "gz"), (fx, fz, gx, gz)):
            raw = np.asarray(value)
            if raw.ndim != 2 or np.iscomplexobj(raw) or raw.dtype.kind not in "fiu":
                fail('invalid_input', 'real_matrix_required', ' must be a real matrix')
            if not np.isfinite(raw).all():
                fail('invalid_input', 'nonfinite_matrix', ' contains nonfinite values')
            matrices.append(raw.astype(float, copy=True))
        self.fx, self.fz, self.gx, self.gz = matrices
        n, m = self.fx.shape[0], self.gz.shape[0]
        if any(value.shape != shape for value, shape in zip(matrices, ((n, n), (n, m), (m, n), (m, m)))):
            fail('invalid_input', 'matrix_dimensions', 'DAE block dimensions are inconsistent')

    def clone(self):
        return copy.deepcopy(self)

    def evaluate(self, x, z, time_s):
        return self.fx@x+self.fz@z, self.gx@x+self.gz@z


def inside_root(path, root=WORKSPACE):
    'Resolve paths and reject escapes from the supplied root, including existing links.'
    root = Path(root).resolve()
    value = Path(path)
    resolved = (value if value.is_absolute() else root/value).resolve()
    if not resolved.is_relative_to(root):
        fail('invalid_input', 'outside_root', 'Path must remain inside the working root')
    return resolved


def file_input(path, root):
    path = inside_root(path, root)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"path": path.relative_to(Path(root).resolve()).as_posix(), "sha256": digest}


def complex_input(value, unit):
    'Read a serialized phasor with explicit units. Do not reinterpret voltage, power or base.'
    if not isinstance(value, dict) or value.get("unit") != unit:
        fail('invalid_input', 'complex_unit', 'The complex value requires explicit unit ')
    parts = real_vector([value["real"], value["imag"]], 'Complex components')
    return complex(parts[0], parts[1])


def _blocks(device):
    from power_machine.dynamics.dsl_switching import device_blocks
    return device_blocks(device)


def _graph_features(block):
    """Inspección recursiva del IR real, incluso subgrafos aún no instanciados."""
    functions = set(block.ir.functions)
    kinds = set()
    for node in block.nodes:
        kinds.add(node.kind)
        if node.ir is not None:
            functions.update(node.ir.functions)
        if node.subgraph is not None:
            child_functions, child_kinds = _graph_features(node.subgraph)
            functions.update(child_functions)
            kinds.update(child_kinds)
    return functions & MEMORY_FUNCTIONS, kinds & {"ElmSamp", "ElmClock", "BlkDelay", "BlkSamp", "BlkFlipFlop"}


def inspect_solver(solver):
    'Report coverage by ID without treating hybrid devices as a continuous DAE.'
    coverage, blockers = [], []
    supported = {"SynchronousMachine", "ControlledSynchronousMachine", "StaticGeneratorRMS", "RMSLoad"}
    for dev in solver.devices:
        identity = {"object_id": dev.object_id, "relative_path": dev.relative_path}
        reasons = []
        if not solver.in_service.get(dev.object_id, True):
            reasons.append(("inactive_dynamic_device", 'A disconnected device retains frozen states'))
        if type(dev).__name__ not in supported:
            reasons.append(("device_profile_unreviewed", 'Continuous class has no reviewed modal profile: '))
        if getattr(dev, "samplers", None) or getattr(dev, "clock", None):
            reasons.append(("sample_hold", 'Sample&Hold or a clock prevents an equivalent continuous DAE'))
        for sampler in getattr(dev, "samplers", {}).values():
            row = {"object_id": sampler.object_id, "relative_path": sampler.relative_path,
                   "code": "sample_hold", "message": 'Sample&Hold prevents an equivalent continuous DAE'}
            blockers.append(row)
            coverage.append({**row, "status": "excluded"})
        for block, _ in _blocks(dev):
            functions, kinds = _graph_features(block)
            if functions or kinds:
                row = {"object_id": block.object_id, "relative_path": block.relative_path,
                       "code": "unsupported_hybrid_dsl",
                       "message": 'Historical or discrete functions, or varying inputs: '+", ".join(sorted(functions|kinds))}
                blockers.append(row)
                coverage.append({**row, "status": "excluded"})
                reasons.append(("hybrid_controller", 'Device depends on an excluded controller'))
        held = getattr(dev, "discrete_state", {}).get("held_ve_pt", False)
        if held:
            reasons.append(("frozen_controller", 'Runtime retains controls without dynamic coupling'))
        for code, message in reasons:
            blockers.append({**identity, "code": code, "message": message})
        coverage.append({**identity, "class": type(dev).__name__,
                         "status": "excluded" if reasons else "continuous_candidate"})
    return coverage, blockers


def state_refs(solver, *, supplied=None):
    'Return native rotor names and units. Preserve DSL identity without inferring missing units.'
    supplied = {int(row["index"]): row for row in (supplied or [])}
    refs = []
    for dev, xs in zip(solver.devices, solver.x_slices):
        native = getattr(dev, "machine", dev)
        machine = type(native).__name__ == "SynchronousMachine"
        names = ["psi_fd", "psi_1d", "psi_1q"]
        if machine and native.n_states == 6:
            names.append("psi_2q")
        names += ["speed_pu", "rotor_angle_rad"]
        local = {}
        if machine:
            for i, name in enumerate(names):
                local[i] = (native.object_id, native.relative_path, name,
                            "rad" if name == "rotor_angle_rad" else "p.u.")
        for block, offset in _blocks(dev):
            for i, name in enumerate(block.state_names):
                local[offset+i] = (block.object_id, block.relative_path, name, 'undeclared DSL unit')
        for i in range(xs.stop-xs.start):
            index = xs.start+i
            oid, path, name, unit = local.get(i, (dev.object_id, dev.relative_path,
                                                 f"state_{i}", 'undeclared unit'))
            scale = 1.0
            if index in supplied:
                row = supplied[index]
                if row.get("object_id", oid) != oid or row.get("relative_path", path) != path:
                    fail('invalid_input', 'state_identity', 'Supplied metadata changes state identity')
                if 'undeclared' not in unit and row["unit"] != unit:
                    fail('invalid_input', 'state_unit_conversion_required',
                         'Native unit cannot change without converting the state and its equations')
                unit, scale = row["unit"], float(row["scale"])
            refs.append(StateRef(index, oid, path, name, unit, scale))
    return tuple(refs)


class RMSDAEAdapter:
    'Wrap the RMS residual and its reference-frequency policy. z=[z_local, Re(V), Im(V)] and g=[g_local, Re(I-YV), Im(I-YV)]. Clone runtime memory while sharing immutable AST identity.'
    def __init__(self, solver):
        self.solver = self._copy_solver(solver)
        self.nz_local = solver.nz
        self.nv = len(solver.network.node_ids)

    def clone(self):
        clone = object.__new__(type(self))
        clone.solver = self._copy_solver(self.solver)
        clone.nz_local, clone.nv = self.nz_local, self.nv
        # Los planes se regeneran sobre el runtime privado. El AST congelado
        # se comparte para conservar las claves id(call) de los historiales.
        for dev in clone.solver.devices:
            if hasattr(dev, "_cache"):
                dev._cache = None
            for block, _ in _blocks(dev):
                clone._reset_block(block)
        return clone

    @staticmethod
    def _copy_solver(solver):
        memo = {}
        visited = set()

        def remember(block):
            if id(block) in visited:
                return
            visited.add(id(block))
            memo[id(block.ir)] = block.ir
            for node in block.nodes:
                if node.ir is not None:
                    memo[id(node.ir)] = node.ir
                if node.subgraph is not None:
                    remember(node.subgraph)
            for child in block._children.values():
                remember(child)
        for dev in getattr(solver, "devices", ()):
            for block, _ in _blocks(dev):
                remember(block)
        return copy.deepcopy(solver, memo)

    @staticmethod
    def _reset_block(block, visited=None):
        visited = set() if visited is None else visited
        if id(block) in visited:
            return
        visited.add(id(block))
        block._flat_plan = None
        block._graph_plan = None
        block._evaluation_cache = None
        block.memory.switch_modes.clear()
        for child in block._children.values():
            RMSDAEAdapter._reset_block(child, visited)
        for node in block.nodes:
            if node.subgraph is not None:
                RMSDAEAdapter._reset_block(node.subgraph, visited)

    def evaluate(self, x, z, time_s):
        x, z = real_vector(x, "x RMS"), real_vector(z, 'Real RMS z')
        if len(x) != self.solver.nx or len(z) != self.nz_local+2*self.nv:
            fail('invalid_input', 'rms_dimensions', 'State dimension does not match the RMS solver')
        v = z[self.nz_local:self.nz_local+self.nv]+1j*z[self.nz_local+self.nv:]
        f, g, net = self.solver._evaluate(time_s, x, z[:self.nz_local], v)
        return f, np.r_[g, net]


def point_from_solver(solver, state, *, metadata=None, supplied_states=None):
    refs = state_refs(solver, supplied=supplied_states)
    coverage, blockers = inspect_solver(solver)
    for ref in refs:
        if 'undeclared' in ref.unit:
            blockers.append({"code": "missing_state_unit", "object_id": ref.object_id,
                "relative_path": ref.relative_path,
                "message": 'Missing explicit unit and scale for '})
    nodes = list(solver.network.node_ids)
    alg = []
    for dev, zs in zip(solver.devices, solver.z_slices):
        for i in range(zs.start, zs.stop):
            alg.append({"kind": "local", "index": i, "object_id": dev.object_id,
                        "relative_path": dev.relative_path,
                        "unit": 'undeclared local algebraic unit'})
            blockers.append({"code": "missing_algebraic_unit", "object_id": dev.object_id,
                "relative_path": dev.relative_path,
                "message": 'Missing unit and scale for local algebraic residual '})
    bus_rows = getattr(solver.network, "model", {}).get("buses", [])
    mapping = getattr(solver.network, "bus_to_node", {})
    for part in ("real", "imag"):
        for node in nodes:
            members = [{"object_id": row.get("object_id", row.get("id")),
                        "relative_path": row.get("relative_path", row.get("path"))}
                       for row in bus_rows if mapping.get(row["id"], row["id"]) == node]
            alg.append({"kind": "voltage", "object_id": node,
                "relative_path": next((r["relative_path"] for r in members if r["object_id"] == node), None),
                "members": members, "part": part, "unit": "p.u.",
                "residual_unit": 'p.u. current on a 100 MVA base', "residual_scale": 1.})
    details = {"angular_frame": 'Rotor and phasors preserved, reference speed evaluated from x',
               "frequency_reference_index": solver.reference_speed_index,
               "algebraic_order": "[z_local, Re(V), Im(V)]",
               "residual_order": "[g_local, Re(I-YV), Im(I-YV)]",
               "algebraic_variables": alg, "coverage": coverage,
               "unsupported_features": blockers, **(metadata or {})}
    return OperatingPoint(state.x.copy(), np.r_[state.z, state.v.real, state.v.imag],
        float(state.t), refs, np.ones(solver.nz+2*len(nodes)), details)


def prepare_rms(root, model, study, input_data):
    'Initialize one real RMSRunner and wrap its DAE interface.'
    from power_machine.dynamics.runner import RMSRunner
    from power_machine.loadflow import _scenario
    from power_machine.study import StudyCase, run_loadflow
    case = StudyCase.from_json(study)
    if case.events:
        fail('unsupported', 'scheduled_events', 'Scheduled events do not form a continuous autonomous DAE')
    operational_model = _scenario(model, case.scenario)
    supplied = input_data.get("operating_point")
    if supplied is None:
        lf = run_loadflow(root, case, model=model)
        if any(row["status"] == "no_convergente" for row in lf["islands"]):
            fail('not_converged', 'loadflow_not_converged', 'Scenario load flow did not converge')
        resolved = [b for b in lf["buses"] if b.get("energized")]
        if not resolved:
            fail('not_converged', 'loadflow_no_solution', 'Load flow returned no energized buses')
        operating = {"bus_voltages": {b["id"]: float(b["u_pu"])*np.exp(1j*math.radians(b["angle_deg"]))
                     for b in resolved}, "generator_powers": {g["id"]: complex(g["p_mw"], g["q_mvar"])
                     for g in lf["gens"] if g.get("p_mw") is not None and g.get("q_mvar") is not None}}
    else:
        operating = {key: {oid: complex_input(value, unit) for oid, value in supplied[key].items()}
                     for key, unit in (("bus_voltages", "p.u."), ("generator_powers", "MW+jMvar"))}
    runner = RMSRunner(root, events=[], monitored=[], tstart=case.options.t_start_s,
                       model=operational_model, operating_point=operating)
    point = point_from_solver(runner.solver, runner.state, supplied_states=input_data.get("states"),
        metadata={"equilibrium_initialization": runner.equilibrium_report, "profile": "full_rms",
                  "reference_angle_index": runner.reference_angle_index,
                  "runner_coverage": runner.coverage, "dsl_coverage": runner.dsl_coverage})
    blockers = list(point.metadata["unsupported_features"])
    # El runtime histórico puede leer fuentes externas, que no se vuelven a
    # abrir ni se copian desde modal. Su procedencia incompleta bloquea el perfil.
    external_inputs = []
    for device in getattr(runner.solver, "devices", ()):
        native_path = getattr(device, "native_provenance", None)
        if native_path and not Path(native_path).resolve().is_relative_to(Path(root).resolve()):
            row = {"object_id": device.object_id, "relative_path": device.relative_path,
                   "path": str(native_path), "sha256": None,
                   "pf_comparison": 'not validated against PF'}
            external_inputs.append(row)
            blockers.append({"code": "external_runtime_input", "object_id": device.object_id,
                "relative_path": device.relative_path,
                "message": 'Runtime depends on external sources without a complete local fingerprint. Panel integration is required'})
    point.metadata["external_runtime_inputs"] = external_inputs
    point.metadata["provenance_complete"] = not bool(external_inputs)
    for row in runner.coverage:
        blockers.append({"code": "runtime_coverage_gap", "object_id": row["object_id"],
            "relative_path": row.get("relative_path", row.get("path")), "message": row["status"]})
    for row in runner.dsl_coverage:
        if row["status"] == 'pending':
            blockers.append({"code": "dsl_not_coupled", "object_id": row["object_id"],
                "relative_path": row["relative_path"], "message": row["cause"]})
    point.metadata["unsupported_features"] = blockers
    return RMSDAEAdapter(runner.solver), point


def prepare_machine(root, model, input_data):
    'Build an explicitly selected ElmSym with local admittance load. Use explicit operating data and list the excluded network and controllers by ID. This is a partial model, not a system equivalent.'
    from power_machine.dynamics.machine_init import initialize_machine
    from power_machine.dynamics.network import DynamicNetwork
    from power_machine.dynamics.solver import DAESolver, DAEState
    from power_machine.dynamics.synchronous import SynchronousMachine
    identifier = input_data["object_id"]
    gen = next((g for g in model["gens"] if g["id"] == identifier), None)
    if gen is None or gen.get("class") != "ElmSym" or gen.get("outserv"):
        fail('invalid_input', 'machine_selection', 'Select an in-service ElmSym by object_id')
    bus = next(b for b in model["buses"] if b["id"] == gen["bus"])
    operating = input_data.get("machine_operating_point")
    inputs = []
    if operating is None:
        # Esta opción lee la referencia almacenada y exige modelo base idéntico.
        stored_path = inside_root(root/"model.json", root)
        stored = json.loads(stored_path.read_text(encoding="utf-8"))
        inputs.append(file_input(stored_path, root))
        stored_gen = next(g for g in stored["gens"] if g["id"] == identifier)
        stored_bus = next(b for b in stored["buses"] if b["id"] == gen["bus"])
        if stored_gen != gen or stored_bus != bus:
            fail('invalid_input', 'reference_model_mismatch', 'Modified submodel requires its own operating point')
        paths = [root/"pf_reference/ldf_buses.csv", root/"pf_reference/ldf_injections.csv"]
        tables = []
        for path in paths:
            with path.open(encoding="utf-8-sig", newline="") as handle:
                tables.append({r["object_id"]: r for r in csv.DictReader(handle)})
            inputs.append(file_input(path, root))
        b, g = tables[0][bus["id"]], tables[1][identifier]
        voltage = float(b["m:u"])*np.exp(1j*math.radians(float(b["m:phiu"])))
        power = complex(float(g["m:P:bus1"]), float(g["m:Q:bus1"]))
    else:
        voltage = complex_input(operating["voltage_pu"], "p.u.")
        power = complex(operating["p_mw"], operating["q_mvar"])
    selectors = input_data.get("dynamic_attributes")
    if selectors is None:
        from power_machine.dynamics.schema import read_dynamic_export
        export = read_dynamic_export(root/"pf_dynamic")
        selectors = {r["attribute"]: json.loads(r["value_json"]) for r in export["attributes"]
                     if r["object_id"] == gen.get("type_id") and r["status"] == "PRESENT"
                     and r["attribute"] in {"model_inp", "iturbo", "isat", "isatSalient", "i_speedVar", "i_exactConv"}}
        inputs += [file_input(root/"pf_dynamic"/name, root)
                   for name in ("manifest.json", "objects.csv", "attributes.csv", "references.csv")]
    machine = SynchronousMachine(gen, bus_un_kv=bus["un_kv"],
        s_base_mva=model["meta"]["s_base_mva"], f_nom_hz=model["meta"]["f_nom_hz"], dynamic_attributes=selectors)
    initial = initialize_machine(machine, voltage, power)
    load = {"id": "modal_admittance_"+identifier, "class": "ElmLod", "bus": bus["id"],
            "outserv": 0, "p_mw": power.real, "q_mvar": power.imag, "scale": 1.,
            "params": {"plini": power.real, "qlini": power.imag, "scale0": 1.},
            "units": {"plini": "MW", "qlini": "Mvar", "scale0": "1"}}
    local_model = {"meta": copy.deepcopy(model["meta"]), "buses": [copy.deepcopy(bus)],
                   "gens": [copy.deepcopy(gen)], "loads": [load], "switches": [], "branches": [],
                   "branches3": [], "shunts": []}
    solver = DAESolver(DynamicNetwork(local_model, {bus["id"]: voltage}), [machine],
                       step_s=0.001, atol=1e-12, rtol=1e-11)
    state = DAEState(0., initial.state.copy(), np.empty(0), np.array([voltage]))
    state, _ = solver.solve_algebraic(state)
    exclusions = [{"object_id": row.get("object_id", row.get("id")),
                   "relative_path": row.get("relative_path", row.get("path")),
                   "status": "excluded", "message": 'Outside the explicit machine-admittance submodel'}
                  for group in ("buses", "gens", "loads", "branches", "branches3", "shunts", "controllers", "switches")
                  for row in model.get(group, []) if row.get("id") not in (identifier, bus["id"])]
    point = point_from_solver(solver, state, metadata={"profile": "machine_admittance",
        "coverage": [{"object_id": identifier, "relative_path": gen["path"], "status": "included"},
                     {"object_id": bus["id"], "relative_path": bus.get("path"), "status": "included"}, *exclusions],
        "exclusions": exclusions, "partial_model": True, "source_inputs": inputs,
        "equilibrium_initialization": {"machine_residual": initial.residual},
        "constant_inputs": {"ve_pu_machine": machine.ve, "pt_pu_machine": machine.pt},
        "native_assumptions": machine.discrete_state,
        "admittance_load": {"p_consumed_mw": power.real, "q_consumed_mvar": power.imag,
                            "origin": "pm_created", "object_id": None, "relative_path": None,
                            "definition": 'conj(S/100 MVA)/abs(V)^2'},
        "angular_frame": 'Absolute rotor, constant fref=1 p.u., structural rotation preserved'})
    return RMSDAEAdapter(solver), point
