"AC power flow using polar Newton-Raphson and reusable network preparation."

from __future__ import annotations

import copy
import math
from collections import defaultdict

import numpy as np

from . import elements
from .overrides import apply_overrides
from .results import branch_result, reconstruct_ideal_flows
from .topology import build_topology


def _scenario(model: dict, scenario: dict | None) -> dict:
    """Apply outserv, load_scale, v_set_pu and gen_p_mw mappings by element ID."""
    result = copy.deepcopy(model)
    if not scenario:
        return result
    index = {item["id"]: item for group in ("buses", "switches", "branches", "branches3",
                                           "shunts", "loads", "gens") for item in result.get(group, [])}
    for field, changes in scenario.items():
        if field not in ("outserv", "load_scale", "v_set_pu", "gen_p_mw", "closed"):
            raise ValueError('[invalid_input] ' + f"Unsupported scenario change: {field}")
        for item_id, value in changes.items():
            if item_id not in index:
                raise ValueError('[invalid_input] ' + f"Unknown scenario element: {item_id}")
            item = index[item_id]
            if field == "outserv":
                item["outserv"] = int(value)
            elif field == "closed":
                item["closed"] = bool(value)
            elif field == "load_scale":
                if item["class"] != "ElmLod":
                    raise ValueError('[invalid_input] ' + f"{item_id} is not ElmLod")
                item.setdefault("params", {})["scale0"] = float(value)
                item["scale"] = float(value)
            elif field == "v_set_pu":
                if item["class"] not in ("ElmSym", "ElmGenstat", "ElmPvsys"):
                    raise ValueError('[invalid_input] ' + f"{item_id} is not a generator")
                item["v_set_pu"] = float(value)
                item.setdefault("params", {})["usetp"] = float(value)
            else:
                if item["class"] not in ("ElmSym", "ElmGenstat", "ElmPvsys"):
                    raise ValueError('[invalid_input] ' + f"{item_id} is not a generator")
                scale = float(item.get("params", {}).get("scale0", 1))
                n = float(item.get("params", {}).get("ngnum", 1))
                divisor = 1000 if item["class"] == "ElmPvsys" else 1
                item.setdefault("params", {})["pgini"] = float(value) * divisor / (n * scale)
                item["p_mw"] = float(value)
    return result


def _stamp(item, buses, s_base_mva, *, provisional_tr3=False):
    if item["class"] == "ElmTr3":
        ids = [item[key] for key in ("bus_hv", "bus_mv", "bus_lv")]
        un = [buses[bus_id]["un_kv"] for bus_id in ids]
        matrix = elements.y_tr3(item, *un, s_base_mva,
                                allow_missing_losses=provisional_tr3)
    else:
        ids = [item["bus_from"], item["bus_to"]]
        un = [buses[bus_id]["un_kv"] for bus_id in ids]
        if item["class"] == "ElmLne":
            p = item.get("params", {})
            if p.get("R1") == p.get("X1") == 0:
                raise elements.MissingParameter("ElmTow/TypTow electrical parameters (R1=X1=0)")
            matrix = elements.y_line(item, *un, s_base_mva)
        elif item["class"] == "ElmTr2":
            matrix = elements.y_tr2(item, *un, s_base_mva)
        elif item["class"] == "ElmZpu":
            matrix = elements.y_zpu(item, *un, s_base_mva)
        elif item["class"] in ("ElmScap", "ElmSind"):
            matrix = elements.y_series(item, *un, s_base_mva)
        else:
            raise ValueError('[invalid_input] ' + f"Unsupported branch: {item['class']}")
    return ids, un, matrix


def _power(ybus, vm, angle):
    v = vm * np.exp(1j * angle)
    return v * np.conj(ybus @ v)


def _jacobian_condition(jac):
    """Dimensionless condition estimate, null when it cannot be represented in JSON."""
    if hasattr(jac, "toarray"):
        jac = jac.toarray()
    if not jac.size:
        return 1.0
    try:
        value = float(np.linalg.cond(jac))
    except np.linalg.LinAlgError:
        return None
    return value if math.isfinite(value) else None


def _jacobian(ybus, vm, angle, power, angle_idx, pq_idx):
    g, b = ybus.real, ybus.imag
    delta = angle[:, None] - angle[None, :]
    c, s = np.cos(delta), np.sin(delta)
    vv = vm[:, None] * vm[None, :]
    h = vv * (g * s - b * c)
    n = vm[:, None] * (g * c + b * s)
    m = -vv * (g * c + b * s)
    ell = vm[:, None] * (g * s - b * c)
    idx = np.arange(len(vm))
    h[idx, idx] = -power.imag - b[idx, idx] * vm ** 2
    n[idx, idx] = power.real / vm + g[idx, idx] * vm
    m[idx, idx] = power.real - g[idx, idx] * vm ** 2
    ell[idx, idx] = power.imag / vm - b[idx, idx] * vm
    return np.block([[h[np.ix_(angle_idx, angle_idx)], n[np.ix_(angle_idx, pq_idx)]],
                     [m[np.ix_(pq_idx, angle_idx)], ell[np.ix_(pq_idx, pq_idx)]]])


def _solve(ybus, p_spec, q_spec, ref, pv, v_set, tolerance, max_iter,
           initial_vm=None, initial_angle=None, *, diagnostic_condition=False,
           backend="dense", pattern_cache=None):
    from .compiled import linear_solve, SparseJacobianPattern
    count = len(p_spec)
    angle_idx = np.array([i for i in range(count) if i != ref], dtype=int)
    pq_idx = np.array([i for i in angle_idx if i not in pv], dtype=int)
    if backend == 'sparse':
        key = (tuple(angle_idx), tuple(pq_idx))
        if pattern_cache is None: pattern_cache = {}
        if key not in pattern_cache: pattern_cache[key] = SparseJacobianPattern(ybus, angle_idx, pq_idx)
        jacobian = lambda vm, angle, power: pattern_cache[key].matrix(ybus, vm, angle, power)
    else:
        jacobian = lambda vm, angle, power: _jacobian(ybus, vm, angle, power, angle_idx, pq_idx)
    vm = np.ones(count) if initial_vm is None else np.asarray(initial_vm, dtype=float).copy()
    angle = np.zeros(count) if initial_angle is None else np.asarray(initial_angle, dtype=float).copy()
    for i, value in v_set.items():
        vm[i] = value
    if initial_angle is None and count > 2 and angle_idx.size:
        # La fase de transformadores y ElmZpu puede alejar la solución del
        # arranque plano. Un paso linealizado de P entrega un ángulo inicial.
        flat_power = _power(ybus, vm, angle)
        flat_jac = jacobian(vm, angle, flat_power)
        try:
            angle_step = linear_solve(flat_jac[:len(angle_idx), :len(angle_idx)],
                                         p_spec[angle_idx] - flat_power.real[angle_idx], backend)
            if np.all(np.isfinite(angle_step)):
                angle[angle_idx] = angle_step
        except np.linalg.LinAlgError:
            pass

    def mismatch(vm_current, angle_current):
        power = _power(ybus, vm_current, angle_current)
        vector = np.r_[p_spec[angle_idx] - power.real[angle_idx],
                       q_spec[pq_idx] - power.imag[pq_idx]]
        return power, vector

    stalled_steps = 0
    for iteration in range(max_iter + 1):
        power, vector = mismatch(vm, angle)
        error = float(np.max(np.abs(vector))) if vector.size else 0.0
        worst_position = int(np.argmax(np.abs(vector))) if vector.size else None
        worst_node = (int(angle_idx[worst_position]) if worst_position is not None
                      and worst_position < len(angle_idx) else
                      int(pq_idx[worst_position - len(angle_idx)]) if worst_position is not None
                      else None)
        if error <= tolerance:
            condition = _jacobian_condition(jacobian(vm, angle, power)) if diagnostic_condition else None
            return vm * np.exp(1j * angle), iteration, error, condition, True, "converged", worst_node
        if iteration == max_iter:
            reason = "iteration_limit"
            break
        jac = jacobian(vm, angle, power)
        try:
            step = linear_solve(jac, vector, backend)
        except np.linalg.LinAlgError:
            reason = "singular_jacobian"
            break
        if not np.all(np.isfinite(step)):
            reason = "nonfinite_newton_step"
            break
        alpha = 1.0
        accepted = False
        for _ in range(14):
            trial_vm = vm.copy()
            trial_angle = angle.copy()
            trial_angle[angle_idx] += alpha * step[:len(angle_idx)]
            trial_vm[pq_idx] += alpha * step[len(angle_idx):]
            if np.all(trial_vm > 0):
                _, trial_vector = mismatch(trial_vm, trial_angle)
                trial_error = float(np.max(np.abs(trial_vector))) if trial_vector.size else 0
                if trial_error < error:
                    vm, angle = trial_vm, trial_angle
                    accepted = True
                    stalled_steps = stalled_steps + 1 if trial_error > error * (1 - 1e-5) else 0
                    break
            alpha /= 2
        if not accepted:
            reason = "line_search_failed"
            break
        if stalled_steps >= 4:
            reason = "stalled_newton"
            break
    power, vector = mismatch(vm, angle)
    error = float(np.max(np.abs(vector))) if vector.size else 0.0
    worst_position = int(np.argmax(np.abs(vector))) if vector.size else None
    worst_node = (int(angle_idx[worst_position]) if worst_position is not None
                  and worst_position < len(angle_idx) else
                  int(pq_idx[worst_position - len(angle_idx)]) if worst_position is not None
                  else None)
    jac = jacobian(vm, angle, power)
    condition = _jacobian_condition(jac)
    return vm * np.exp(1j * angle), iteration, error, condition, False, reason, worst_node


def _prepare_state(model, options, stamp_cache):
    missing = options.get("missing", "error")
    warnings = list(model.get("warnings", []))
    for branch in model.get("branches", []):
        if branch.get("class") != "ElmTr2" or branch.get("outserv") == 1:
            continue
        p, tp = branch.get("params", {}), branch.get("type_params", {})
        position, lower, upper = p.get("nntap"), tp.get("ntpmn"), tp.get("ntpmx")
        if position is not None and lower is not None and upper is not None:
            effective = min(max(float(position), float(lower)), float(upper))
            if effective != float(position):
                warnings.append(f"[tap_clamped] Tap clamped {branch['id']}: {position} -> {effective} [ntpmn, ntpmx]")
    s_base = float(model["meta"]["s_base_mva"])
    buses_by_id = {item["id"]: item for item in model["buses"]}
    items = {item["id"]: item for group in ("branches", "branches3", "shunts", "loads", "gens")
             for item in model.get(group, [])}
    provisional = build_topology(model)
    ideal_ids = {edge["id"] for edge in provisional["ideal_edges"]}
    for edge in provisional["ideal_edges"]:
        if edge["class"] == "ElmLne":
            warnings.append(f"[candidate_ideal_line] Candidate ideal line {edge['id']}: R1=X1=B1=0, no electrical type. Confirm against REF")
        elif edge["class"] == "ElmZpu":
            warnings.append(f"[ideal_impedance] Ideal ElmZpu {edge['id']}: |z_ij| and |z_ji| < 1e-6 p.u., neutral tap, zero shunts. KCL flow")
    stamps = {}
    omitted = []
    for group in ("branches", "branches3"):
        candidates = list(provisional["active"][group])
        if group == "branches":
            candidates += list(provisional["dangling_branches"])
        for item_id in candidates:
            if item_id in ideal_ids:
                continue
            item = items[item_id]
            try:
                key = (item_id, bool(options.get("provisional_tr3")))
                saved = stamp_cache.get(key)
                if saved is not None and saved[0] is item:
                    stamps[item_id] = saved[1]
                else:
                    stamps[item_id] = _stamp(item, buses_by_id, s_base, provisional_tr3=options.get("provisional_tr3", False))
                    stamp_cache[key] = (item, stamps[item_id])
                if item["class"] == "ElmTr3":
                    for assumption in elements.tr3_assumptions(
                            item, allow_missing_losses=options.get("provisional_tr3", False)):
                        warnings.append(f"[provisional_three_winding] 3W provisional {item_id}: {assumption}")
            except elements.MissingParameter as exc:
                if missing == "error":
                    raise elements.MissingParameter(f"{item_id} ({item['name']}): {exc.attribute}") from exc
                omitted.append(item_id)
                warnings.append(f"[missing_branch_parameter] Branch opened because of missing data: {item_id} ({item['name']}): {exc.attribute}")
    topology = build_topology(model, omitted) if omitted else provisional
    dangling = topology["dangling_branches"]
    if omitted:
        warnings.append(f"[diagnostic_open_mode] Diagnostic open mode: {len(omitted)} excluded branches. Not suitable for REF validation")
    if any(item.get("type_class") == "TypLod" for item in model.get("loads", [])):
        warnings.append("[constant_pq_load] TypLod voltage exponents were not exported. Constant P/Q is used")
    return {"warnings":warnings,"s_base":s_base,"buses":buses_by_id,"items":items,
            "stamps":stamps,"omitted":omitted,"topology":topology}


def run_loadflow(model: dict, scenario: dict | None = None,
                 options: dict | None = None) -> dict:
    "Solve AC power flow per referenced island using a reusable network snapshot.\n\nOptions include backend=\"auto\"|\"dense\"|\"sparse\", diagnostic_condition=False,\nmissing=\"error\"|\"open\", q_limits, base_result and warm_start. Missing data\nremain explicit. The input model and scenario are not modified."
    options = options or {}
    base_voltages = {}
    if scenario and options.get("warm_start", True):
        base_result = options.get("base_result")
        if base_result is None:
            base_options = {**options, "warm_start": False}
            base_options.pop("base_result", None)
            base_result = run_loadflow(model, options=base_options)
        base_voltages = {
            row["id"]: row["u_pu"] * np.exp(1j * math.radians(row["angle_deg"]))
            for row in base_result["buses"] if row["u_pu"] is not None
        }
    missing = options.get("missing", "error")
    if missing not in ("error", "open"):
        raise ValueError('[invalid_input] ' + "options['missing'] must be 'error' or 'open'")
    from .compiled import cached_network, clone_topology
    prepared = cached_network(model)
    override_warnings = []
    if options.get("overrides"):
        prepared, override_warnings = prepared.overridden(options["overrides"])
    model = prepared.scenario(scenario)
    state = prepared.state(model, scenario, options)
    override_warnings = [message.replace("; fuente=", "; source=").replace("; fecha=", "; date=") for message in override_warnings]
    warnings = list(state['warnings']) + list(override_warnings)
    s_base, buses_by_id = state['s_base'], state['buses']
    items = {item['id']: item for group in ('branches','branches3','shunts','loads','gens') for item in model.get(group, [])}
    topology = clone_topology(state['topology'])
    stamps, omitted = state['stamps'], list(state['omitted'])
    dangling = topology['dangling_branches']
    output = {"converged": True, "iterations": 0, "max_mismatch_mva": 0.0,
              "jacobian_condition": None, "buses": [], "branches": [], "gens": [],
              "loads": [], "islands": topology["islands"], "totals": {},
              "warnings": warnings, "omitted_branches": omitted,
              "ideal_lines": sorted(edge["id"] for edge in topology["ideal_edges"]
                                    if edge["class"] == "ElmLne"),
              "ideal_zpu": sorted(edge["id"] for edge in topology["ideal_edges"]
                                   if edge["class"] == "ElmZpu"),
              "bus_to_node": topology["bus_to_node"], "backend": state["backend"],
              "diagnostics": ([{"code":"sparse_backend_unavailable","level":"warning",
                                "message":"SciPy is unavailable. Using the dense NumPy backend."}] if state["backend_unavailable"] else [])}
    voltages = {}
    generation = {}
    q_limited = set()
    conditions = []
    for island in output["islands"]:
        if island["status"] == "sin_referencia":
            output["converged"] = False
            continue
        nodes = island["nodes"]
        plan = state['plans'][island['id']]
        node_index, ybus = plan['node_index'], plan['ybus']
        p_spec = np.zeros(len(nodes))
        q_spec = np.zeros(len(nodes))
        load_at = defaultdict(complex)
        gen_at = defaultdict(list)
        for item_id in island["elements"]["loads"]:
            item = items[item_id]
            p, q = elements.load_pq_mw(item)
            i = node_index[topology["bus_to_node"][item["bus"]]]
            p_spec[i] -= p / s_base
            q_spec[i] -= q / s_base
            load_at[i] += complex(p, q)
            output["loads"].append({"id": item_id, "name": item["name"], "path": item.get("path"),
                                    "bus": item["bus"], "island": island["id"],
                                    "p_mw": p, "q_mvar": q, "status": "resuelto"})
        for item_id in island["elements"]["gens"]:
            item = items[item_id]
            p, q = elements.gen_pq_mw(item)
            i = node_index[topology["bus_to_node"][item["bus"]]]
            p_spec[i] += p / s_base
            q_spec[i] += q / s_base
            gen_at[i].append((item_id, p, q))
        ref_id = island["reference_gen"]
        ref_item = items[ref_id]
        ref = node_index[topology["bus_to_node"][ref_item["bus"]]]
        pv = set()
        v_set = {ref: float(ref_item.get("v_set_pu") or 1.0)}
        for i, generators in gen_at.items():
            if i == ref:
                continue
            controllers = [items[item_id] for item_id, _, _ in generators
                           if items[item_id].get("mode") == "constv"]
            if controllers:
                pv.add(i)
                values = [float(gen.get("v_set_pu") or 1.0) for gen in controllers]
                if max(values) - min(values) > 1e-6:
                    warnings.append(f"[conflicting_voltage_setpoints] Different PV setpoints at node {nodes[i]}: {values}. The first is used")
                v_set[i] = values[0]
        max_iter = int(options.get("max_iter", 30))
        tolerance = float(options.get("tolerance_pu", 1e-8))
        iterations = 0
        initial_vm = initial_angle = None
        members = plan['members']
        if base_voltages and all(any(bus_id in base_voltages for bus_id in members[node]) for node in nodes):
            initial_v = np.array([
                np.mean([base_voltages[bus_id] for bus_id in members[node] if bus_id in base_voltages])
                for node in nodes])
            initial_vm = np.abs(initial_v)
            initial_angle = np.unwrap(np.angle(initial_v) - np.angle(initial_v[ref]))
            warnings.append(f"Island {island['id']}: [base_warm_start] Starting from the base load flow")
        for _ in range(len(pv) + 1):
            voltage, used, error, condition, converged, reason, worst_node = _solve(
                ybus, p_spec, q_spec, ref, pv, v_set, tolerance, max_iter,
                initial_vm=initial_vm, initial_angle=initial_angle,
                diagnostic_condition=bool(options.get("diagnostic_condition", False)),
                backend=state["backend"], pattern_cache=plan["patterns"])
            iterations += used
            initial_vm, initial_angle = np.abs(voltage), np.angle(voltage)
            if not converged or not options.get("q_limits", False):
                break
            power = voltage * np.conj(ybus @ voltage) * s_base
            changed = False
            for i in sorted(pv):
                controllers = [items[item_id] for item_id, _, _ in gen_at[i]
                               if items[item_id].get("mode") == "constv"]
                try:
                    bounds = [elements.gen_q_limits_mvar(item) for item in controllers]
                except elements.MissingParameter as exc:
                    warnings.append(f"[missing_reactive_limits] Q limits unavailable at node {nodes[i]}: {exc.attribute}")
                    continue
                fixed_q = sum(q for item_id, _, q in gen_at[i] if items[item_id].get("mode") != "constv")
                needed = float(power[i].imag + load_at[i].imag - fixed_q)
                low = sum(bound[0] for bound in bounds)
                high = sum(bound[1] for bound in bounds)
                if needed < low - 1e-7 or needed > high + 1e-7:
                    bound = low if needed < low else high
                    q_spec[i] = (fixed_q + bound - load_at[i].imag) / s_base
                    pv.remove(i)
                    v_set.pop(i, None)
                    q_limited.update(item["id"] for item in controllers)
                    warnings.append(f"Node {nodes[i]}: aggregate Q limit {bound:.6g} Mvar; PV→PQ")
                    changed = True
            if not changed:
                break
        island["iterations"] = iterations
        island["max_mismatch_mva"] = error * s_base
        island["jacobian_condition"] = condition
        island["reason"] = reason
        island["worst_mismatch_node"] = nodes[worst_node] if worst_node is not None else None
        island["status"] = "resuelto" if converged else "no_convergente"
        output["iterations"] = max(output["iterations"], iterations)
        output["max_mismatch_mva"] = max(output["max_mismatch_mva"], error * s_base)
        if not converged:
            output["converged"] = False
            condition_text = f"{condition:.3e}" if condition is not None else "not_finite"
            warnings.append(f"Island {island['id']}: Newton did not converge ({reason}); mismatch={error * s_base:.6g} MVA; condition={condition_text}; node={island['worst_mismatch_node']}")
            continue
        if condition is not None:
            conditions.append(condition)
        if condition is not None and condition > 1e14:
            condition_text = f"{condition:.3e}" if condition is not None else "not_finite"
            warnings.append(f"Island {island['id']}: ill-conditioned Jacobian ({condition_text}); extreme flows may be unreliable")
        power = voltage * np.conj(ybus @ voltage) * s_base
        if ref_item.get("class")=="ElmXnet":
            from .external_grid import number
            voltage *= np.exp(1j*np.deg2rad(number(ref_item,"phiini","deg")))
        for node, i in node_index.items():
            voltages[node] = voltage[i]
        for i, gens in gen_at.items():
            slack = [row for row in gens if items[row[0]].get("ref")]
            pv_gens = [row for row in gens if items[row[0]].get("mode") == "constv" and not items[row[0]].get("ref")]
            fixed = [row for row in gens if row not in slack and row not in pv_gens]
            p_slack = float(power[i].real + load_at[i].real - sum(row[1] for row in fixed + pv_gens))
            q_variable = float(power[i].imag + load_at[i].imag - sum(row[2] for row in fixed))
            if i in q_limited or any(row[0] in q_limited for row in pv_gens):
                q_variable = q_spec[i] * s_base + load_at[i].imag - sum(row[2] for row in fixed)
            for rows, p_value, q_value in ((slack, p_slack, q_variable),
                                            (pv_gens, None, q_variable if not slack else 0.0)):
                if not rows:
                    continue
                weights = [max(float(items[row[0]].get("s_rated_mva") or 1), 1e-9) for row in rows]
                total_weight = sum(weights)
                for row, weight in zip(rows, weights):
                    generation[row[0]] = (p_value * weight / total_weight if p_value is not None else row[1],
                                          q_value * weight / total_weight)
            for item_id, p, q in fixed:
                generation[item_id] = (p, q)
    output["jacobian_condition"] = max(conditions) if conditions else None
    for bus in model["buses"]:
        node = topology["bus_to_node"].get(bus["id"])
        if node is None:
            continue
        island_id = topology["node_to_island"][node]
        v = voltages.get(node)
        output["buses"].append({"id": bus["id"], "name": bus["name"], "path": bus.get("path"),
                                "node": node, "island": island_id,
                                "status": "resuelto" if v is not None else output["islands"][island_id]["status"],
                                "u_pu": float(abs(v)) if v is not None else None,
                                "u_kv": float(abs(v) * bus["un_kv"]) if v is not None else None,
                                "angle_deg": float(math.degrees(np.angle(v))) if v is not None else None})
    for island in output["islands"]:
        passive = (island["status"] == "sin_referencia" and
                   all(not island["elements"][group] for group in ("loads", "gens", "shunts")))
        if island["status"] != "resuelto" and not passive:
            continue
        for group in ("branches", "branches3"):
            for item_id in island["elements"][group]:
                item = items[item_id]
                ids, un, matrix = stamps[item_id]
                vs = ([0j] * len(ids) if passive else
                      [voltages[topology["bus_to_node"][bus_id]] for bus_id in ids])
                row = branch_result(item, vs, matrix, un, s_base, island["id"])
                if passive:
                    row["status"] = "sin_energia_pasiva"
                output["branches"].append(row)
    for item_id, connected in dangling.items():
        item = items[item_id]
        ids, un, matrix = stamps[item_id]
        island_id = topology["node_to_island"][topology["bus_to_node"][ids[connected]]]
        if output["islands"][island_id]["status"] != "resuelto":
            continue
        other = 1 - connected
        vs = [0j, 0j]
        vs[connected] = voltages[topology["bus_to_node"][ids[connected]]]
        vs[other] = -matrix[other, connected] / matrix[other, other] * vs[connected]
        row = branch_result(item, vs, matrix, un, s_base, island_id)
        row["status"] = "extremo_abierto"
        output["branches"].append(row)
    reported_branches = {row["id"] for row in output["branches"]}
    for group in ("branches", "branches3"):
        for item_id in topology["active"][group] + [key for key in omitted if key in {e["id"] for e in model[group]}]:
            if item_id in reported_branches:
                continue
            item = items[item_id]
            first = item.get("bus_from") or item.get("bus_hv")
            island_id = topology["node_to_island"][topology["bus_to_node"][first]]
            row = {"id": item_id, "name": item["name"], "class": item["class"],
                   "path": item.get("path"), "island": island_id,
                   "status": "abierta_faltante" if item_id in omitted else output["islands"][island_id]["status"],
                   "p_loss_mw": None, "q_loss_mvar": None}
            labels = ("from", "to") if group == "branches" else ("hv", "mv", "lv")
            for label in labels:
                row[f"bus_{label}"] = item[f"bus_{label}"]
                row[f"p_{label}_mw"] = None
                row[f"q_{label}_mvar"] = None
                row[f"i_{label}_ka"] = None
            output["branches"].append(row)
    blocked_ports = set(map(tuple, topology["blocked_ports"]))
    reported_branches = {row["id"] for row in output["branches"]}
    for item in model.get("branches", []):
        if item["id"] in reported_branches or item.get("outserv", 0) == 1:
            continue
        ports = ("bushv", "buslv") if item["class"] == "ElmTr2" else ("bus1", "bus2")
        if all((item["id"], port) in blocked_ports for port in ports):
            output["branches"].append({"id": item["id"], "name": item["name"],
                "class": item["class"], "path": item.get("path"), "island": None,
                "status": "ambos_puertos_abiertos", "bus_from": item["bus_from"],
                "bus_to": item["bus_to"], "p_from_mw": 0.0, "q_from_mvar": 0.0,
                "p_to_mw": 0.0, "q_to_mvar": 0.0, "i_from_ka": 0.0,
                "i_to_ka": 0.0, "p_loss_mw": 0.0, "q_loss_mvar": 0.0})
    for item in model["gens"]:
        if item["id"] not in topology["active"]["gens"]:
            continue
        island_id = topology["node_to_island"][topology["bus_to_node"][item["bus"]]]
        pq = generation.get(item["id"])
        output["gens"].append({"id": item["id"], "name": item["name"], "path": item.get("path"),
                               "bus": item["bus"], "island": island_id,
                               "status": "resuelto" if pq else output["islands"][island_id]["status"],
                               "p_mw": pq[0] if pq else None, "q_mvar": pq[1] if pq else None,
                               "q_limit": item["id"] in q_limited})
    shunt_power = {}
    for item_id in topology["active"]["shunts"]:
        item = items[item_id]
        node = topology["bus_to_node"][item["bus"]]
        if node in voltages:
            v = voltages[node]
            admittance = elements.y_shunt(item, buses_by_id[item["bus"]]["un_kv"], s_base,
                                          f_nom_hz=model.get("meta", {}).get("f_nom_hz"))
            shunt_power[item["bus"]] = shunt_power.get(item["bus"], 0j) + v * np.conj(admittance * v) * s_base
    edge_index = {item["id"]: item for item in model.get("switches", []) + model.get("branches", [])}
    ideal_edges = [{**edge, "name": edge_index[edge["id"]].get("name", edge["id"]),
                    "path": edge_index[edge["id"]].get("path")}
                   for edge in topology["ideal_edges"]]
    passive_nodes = {node for island in output["islands"]
                     if island["status"] == "sin_referencia"
                     and all(not ids for ids in island["elements"].values())
                     for node in island["nodes"]}
    ideal_rows, ideal_warnings = reconstruct_ideal_flows(
        ideal_edges, topology["bus_to_node"], topology["node_to_island"],
        voltages, buses_by_id, output["branches"], output["gens"],
        output["loads"], shunt_power, passive_nodes)
    output["branches"].extend(ideal_rows)
    for message in ideal_warnings:
        message = message.replace("Nodo ", "Node ", 1)
        if "ideal connections in a mesh" in message:
            message = "[nonunique_ideal_mesh] " + message.replace("ideal connections in a mesh; minimum-norm flows are not unique", "ideal connections in a mesh; minimum-norm flows are not unique")
        elif "ideal-connection KCL residual" in message:
            message = "[ideal_flow_kcl_residual] " + message.replace("ideal-connection KCL residual", "ideal-connection KCL residual")
        warnings.append(message)
    for item_id in topology["ideal_disconnected"]:
        item = items[item_id]
        output["branches"].append({"id": item_id, "name": item["name"],
            "class": item["class"], "path": item.get("path"), "island": None,
            "status": "puerto_abierto_ideal", "bus_from": item["bus_from"],
            "bus_to": item["bus_to"], "p_from_mw": 0.0, "q_from_mvar": 0.0,
            "p_to_mw": 0.0, "q_to_mvar": 0.0, "i_from_ka": 0.0,
            "i_to_ka": 0.0, "p_loss_mw": 0.0, "q_loss_mvar": 0.0})
    output["totals"] = {"generation_p_mw": sum(row["p_mw"] for row in output["gens"] if row["p_mw"] is not None),
                        "generation_q_mvar": sum(row["q_mvar"] for row in output["gens"] if row["q_mvar"] is not None),
                        "load_p_mw": sum(row["p_mw"] for row in output["loads"]),
                        "load_q_mvar": sum(row["q_mvar"] for row in output["loads"]),
                        "branch_loss_p_mw": sum(row["p_loss_mw"] for row in output["branches"] if row["p_loss_mw"] is not None),
                        "branch_loss_q_mvar": sum(row["q_loss_mvar"] for row in output["branches"] if row["q_loss_mvar"] is not None)}
    status_codes = {'resuelto':'solved', 'sin_referencia':'no_reference',
                    'no_convergente':'not_converged', 'sin_energia_pasiva':'passive_deenergized',
                    'extremo_abierto':'open_terminal', 'abierta_faltante':'missing_data_open',
                    'ambos_puertos_abiertos':'both_terminals_open', 'puerto_abierto_ideal':'ideal_terminal_open',
                    'malla_no_unica':'nonunique_mesh', 'fuera_de_servicio':'out_of_service'}
    for group in ('islands', 'buses', 'branches', 'gens', 'loads'):
        for row in output[group]:
            row['status_code'] = status_codes.get(row.get('status'), row.get('status'))
    warning_codes = ('reactive_limit_reached', 'loadflow_not_converged', 'ill_conditioned_jacobian')
    for i, message in enumerate(warnings):
        if not message.startswith('['):
            code = warning_codes[0] if 'PV' in message and 'PQ' in message else warning_codes[2] if 'ill-conditioned' in message else warning_codes[1] if 'did not converge' in message else 'loadflow_warning'
            warnings[i] = '[' + code + '] ' + message
    return output
