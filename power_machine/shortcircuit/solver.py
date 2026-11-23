"Explicit data, numerical methods and traceable results for shortcircuit studies."
from __future__ import annotations

from dataclasses import asdict
import json
import math

import numpy as np

from . import ansi_ieee, iec60909
from .data import DATA_SCHEMA, impedance_ohm, scalar
from .schema import FaultSpec, SCError, ShortCircuitData, ShortCircuitOptions, ShortCircuitResult, diagnostic
from .sequences import fault_currents, phase_currents, prepare_network, solve_column, stamp_island

QUANTITY_KEYS = ("Ik_initial_sym_ka", "ip_peak_ka", "Ib_sym_ka", "Ith_ka",
                 "phase_currents_ka", "sequence_currents_ka", "z_th_ohm")


def _cancelled(cancel):
    return cancel is not None and (cancel.is_set() if hasattr(cancel, "is_set") else bool(cancel()))


def _validate(fault, options, data, *, validate_data=True):
    if validate_data:
        json.dumps(asdict(data), allow_nan=False)
        json.dumps(asdict(options), allow_nan=False)
    if data.schema != DATA_SCHEMA:
        raise SCError("invalid_sc_schema", "Incompatible short-circuit data schema")
    if fault.kind not in ("3ph", "ll", "lg", "llg"):
        raise SCError("invalid_fault_kind", "Fault kind must be 3ph, ll, lg or llg")
    if not math.isfinite(fault.r_fault_ohm) or not math.isfinite(fault.x_fault_ohm) or fault.r_fault_ohm < 0:
        raise SCError("invalid_fault_impedance", "Zf must be finite and Rf nonnegative")
    if options.method not in ("iec60909", "ansi_ieee"):
        raise SCError("invalid_sc_method", "Unknown short-circuit method")
    if options.case not in ("maximum", "minimum"):
        raise SCError("invalid_case", "case must be maximum or minimum")
    if options.frequency_hz <= 0 or not math.isfinite(options.frequency_hz):
        raise SCError("invalid_frequency", "Frequency in Hz must be finite and positive")
    for t in (options.min_break_time_s, options.fault_duration_s):
        if t is not None and (not math.isfinite(t) or t <= 0):
            raise SCError("invalid_sc_time", "Short-circuit times in seconds must be finite and positive")
    if options.method == "iec60909" and options.profile not in ("iec_radial_v1", "iec_pf2016_c1_v1"):
        raise SCError("unsupported_iec_profile", "IEC profile is not implemented", status="unsupported")
    if options.profile == "iec_pf2016_c1_v1" and (fault.kind != "3ph" or options.case != "maximum"):
        raise SCError("unsupported_iec_profile", "PF2016 C(1) is restricted to maximum three-phase faults", status="unsupported")
    if options.method == "ansi_ieee":
        if options.profile not in ("analytical_rl_v1", "ansi_duty"):
            raise SCError("unsupported_ansi_profile", "ANSI profile is not implemented", status="unsupported")
        if options.prefault_voltage_pu is None:
            raise SCError("missing_sc_data", "ANSI/RL requires an explicit prefault voltage", magnitude="prefault_voltage_pu")
        if not math.isfinite(options.prefault_voltage_pu) or options.prefault_voltage_pu <= 0:
            raise SCError("invalid_prefault_voltage", "Invalid prefault voltage in pu")


def run_shortcircuit(model: dict, fault: FaultSpec, options: ShortCircuitOptions,
                     data: ShortCircuitData) -> ShortCircuitResult:
    """Run one explicit fault with data coverage, finite-value and residual checks."""
    return _run_shortcircuit(model, fault, options, data)


def _run_shortcircuit(model, fault, options, data, *, cancel=None, progress=None, workspace=None):
    fault_info = asdict(fault)
    quantities = dict.fromkeys(QUANTITY_KEYS)
    diagnostics, contributions = [], []
    try:
        _validate(fault, options, data, validate_data=workspace is None or not workspace.validated)
        if workspace is not None:
            workspace.validated = True
        if _cancelled(cancel):
            return ShortCircuitResult("cancelled", fault_info, quantities, (), ())
        if workspace is None:
            from .compiled import cached_workspace
            workspace = cached_workspace(model,data,options,fault.kind)
        network = workspace.network(fault.bus_id)
        topology, buses = network["topology"], network["buses"]
        diagnostics.extend(network["diagnostics"])
        if workspace.backend_unavailable:
            diagnostics.append(diagnostic("sparse_backend_unavailable","SciPy is unavailable. Using the dense NumPy backend."))
        quantities["coverage"] = network["coverage"]
        fault_bus = buses.get(fault.bus_id)
        if fault_bus is None or fault.bus_id not in topology["bus_to_node"]:
            raise SCError("invalid_fault_bus", "Fault bus does not exist or is out of service", fault_bus)
        fault_info.update(object_id=fault_bus["id"], relative_path=fault_bus.get("relative_path", fault_bus.get("path")))
        errors = [d for d in diagnostics if d["level"] == "error"]
        if errors:
            status = "unsupported" if all(d["code"].startswith("unsupported") for d in errors) else "invalid_input"
            return ShortCircuitResult(status, fault_info, quantities, (), tuple(diagnostics))
        nf = topology["bus_to_node"][fault.bus_id]
        island = topology["islands"][topology["node_to_island"][nf]]
        nodes = island["nodes"]
        source_list = [s for s in network["sources"] if not s.get("excluded") and
                       topology["bus_to_node"][s["bus_id"]] in nodes]
        if not source_list:
            # Se demuestra desconexión con topología completa y fuentes auditadas.
            quantities.update(Ik_initial_sym_ka=0.0, ip_peak_ka=0.0, Ib_sym_ka=0.0, Ith_ka=0.0,
                              phase_currents_ka={k: 0j for k in "abc"},
                              sequence_currents_ka={k: 0j for k in (0, 1, 2)}, energization="deenergized")
            diagnostics.append(diagnostic("deenergized", "The fault island has no connected source", fault_bus, level="info"))
            return ShortCircuitResult("completed", fault_info, quantities, (), tuple(diagnostics))
        kv, sb = buses[fault.bus_id]["un_kv"], network["s_base_mva"]
        zbase_ohm, ibase_ka = kv*kv/sb, sb/(math.sqrt(3)*kv)
        quantities["bases"] = {"s_base_mva": sb, "u_base_kv": kv,
                               "z_base_ohm": zbase_ohm, "i_base_ka": ibase_ka}
        columns, stamps, zth, checks = {}, {}, {}, {}
        for seq in network["sequences"]:
            if _cancelled(cancel):
                return ShortCircuitResult("cancelled", fault_info, dict.fromkeys(QUANTITY_KEYS), (), tuple(diagnostics))
            try:
                y, stamps[seq], factorization = workspace.matrix(fault.bus_id,network,nodes,seq)
            except np.linalg.LinAlgError as exc:
                raise SCError("singular_sequence_matrix",str(exc),status="numerical_failure") from exc
            columns[seq], checks[seq] = solve_column(y, nodes.index(nf), diagnostics,
                factorization=factorization, diagnostic_condition=getattr(options,"diagnostic_condition",False))
            zth[seq] = columns[seq][nodes.index(nf)] * zbase_ohm
            if progress:
                progress({"stage": "sequence", "sequence": seq, "n_nodes": len(nodes)})
        c = (iec60909.voltage_factor(kv, options.case, options.lv_tolerance_pct)
             if options.method == "iec60909" else options.prefault_voltage_pu)
        seq_i = fault_currents(c*kv/math.sqrt(3), zth, fault.kind,
                               complex(fault.r_fault_ohm, fault.x_fault_ohm))
        phases = phase_currents(seq_i)
        if not all(np.isfinite(v) for v in phases.values()):
            raise SCError("nonfinite_fault_current", "Nonfinite fault current", status="numerical_failure")
        ik = max(abs(v) for v in phases.values())
        quantities.update(Ik_initial_sym_ka=float(ik), phase_currents_ka=phases,
                          sequence_currents_ka=seq_i, z_th_ohm=zth, numerical_checks=checks,
                          energization="energized", voltage_factor=c)
        # Caídas inducidas por la falla, tensión = prefalla - caída.
        drops = {seq: columns[seq] * seq_i[seq]/ibase_ka for seq in columns}
        b2n = topology["bus_to_node"]
        for source in source_list:
            bus_id, item = source["bus_id"], source["item"]
            pos = nodes.index(b2n[bus_id])
            uk = buses[bus_id]["un_kv"]
            local_base = sb/(math.sqrt(3)*uk)
            currents = {s: drops[s][pos] / (source["z_ohm"][s]/(uk*uk/sb))*local_base*source["unit_count"] for s in drops}
            contributions.append({"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                                  "kind": "source", "source_kind": source["kind"], "bus_id": bus_id,
                                  "direction": "source_to_network", "unit_count": source["unit_count"],
                                  "sequence_currents_per_unit_ka": {s: v/source["unit_count"] for s, v in currents.items()},
                                  "sequence_currents_ka": currents,
                                  "phase_currents_ka": phase_currents(currents), "correction_factor": source["factor"]})
        branch_currents = {}
        for seq, entries in stamps.items():
            for branch, ports, mat in entries:
                item = branch["item"]
                # Corriente saliendo de barra = -Y_terminal * caída de tensión.
                terminal_i_pu = -mat @ drops[seq][ports]
                row = branch_currents.setdefault(item["id"], {
                    "object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")), "kind": "branch",
                    "correction_factor": branch["factor"], "unit_count": branch["unit_count"], "terminals": {}})
                bus_ids = branch.get("bus_ids") or [item["bus_from"], item["bus_to"]]
                labels = ("hv", "mv", "lv") if len(bus_ids) == 3 else ("from", "to")
                for port, bus_id, value in zip(labels, bus_ids, terminal_i_pu):
                    local_base = sb/(math.sqrt(3)*buses[bus_id]["un_kv"])
                    terminal = row["terminals"].setdefault(port, {"bus_id": bus_id, "direction": "bus_to_element", "sequence_currents_ka": {}})
                    terminal["sequence_currents_ka"][seq] = value*local_base
        for row in branch_currents.values():
            for terminal in row["terminals"].values():
                terminal["phase_currents_ka"] = phase_currents(terminal["sequence_currents_ka"])
            contributions.append(row)
        contributions.extend(_ideal_contributions(network, nodes, contributions, fault, seq_i, diagnostics))
        # Pico radial solo para una fuente y árbol serie sin derivaciones.
        island_branches = [b for b in network["branches"] if b2n[b["item"].get("bus_from", b["item"].get("bus_hv"))] in nodes]
        radial = len(source_list) == 1 and len(island_branches) == len(nodes)-1
        radial = radial and not network.get("shunts") and all(b["item"]["class"] in ("ElmLne", "ElmTr2", "ElmSind") for b in island_branches)
        if options.method == "iec60909":
            if options.profile == "iec_pf2016_c1_v1":
                from .meshed import meshed_quantities
                meshed_quantities(quantities, diagnostics, contributions, network, nodes, options, fault)
            else:
                _iec_quantities(quantities, diagnostics, contributions, source_list, radial, zth[1], options, fault, ik)
        else:
            quantities.update(momentary_duty=None, close_latch_duty=None, interrupting_duty=None,
                              normative_status="pending")
            diagnostics.append(diagnostic("pending_ansi_duty_evidence", "ANSI duty requires an edition, class, cycles, period-specific networks and local tables"))
            diagnostics.append(diagnostic("unsupported_ansi_quantities", "ip, Ib and Ith are not covered by the analytical RL profile and remain null"))
            if fault.kind == "3ph" and radial and source_list[0]["kind"] == "external":
                z = zth[1] + complex(fault.r_fault_ohm, fault.x_fault_ohm)
                phi, tau, omega = ansi_ieee.rl_parameters(z.real, z.imag, options.frequency_hz)
                quantities["rl"] = {"phi_rad": phi, "tau_s": tau, "omega_rad_s": omega,
                    **ansi_ieee.rl_window_rms_ka(ik, z.real, z.imag, options.frequency_hz,
                        options.inception_angle_rad, options.rl_window_start_s,
                        options.rl_window_end_s, options.rl_steps)}
            else:
                diagnostics.append(diagnostic("unsupported_rl_profile", "RL requires a three-phase fault and one radial external source"))
        if _cancelled(cancel):
            return ShortCircuitResult("cancelled", fault_info, dict.fromkeys(QUANTITY_KEYS), (), tuple(diagnostics))
        # Un null obligatorio no cubierto hace explícita la entrega parcial.
        partial = any(quantities[k] is None for k in ("ip_peak_ka", "Ib_sym_ka", "Ith_ka"))
        if options.method == "ansi_ieee":
            partial = options.profile == "ansi_duty" or "rl" not in quantities
        return ShortCircuitResult("partial" if partial else "completed", fault_info, quantities,
                                  tuple(contributions), tuple(diagnostics))
    except SCError as exc:
        diagnostics.append(exc.diagnostic)
        # No publica corrientes globales si falla un cálculo o su perfil.
        empty = dict.fromkeys(QUANTITY_KEYS)
        if "coverage" in quantities:
            empty["coverage"] = quantities["coverage"]
        return ShortCircuitResult(exc.status, fault_info, empty, (), tuple(diagnostics))
    except (ArithmeticError, ValueError, KeyError, TypeError, np.linalg.LinAlgError) as exc:
        diagnostics.append(diagnostic("invalid_sc_input", str(exc), level="error"))
        return ShortCircuitResult("invalid_input", fault_info, dict.fromkeys(QUANTITY_KEYS), (), tuple(diagnostics))


def _iec_quantities(qty, diagnostics, contributions, sources, radial, zth, options, fault, ik):
    source = sources[0] if len(sources) == 1 else None
    kappa = None
    if radial and fault.kind == "3ph":
        try:
            z = zth + complex(fault.r_fault_ohm, fault.x_fault_ohm)
            r = z.real
            if source["kind"] != "external":
                if len(qty["numerical_checks"]) and len(qty.get("coverage", ())) > 1:
                    raise SCError("unsupported_peak_profile", "Rotating-source peak with branches requires equivalent R referred to the fault", status="unsupported")
                rk = impedance_ohm(source["record"], "r_kappa_ohm", 1, source["item"], allow_zero=True)
                if rk.imag:
                    raise SCError("invalid_sc_value", "r_kappa_ohm must be a real resistance", source["item"])
                r += (source["factor"]*rk.real - source["z_ohm"][1].real)/source["unit_count"]
            kappa = iec60909.peak_factor(r, z.imag)
            qty.update(kappa=kappa, ip_peak_ka=math.sqrt(2)*kappa*ik)
        except SCError as exc:
            d = exc.diagnostic
            d["level"] = "warning"
            diagnostics.append(d)
    else:
        diagnostics.append(diagnostic("unsupported_peak_profile", "Radial peak does not cover meshes, multiple sources or asymmetric faults"))
    if fault.kind == "3ph" and source is not None:
        if source["kind"] == "external":
            qty["Ib_sym_ka"] = ik
        elif options.min_break_time_s is None:
            diagnostics.append(diagnostic("missing_break_time", "Rotating-source Ib requires a minimum break time"))
        else:
            try:
                source_i = next(c for c in contributions if c["kind"] == "source")
                ik_source = max(abs(v) for v in source_i["phase_currents_ka"].values())/source["unit_count"]
                ir = scalar(source["record"], "ir_ka", source["item"], unit="kA", positive=True)
                mu = iec60909.synchronous_decay(ik_source/ir, options.min_break_time_s, options.normative_evidence)
                q = 1.0
                if source["kind"] == "motor":
                    p = scalar(source["record"], "power_mechanical_mw", source["item"], unit="MW", positive=True)
                    poles = scalar(source["record"], "pole_pairs", source["item"], unit="1", positive=True)
                    q = iec60909.motor_decay(p, poles, options.min_break_time_s, options.normative_evidence)
                qty.update(Ib_sym_ka=mu*q*ik, mu=mu, motor_q=q)
                source_i["Ib_phase_currents_ka"] = {k: mu*q*v for k, v in source_i["phase_currents_ka"].items()}
            except SCError as exc:
                d = exc.diagnostic
                d["level"] = "warning"
                diagnostics.append(d)
    else:
        diagnostics.append(diagnostic("unsupported_breaking_profile", "Global Ib for multiple sources or asymmetric faults is not covered"))
    if source and source["kind"] == "external" and kappa is not None and options.fault_duration_s is not None:
        m = iec60909.thermal_factor(kappa, options.frequency_hz, options.fault_duration_s)
        qty.update(thermal_m=m, thermal_n=1.0, Ith_ka=ik*math.sqrt(m+1))
    else:
        diagnostics.append(diagnostic("unsupported_thermal_profile", "Ith requires T and constant AC from a radial external source. Machine n is not assumed"))


def _ideal_contributions(network, nodes, contributions, fault, seq_i, diagnostics):
    """Recover ideal currents by KCL at original buses. Meshed flows are nonunique."""
    topology = network["topology"]
    edges = [e for e in topology["ideal_edges"] if topology["bus_to_node"][e["bus_from"]] in nodes]
    if not edges:
        return []
    lookup = {i["id"]: i for group in ("branches", "switches") for i in network.get("model", {}).get(group, [])}
    result = []
    for node in nodes:
        local_edges = [e for e in edges if topology["bus_to_node"][e["bus_from"]] == node]
        if not local_edges:
            continue
        buses = sorted({e[key] for e in local_edges for key in ("bus_from", "bus_to")})
        index = {bus_id: i for i, bus_id in enumerate(buses)}
        incidence = np.zeros((len(buses), len(local_edges)))
        for j, edge in enumerate(local_edges):
            incidence[index[edge["bus_from"]], j] += 1
            incidence[index[edge["bus_to"]], j] -= 1
        edge_values = {e["id"]: {s: 0j for s in network["sequences"]} for e in local_edges}
        for seq in network["sequences"]:
            injection = np.zeros(len(buses), complex)
            for row in contributions:
                if row["kind"] == "source" and row["bus_id"] in index:
                    injection[index[row["bus_id"]]] += row["sequence_currents_ka"].get(seq, 0j)
                elif row["kind"] == "branch":
                    for terminal in row["terminals"].values():
                        if terminal["bus_id"] in index:
                            injection[index[terminal["bus_id"]]] -= terminal["sequence_currents_ka"].get(seq, 0j)
            if fault.bus_id in index:
                injection[index[fault.bus_id]] -= seq_i[seq]
            currents, _, rank, _ = np.linalg.lstsq(incidence, injection, rcond=None)
            residual = float(np.linalg.norm(incidence@currents-injection)/max(np.linalg.norm(injection), 1e-30))
            if residual > 1e-10:
                raise SCError("ideal_current_kcl_failure", f"Ideal KCL residual {residual:.6g}", status="numerical_failure")
            if len(local_edges) > rank and seq == 1:
                diagnostics.append(diagnostic("nonunique_ideal_current", "Ideal mesh. Minimum-norm currents are not unique", level="warning"))
            for edge, value in zip(local_edges, currents):
                edge_values[edge["id"]][seq] = value
        for edge in local_edges:
            values = edge_values[edge["id"]]
            row = {"object_id": edge["id"], "relative_path": lookup.get(edge["id"], {}).get("path"),
                   "kind": "branch", "ideal": True, "terminals": {}}
            for side, key, sign in (("from", "bus_from", 1), ("to", "bus_to", -1)):
                currents = {s: sign*v for s, v in values.items()}
                row["terminals"][side] = {"bus_id": edge[key], "direction": "bus_to_element",
                    "sequence_currents_ka": currents, "phase_currents_ka": phase_currents(currents)}
            result.append(row)
    return result
