"Explicit data, numerical methods and traceable results for shortcircuit studies."
from __future__ import annotations

import math

import numpy as np

from power_machine.topology import build_topology
from power_machine import elements
from .data import impedance_ohm, parameter, scalar, text_value
from .iec60909 import generator_factor, resistance_at_temperature, transformer_factor, voltage_factor
from .schema import SCError, diagnostic


def unit_count(item, record):
    "Return the number of identical units. Supplementary Z is defined on one equipment unit."
    p = record.get("parameters", record)
    if "parallel_units" in p:
        count = scalar(record, "parallel_units", item, unit="1", positive=True)
    else:
        field = "nlnum" if item.get("class") == "ElmLne" else "ntnum"
        count = float(item.get("n_units", item.get("params", {}).get(field, 1)))
    if not math.isfinite(count) or count <= 0 or count != int(count):
        raise SCError("invalid_parallel_units", "Unit count must be a positive integer", item)
    return int(count)


def required_sequences(kind):
    return (1,) if kind == "3ph" else ((1, 2) if kind == "ll" else (1, 2, 0))


def phase_currents(sequence):
    "Convert sequence 0/1/2 phasors to phases a/b/c in the same units."
    i0, i1, i2 = sequence.get(0, 0j), sequence.get(1, 0j), sequence.get(2, 0j)
    a = complex(-0.5, math.sqrt(3) / 2)
    return {"a": i0 + i1 + i2, "b": i0 + a*a*i1 + a*i2,
            "c": i0 + a*i1 + a*a*i2}


def fault_currents(e_phase_kv, impedances_ohm, kind, z_fault_ohm):
    "Solve 3ph, LL on b/c, LG on a, or LLG on b/c with explicit fault impedance."
    z1 = impedances_ohm[1]
    if kind == "3ph":
        return {0: 0j, 1: e_phase_kv / (z1 + z_fault_ohm), 2: 0j}
    z2 = impedances_ohm[2]
    if kind == "ll":
        i1 = e_phase_kv / (z1 + z2 + z_fault_ohm)
        return {0: 0j, 1: i1, 2: -i1}
    z0 = impedances_ohm[0]
    if kind == "lg":
        i = e_phase_kv / (z1 + z2 + z0 + 3*z_fault_ohm)
        return {0: i, 1: i, 2: i}
    zg = z0 + 3*z_fault_ohm
    i1 = e_phase_kv / (z1 + z2*zg/(z2 + zg))
    return {0: -i1*z2/(z2 + zg), 1: i1, 2: -i1*zg/(z2 + zg)}


def _source_values(record, item, bus_kv, options, seqs):
    kind = record.get("kind")
    if kind == "current_limited":
        parameter(record, "current_limit_ka", item, unit="kA")
        raise SCError("unsupported_limited_source", "The documented limited source has no implemented injection profile", item, status="unsupported")
    if kind not in ("external", "synchronous", "motor"):
        raise SCError("missing_sc_data", "Missing identified short-circuit source model", item, magnitude="source_model")
    if options.case == "minimum" and kind == "motor":
        parameter(record, "minimum_exclusion", item)
        if parameter(record, "minimum_exclusion", item)["value"] is not True:
            raise SCError("unsupported_minimum_motor", "The minimum profile requires explicit motor exclusion", item, status="unsupported")
        return {"excluded": True, "kind": kind}
    z = {}
    factor = 1.0
    if kind == "synchronous":
        xd = scalar(record, "x_d_subtransient_pu", item, unit="pu", positive=True)
        sn = scalar(record, "sn_mva", item, unit="MVA", positive=True)
        ur = scalar(record, "ur_kv", item, unit="kV", positive=True)
        rg = impedance_ohm(record, "r_generator", ur, item, allow_zero=True)
        if rg.imag != 0:
            raise SCError("invalid_sc_value", "r_generator must be a real resistance", item)
        z[1] = rg + 1j*xd*ur**2/sn
        if options.method == "iec60909":
            pf = scalar(record, "cos_phi_rated", item, unit="1")
            sign = scalar(record, "sin_phi_sign", item, unit="1")
            if not 0 <= pf <= 1 or sign not in (-1, 1):
                raise SCError("invalid_sc_value", "Invalid cos_phi or operating sign", item)
            cmax = voltage_factor(bus_kv, "maximum", options.lv_tolerance_pct)
            factor = generator_factor(bus_kv, ur, cmax, xd, sign*math.sqrt(1-pf*pf))
    else:
        z[1] = impedance_ohm(record, "z1", bus_kv, item)
    for seq in seqs:
        if seq != 1:
            z[seq] = impedance_ohm(record, f"z{seq}", bus_kv, item)
    if 0 in seqs:
        d0 = parameter(record, "z0", item)
        if not d0.get("analytical_fixture"):
            if text_value(record, "grounding", item) != "grounded":
                raise SCError("unsupported_grounding", "Source has no supported zero-sequence return", item, status="unsupported")
            parameter(record, "neutral_included", item)
        if "z_neutral" in record.get("parameters", record):
            included = parameter(record, "neutral_included", item)["value"]
            if not isinstance(included, bool):
                raise SCError("invalid_sc_value", "neutral_included must be boolean", item)
            zn = impedance_ohm(record, "z_neutral", bus_kv, item, allow_zero=True)
            if not included:
                z[0] += 3*zn
    if kind == "motor":
        ir = scalar(record, "ir_ka", item, unit="kA", positive=True)
        ratio = scalar(record, "locked_rotor_ratio", item, unit="1", positive=True)
        scalar(record, "power_mechanical_mw", item, unit="MW", positive=True)
        scalar(record, "pole_pairs", item, unit="1", positive=True)
        if not math.isclose(abs(z[1]), bus_kv/(math.sqrt(3)*ir*ratio), rel_tol=1e-6):
            raise SCError("inconsistent_motor_impedance", "Z1 does not match Ir and locked-rotor current", item)
    return {"z_ohm": {k: v*factor for k, v in z.items()}, "factor": factor,
            "kind": kind, "excluded": False}


def _branch_values(record, item, bus_kv, options, seqs):
    cls = item.get("class")
    if cls not in ("ElmLne", "ElmTr2", "ElmZpu", "ElmScap", "ElmSind"):
        raise SCError("unsupported_sc_element", "Unsupported short-circuit branch class", item, status="unsupported")
    z = {seq: impedance_ohm(record, f"z{seq}", bus_kv, item) for seq in seqs}
    tap, factor, zero_mode = 1+0j, 1.0, "series"
    if cls == "ElmZpu":
        from .data import complex_value
        tap = complex_value(parameter(record, "ratio_pu", item, unit="pu")["value"])
        if not math.isfinite(abs(tap)) or tap == 0:
            raise SCError("invalid_sc_value", "Invalid complex ElmZpu ratio", item)
        for seq in seqs:
            impedance_ohm(record, f"z{seq}_reverse", bus_kv, item)
            for side in ("from", "to"):
                parameter(record, f"y{seq}_{side}", item, unit="S")
    if cls == "ElmTr2":
        text_value(record, "vector_group", item)
        tap_d = parameter(record, "tap_pu", item, unit="pu")
        from .data import complex_value
        tap = complex_value(tap_d["value"])
        if not math.isfinite(abs(tap)) or tap == 0:
            raise SCError("invalid_sc_value", "Invalid complex short-circuit tap", item)
        if 0 in seqs:
            zero_mode = text_value(record, "zero_connection", item)
            if zero_mode not in ("series", "blocked", "ground_from", "ground_to"):
                raise SCError("unsupported_zero_connection", "Unsupported zero-sequence connection", item, status="unsupported")
            if "z_neutral" in record.get("parameters", record):
                included = parameter(record, "neutral_included", item)["value"]
                if not isinstance(included, bool):
                    raise SCError("invalid_sc_value", "neutral_included must be boolean", item)
                if not included:
                    z[0] += 3*impedance_ohm(record, "z_neutral", bus_kv, item, allow_zero=True)
        if record.get("transformer_role") != "network":
            raise SCError("unsupported_generator_unit", "An explicit network transformer is required. Generator units are not covered", item, status="unsupported")
        if options.method == "iec60909":
            if record.get("corrections_applied"):
                raise SCError("duplicate_sc_correction", "Data are already corrected. K_T is not applied twice", item)
            sn = scalar(record, "sn_mva", item, unit="MVA", positive=True)
            ur = scalar(record, "ur_kv", item, unit="kV", positive=True)
            xt = z[1].imag/(ur**2/sn)
            factor = transformer_factor(voltage_factor(bus_kv, "maximum", options.lv_tolerance_pct), xt)
    if cls == "ElmLne" and options.method == "iec60909" and options.case == "minimum":
        theta = scalar(record, "conductor_temperature_c", item, unit="degC")
        alpha = scalar(record, "alpha20_per_c", item, unit="1/degC", positive=True)
        reference = scalar(record, "resistance_reference_c", item, unit="degC")
        if reference != 20:
            raise SCError("unsupported_resistance_reference", "Resistance measured at 20 C is required", item, status="unsupported")
        z = {k: complex(resistance_at_temperature(v.real, alpha, theta), v.imag) for k, v in z.items()}
    if options.method == "iec60909" and record.get("corrections_applied") and cls != "ElmTr2":
        raise SCError("duplicate_sc_correction", "Pre-corrected short-circuit data are not supported", item)
    return {"z_ohm": {k: v*factor for k, v in z.items()}, "tap": tap,
            "factor": factor, "zero_mode": zero_mode}


def prepare_network(model, data, options, kind, fault_bus_id=None):
    "Audit active IDs before stamping and accumulate explicit missing-data diagnostics."
    topology = build_topology(model)
    buses = {b["id"]: b for b in model.get("buses", [])}
    seqs = required_sequences(kind)
    diagnostics, coverage, branches, sources = [], [], [], []
    s_base = float(model.get("meta", {}).get("s_base_mva", 100.0))
    if not math.isfinite(s_base) or s_base <= 0:
        raise SCError("invalid_system_base", "S_base_mva must be finite and positive")
    for bus_id in topology["bus_to_node"]:
        un = buses[bus_id].get("un_kv")
        if un is None or not math.isfinite(float(un)) or un <= 0:
            raise SCError("missing_sc_data", "Missing nominal line-to-line voltage in kV", buses[bus_id], magnitude="un_kv")
    fused_kv = {}
    for bus_id, node in topology["bus_to_node"].items():
        kv = buses[bus_id]["un_kv"]
        if node in fused_kv and not math.isclose(kv, fused_kv[node], rel_tol=1e-8):
            raise SCError("incompatible_ideal_voltage_bases", "Ideal connection joins different nominal voltages", buses[bus_id])
        fused_kv[node] = kv
    for group in ("branches", "branches3", "gens"):
        for item in model.get(group, []):
            if item["id"] not in topology["active"][group]:
                continue
            record = (data.sources if group == "gens" else data.elements).get(item["id"])
            try:
                if item.get("class") in ("ElmScap", "ElmSind", "ElmTr3") and record is None:
                    if 0 in seqs:
                        raise SCError("unsupported_native_zero_sequence", "Native zero-sequence winding/grounding data are required", item, status="unsupported")
                    if group == "branches3":
                        ids = [item[k] for k in ("bus_hv", "bus_mv", "bus_lv")]
                        native = {**item, "type_params": {**item.get("type_params", {}), "curm3": 0, "pfe": 0}}
                        mat = elements.y_tr3(native, *(buses[b]["un_kv"] for b in ids), s_base)
                    else:
                        ids = [item["bus_from"], item["bus_to"]]
                        mat = elements.y_series(item, *(buses[b]["un_kv"] for b in ids), s_base)
                    branches.append({"item": item, "record": {}, "unit_count": 1, "factor": 1.0,
                                     "native_matrix": mat, "bus_ids": ids})
                    coverage.append({"object_id": item["id"], "status": "supported", "sequences": list(seqs), "source": "native_ref_parameters"})
                    continue
                if item.get("class") == "ElmXnet" and record is None:
                    from power_machine.external_grid import impedance
                    if 0 in seqs:
                        raise SCError("unsupported_native_zero_sequence", "Explicit zero-sequence and grounding data required for ElmXnet", item, status="unsupported")
                    kv=buses[item['bus']]['un_kv']
                    c=voltage_factor(kv,options.case,options.lv_tolerance_pct) if options.method=='iec60909' else 1.0
                    z=impedance(item,kv,case=options.case,voltage_factor=c)
                    sources.append(dict(item=item,record={},bus_id=item['bus'],unit_count=1,kind='external',factor=1.0,z_ohm={s:z for s in seqs}))
                    coverage.append(dict(object_id=item['id'],status='supported',sequences=list(seqs),source='native_ref_parameters'))
                    continue
                if record is None:
                    missing_keys = [f"z{s}" for s in seqs]
                    if group == "gens":
                        missing_keys.insert(0, "source_model")
                    if item.get("class") == "ElmTr2":
                        missing_keys.extend(("vector_group", "tap_pu", "transformer_role", "sn_mva", "ur_kv"))
                    if 0 in seqs:
                        missing_keys.extend(("zero_connection", "neutral_included"))
                    missing = [diagnostic("missing_sc_data", f"Missing supplementary short-circuit parameter {key}", item,
                                          level="error", magnitude=key) for key in missing_keys]
                    diagnostics.extend(missing)
                    coverage.append({"object_id": item["id"], "relative_path": item.get("path"),
                                     "status": "missing", "missing_magnitudes": missing_keys, "diagnostics": missing})
                    continue
                bus_id = item.get("bus", item.get("bus_from", item.get("bus_hv")))
                if group == "gens":
                    if record.get("bus_id", bus_id) != bus_id:
                        raise SCError("source_bus_mismatch", "Supplementary bus differs from the model connection", item)
                    values = _source_values(record, item, buses[bus_id]["un_kv"], options, seqs)
                    sources.append({"item": item, "record": record, "bus_id": bus_id,
                                    "unit_count": unit_count(item, record), **values})
                else:
                    values = _branch_values(record, item, buses[bus_id]["un_kv"], options, seqs)
                    branches.append({"item": item, "record": record, "unit_count": unit_count(item, record), **values})
                coverage.append({"object_id": item["id"], "relative_path": item.get("path"),
                                 "status": "excluded" if values.get("excluded") else "supported",
                                 "sequences": list(seqs)})
            except (ValueError, TypeError) as exc:
                error = diagnostic("missing_sc_data", str(exc), item, level="error")
                diagnostics.append(error)
                coverage.append({"object_id": item["id"], "status": "missing", "diagnostics": [error]})
            except SCError as exc:
                diagnostics.append(exc.diagnostic)
                coverage.append({"object_id": item["id"], "relative_path": item.get("path"),
                                 "status": "missing" if exc.diagnostic["code"] == "missing_sc_data" else exc.status,
                                 "diagnostics": [exc.diagnostic]})
    # Una fuente de fixture puede existir solo en el suplemento, nunca por nombre.
    known_ids = {i["id"] for i in model.get("gens", [])}
    for source_id, record in data.sources.items():
        if source_id in known_ids:
            continue
        item = {"id": source_id, "path": record.get("relative_path")}
        try:
            bus_id = record.get("bus_id")
            if bus_id not in topology["bus_to_node"]:
                raise SCError("invalid_source_bus", "Supplementary source has no active bus", item)
            values = _source_values(record, item, buses[bus_id]["un_kv"], options, seqs)
            sources.append({"item": item, "record": record, "bus_id": bus_id,
                            "unit_count": unit_count(item, record), **values})
            coverage.append({"object_id": source_id, "relative_path": item["path"], "status": "supported", "sequences": list(seqs)})
        except SCError as exc:
            diagnostics.append(exc.diagnostic)
            coverage.append({"object_id": source_id, "relative_path": item["path"], "status": exc.status, "diagnostics": [exc.diagnostic]})
    for edge in topology["ideal_edges"]:
        coverage.append({"object_id": edge["id"], "relative_path": next(
            (i.get("path") for g in ("branches", "switches") for i in model.get(g, []) if i["id"] == edge["id"]), None),
                         "status": "ideal_topology", "sequences": list(seqs)})
    shunts = []
    for item in model.get("shunts", []):
        if item["id"] not in topology["active"]["shunts"]:
            continue
        try:
            if 0 in seqs:
                raise SCError("unsupported_native_zero_sequence", "Shunt grounding and zero-sequence data are required", item, status="unsupported")
            admittance = elements.y_shunt(item, buses[item["bus"]]["un_kv"], s_base, f_nom_hz=options.frequency_hz)
            shunts.append({"item": item, "admittance": admittance})
            coverage.append({"object_id": item["id"], "status": "supported", "sequences": list(seqs)})
        except (ValueError, TypeError) as exc:
            diagnostics.append(diagnostic("missing_sc_data", str(exc), item, level="error"))
        except SCError as exc:
            diagnostics.append(exc.diagnostic)
    if options.profile == "iec_ref2016_c1_v1":
        _station_corrections(branches, sources, fault_bus_id)
    diagnostics.append(diagnostic("omitted_loads_shunts", "Ordinary loads and transformer magnetization are omitted. Explicit shunts are included", level="info"))
    return {"model": model, "topology": topology, "buses": buses, "branches": branches, "sources": sources,
            "s_base_mva": s_base, "shunts": shunts, "coverage": coverage, "diagnostics": diagnostics, "sequences": seqs}


def _station_corrections(branches, sources, fault_bus_id):
    """Unit without OLTC, IEC 2016 equation (24), K_SO and internal fault equation (21).

K_SO=K_G*(Un_H/Ur_H)^2*(Ur_L/Un_G)^2*(1-p_T)/(1+p_G).
The adapter declares the detected pair and dimensionless power factor ranges.
Calculated b:Kf factors are never imported as input parameters.
"""
    source_by_id = {s["item"]["id"]: s for s in sources}
    for b in branches:
        record = b["record"]
        sid = record.get("station_source_id")
        if sid is None:
            continue
        if sid not in source_by_id or b["item"]["class"] != "ElmTr2":
            raise SCError("invalid_station_pair", "Invalid generator-transformer pair", b["item"])
        s = source_by_id[sid]
        if b["unit_count"] != s["unit_count"] or b["item"]["bus_to"] != s["bus_id"]:
            raise SCError("invalid_station_pair", "Station LV connection or unit counts do not match", b["item"])
        p_t, p_g = record["station_p_t"], record["station_p_g"]
        if not (0 <= p_t < 1 and p_g >= 0):
            raise SCError("invalid_station_range", "Invalid p_T/p_G ranges", b["item"])
        if fault_bus_id == s["bus_id"]:
            target_t, target_g = 1.0, s["factor"]
        else:
            # El tap SC contiene solo relación nominal y grupo, posición neutra.
            target_t = target_g = s["factor"] / abs(b["tap"])**2 * (1-p_t)/(1+p_g)
        for item, target in ((b, target_t), (s, target_g)):
            ratio = target/item["factor"]
            item["z_ohm"] = {seq: z*ratio for seq, z in item["z_ohm"].items()}
            item["factor"] = target


def stamp_island(network, nodes, seq, *, backend="dense"):
    "Assemble sequence Y and terminal stamps in pu, including directed ElmZpu networks."
    index = {node: i for i, node in enumerate(nodes)}
    if backend == "sparse":
        values = [dict() for _ in nodes]
        def add(i,j,value): values[i][j] = values[i].get(j,0j) + value
    else:
        y = np.zeros((len(nodes), len(nodes)), dtype=complex)
        def add(i,j,value): y[i,j] += value
    stamps = []
    b2n, buses, sb = network["topology"]["bus_to_node"], network["buses"], network["s_base_mva"]
    for source in network["sources"]:
        node = b2n[source["bus_id"]]
        if node not in index or source.get("excluded"):
            continue
        kv = buses[source["bus_id"]]["un_kv"]
        zs = source["z_ohm"][seq] / (kv*kv/sb)
        add(index[node], index[node], source["unit_count"]/zs)
    for branch in network["branches"]:
        item = branch["item"]
        if "native_matrix" in branch:
            bus_ids = branch["bus_ids"]
            if not all(b2n[b] in index for b in bus_ids):
                continue
            ports = [index[b2n[b]] for b in bus_ids]
            mat = branch["native_matrix"].T if seq == 2 else branch["native_matrix"]
            for a, i in enumerate(ports):
                for b, j in enumerate(ports):
                    add(i, j, mat[a, b])
            stamps.append((branch, ports, mat))
            continue
        nf, nt = b2n[item["bus_from"]], b2n[item["bus_to"]]
        if nf not in index or nt not in index:
            continue
        uf, ut = buses[item["bus_from"]]["un_kv"], buses[item["bus_to"]]["un_kv"]
        if item["class"] != "ElmTr2" and uf != ut:
            raise SCError("incompatible_voltage_bases", "A branch without a transformer joins different kV bases", item)
        z = branch["z_ohm"][seq] / (uf*uf/sb)
        t = branch["tap"].conjugate() if seq == 2 else branch["tap"]
        mat = np.array([[1/abs(t)**2, -1/t.conjugate()], [-1/t, 1]], complex)/z
        mode = branch["zero_mode"] if seq == 0 else "series"
        if mode != "series":
            mat[:] = 0
            if mode == "ground_from":
                mat[0, 0] = 1/z
            elif mode == "ground_to":
                mat[1, 1] = 1/z
        if item["class"] == "ElmZpu":
            # Datos SC propios, jamás se importan las potencias Norton LF.
            p = branch["record"].get("parameters", branch["record"])
            zr = impedance_ohm(branch["record"], f"z{seq}_reverse", ut, item)/(ut*ut/sb)
            # k es la relación compleja del TechRef, no un tap 1/k.
            mat = np.array([[abs(t)**2/z, -t.conjugate()/z], [-t/zr, 1/zr]], complex)
            for row, key in ((0, f"y{seq}_from"), (1, f"y{seq}_to")):
                if key in p:
                    from .data import complex_value
                    d = parameter(branch["record"], key, item, unit="S")
                    sh = complex_value(d["value"])*(uf*uf/sb if row == 0 else ut*ut/sb)
                    if not math.isfinite(abs(sh)):
                        raise SCError("nonfinite_sc_data", "Nonfinite short-circuit shunt", item)
                    mat[row, row] += sh*(abs(t)**2 if row == 0 else 1)
        mat *= branch["unit_count"]
        ports = [index[nf], index[nt]]
        for a in range(2):
            for b in range(2):
                add(ports[a], ports[b], mat[a, b])
        stamps.append((branch, ports, mat))
    for shunt in network.get("shunts", []):
        node = b2n[shunt["item"]["bus"]]
        if node in index:
            add(index[node], index[node], shunt["admittance"])
    if backend == "sparse":
        from ..compiled import _scipy
        data, columns, indptr = [], [], [0]
        for row in values:
            for column, value in sorted(row.items()):
                columns.append(column)
                data.append(value)
            indptr.append(len(data))
        y = _scipy()[0]((np.asarray(data,complex), np.asarray(columns,int), np.asarray(indptr,int)),shape=(len(nodes),len(nodes)))
    return y, stamps


def solve_column(y, position, diagnostics, *, factorization=None, diagnostic_condition=False):
    """Solve a Z column and enforce a relative residual of at most 1e-10."""
    values=y.data if hasattr(y,'toarray') else y
    if not np.all(np.isfinite(values)):
        raise SCError('nonfinite_sequence_matrix','Sequence Y contains nonfinite values',status='numerical_failure')
    condition=None
    if diagnostic_condition:
        dense=y.toarray() if hasattr(y,'toarray') else y
        condition=float(np.linalg.cond(dense))
        if not math.isfinite(condition):
            raise SCError('singular_sequence_matrix','Sequence Y is singular',status='numerical_failure')
        if condition>1e12:
            diagnostics.append(diagnostic('ill_conditioned_sequence_matrix','cond(Y)>1e12',condition_number=condition))
    rhs=np.zeros(y.shape[0],complex);rhs[position]=1
    try:
        column=factorization.column(position) if factorization is not None else np.linalg.solve(y,rhs)
    except np.linalg.LinAlgError as exc:
        raise SCError('singular_sequence_matrix',str(exc),status='numerical_failure') from exc
    residual=float(np.linalg.norm(y@column-rhs)/np.linalg.norm(rhs))
    if not np.all(np.isfinite(column)) or residual>1e-10:
        raise SCError('sequence_residual_failure',f'Sequence residual {residual:.6g}',status='numerical_failure')
    return column,{'condition_number':condition,'relative_residual':residual}
