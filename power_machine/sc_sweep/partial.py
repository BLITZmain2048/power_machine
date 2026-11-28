"Explicit data, numerical methods and traceable results for sc sweep studies."
from __future__ import annotations

import copy
import cmath
import csv
import json
import math
from pathlib import Path

from power_machine.topology import build_topology, ideal_line_candidate, ideal_zpu_candidate


DATA_SCHEMA = "power_machine-sc-data-v1"


def _datum(value, unit, item, attribute, **extra):
    return {"value": value, "unit": unit, "source_path": "model.json#" + item["id"],
            "source_attribute": attribute, **extra}


def _complex(value):
    return {"real": float(value.real), "imag": float(value.imag)}


def transformer_phase_loops(model, supplement):
    "Check phase-loop consistency using the prepared positive-sequence transformer ratios."
    topology = build_topology(model)
    elements = supplement.get("elements", {})
    parent, weight, tree, cycles = {}, {}, {}, []

    def find(node):
        if node not in parent:
            parent[node], weight[node] = node, 0.0
        if parent[node] == node:
            return node, weight[node]
        root, upstream = find(parent[node])
        weight[node] += upstream
        parent[node] = root
        return root, weight[node]

    def tree_path(start, target):
        queue, previous = [start], {start: (None, None)}
        while queue:
            node = queue.pop(0)
            if node == target:
                break
            for adjacent, edge in tree.get(node, ()):
                if adjacent not in previous:
                    previous[adjacent] = (node, edge)
                    queue.append(adjacent)
        path, node = [], target
        while previous.get(node, (None, None))[0] is not None:
            node, edge = previous[node]
            path.append(edge)
        return path

    active = set(topology["active"]["branches"])
    for branch in sorted(model.get("branches", ()), key=lambda value: value["id"]):
        if branch["id"] not in active:
            continue
        source = topology["bus_to_node"][branch["bus_from"]]
        target = topology["bus_to_node"][branch["bus_to"]]
        if source == target:
            continue
        parameters = elements.get(branch["id"], {}).get("parameters", {})
        relation = parameters.get("tap_pu") if branch.get("class") == "ElmTr2" else parameters.get("ratio_pu")
        value = relation.get("value") if isinstance(relation, dict) else None
        phase = 0.0
        if isinstance(value, dict) and value.get("real") is not None and value.get("imag") is not None:
            phase = math.degrees(math.atan2(float(value["imag"]), float(value["real"])))
        root_source, angle_source = find(source)
        root_target, angle_target = find(target)
        if root_source != root_target:
            parent[root_target] = root_source
            weight[root_target] = angle_source + phase - angle_target
            tree.setdefault(source, []).append((target, branch))
            tree.setdefault(target, []).append((source, branch))
            continue
        residual = (angle_source + phase - angle_target + 180.0) % 360.0 - 180.0
        path = tree_path(source, target)
        cycles.append({"closing_object_id": branch["id"], "closing_relative_path": branch.get("relative_path", branch.get("path")),
                       "closing_name": branch.get("name"), "closing_phase_deg": phase,
                       "residual_deg": residual,
                       "path": [{"object_id": edge["id"], "relative_path": edge.get("relative_path", edge.get("path")),
                                 "name": edge.get("name")} for edge in path]})
    return {"method": "ciclo fundamental sobre tap_pu o ratio_pu positivo, residual modulo 360 grados",
            "cycle_count": len(cycles), "cycles": cycles,
            "incompatible_cycles": [cycle for cycle in cycles if abs(cycle["residual_deg"]) > 1e-7]}


def _tap(item, buses):
    """Main short-circuit ratio, independent of the power-flow tap position."""
    t = item.get("type_params", {})
    source, target = buses.get(item.get("bus_from")), buses.get(item.get("bus_to"))
    if source is None or target is None:
        return None
    uh, ul, un_h, un_l, clock = (t.get("utrn_h"), t.get("utrn_l"),
                                  source.get("un_kv"), target.get("un_kv"),
                                  t.get("nt2ag"))
    if None in (uh, ul, un_h, un_l, clock):
        return None
    if not all(math.isfinite(float(value)) for value in (uh, ul, un_h, un_l, clock)):
        return None
    if min(float(uh), float(ul), float(un_h), float(un_l)) <= 0:
        return None
    nominal_ratio = (float(uh) / float(un_h)) / (float(ul) / float(un_l))
    phase = math.radians(float(clock) * 30.0)
    return _complex(nominal_ratio * complex(math.cos(phase), math.sin(phase)))

def _operating_tap(item):
    """Exported LF tap for provenance only, never for SC stamping."""
    p, t = item.get("params", {}), item.get("type_params", {})
    n = p.get("nntap")
    n0, step, angle = t.get("nntap0"), t.get("dutap"), t.get("phitr", 0.0)
    if None in (n, n0, step):
        return None
    delta = (float(n) - float(n0)) * float(step) / 100.0
    phase = math.radians(float(angle))
    ratio = 1.0 + delta * complex(math.cos(phase), math.sin(phase))
    if abs(ratio) == 0:
        return None
    return _complex(ratio)


def _transformer_z1(item):
    t = item.get("type_params", {})
    uk, pcu, sn = t.get("uktr"), t.get("pcutr"), t.get("strn")
    un_hv = t.get("utrn_h")
    n_units = item.get("params", {}).get("ntnum", 1)
    if None in (uk, pcu, sn, un_hv, n_units) or float(sn) <= 0 or float(un_hv) <= 0 or float(n_units) <= 0:
        return None
    z_pu = float(uk) / 100.0
    r_pu = float(pcu) / (1000.0 * float(sn))
    if r_pu > z_pu:
        return None
    z_unit = complex(r_pu, math.sqrt(z_pu*z_pu-r_pu*r_pu)) * float(un_hv)**2 / float(sn)
    return z_unit / float(n_units), r_pu, z_pu, math.sqrt(z_pu*z_pu-r_pu*r_pu), float(n_units)


def _line_record(item):
    p = item.get("params", {})
    units = item.get("units", {})
    count = p.get("nlnum")
    if p.get("R1") is None or p.get("X1") is None or count is None or float(count) <= 0:
        return None
    return {"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
            "parameters": {
                "z1": _datum(_complex(complex(p["R1"], p["X1"])), "ohm", item, "params.R1 + j*params.X1",
                             original_units={"R1": units.get("R1"), "X1": units.get("X1")},
                             parallel_handling="nlnum retained but not applied without confirmed per-circuit semantics"),
                "parallel_units": _datum(count, "1", item, "params.nlnum", original_unit=units.get("nlnum"))}}


def _transformer_record(item, buses):
    derived = _transformer_z1(item)
    tap = _tap(item, buses)
    operating_tap = _operating_tap(item)
    p, t, u = item.get("params", {}), item.get("type_params", {}), item.get("type_units", {})
    if derived is None or tap is None:
        return None
    z, r_pu, z_pu, x_pu, n_units = derived
    clock = t.get("nt2ag")
    if clock is None or t.get("tr2cn_h") is None or t.get("tr2cn_l") is None:
        return None
    vector = str(t["tr2cn_h"]) + str(t["tr2cn_l"]) + str(int(float(clock)))
    return {"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
            "transformer_role": "network", "parameters": {
                "z1": _datum(_complex(z), "ohm", item, "type_params.uktr, pcutr, strn, utrn_h and params.ntnum",
                             formula="r_pu=pcutr/(1000*strn), z_pu=uktr/100, x_pu=sqrt(z_pu^2-r_pu^2), Zhv=Zpu*utrn_h^2/(strn*ntnum)"),
                "r_pu": _datum(r_pu, "pu", item, "type_params.pcutr/(1000*type_params.strn)"),
                "x_pu": _datum(x_pu, "pu", item, "sqrt((type_params.uktr/100)^2-r_pu^2)"),
                "z_pu": _datum(z_pu, "pu", item, "type_params.uktr/100"),
                "nominal_ratio_pu": _datum(abs(complex(tap["real"], tap["imag"])), "pu", item,
                                             "type_params.utrn_h, utrn_l and bus Un",
                                             formula="(utrn_h/Un_from)/(utrn_l/Un_to)"),
                "vector_group": _datum(vector, "1", item, "type_params.tr2cn_h, tr2cn_l, nt2ag"),
                "clock_shift_deg": _datum(float(clock) * 30.0, "deg", item, "type_params.nt2ag * 30 deg",
                                             original_unit=u.get("nt2ag"), conversion="clock number * 30 deg"),
                "tap_pu": _datum(tap, "pu", item, "nominal short-circuit ratio and vector group",
                                   formula="((utrn_h/Un_from)/(utrn_l/Un_to))*exp(j*nt2ag*30 deg), toma IEC principal"),
                "operating_tap_pu": _datum(operating_tap, "pu", item,
                                             "params.nntap and type_params.nntap0, dutap, phitr",
                                             formula="1+(nntap-nntap0)*dutap/100*exp(j*phitr), LF provenance only"),
                "sn_mva": _datum(float(t["strn"]) * n_units, "MVA", item, "type_params.strn * params.ntnum",
                                  original_unit=u.get("strn"), parallel_handling="total power of ntnum parallel units"),
                "ur_kv": _datum(t["utrn_h"], "kV", item, "type_params.utrn_h"),
                "parallel_units": _datum(n_units, "1", item, "params.ntnum", original_unit=item.get("units", {}).get("ntnum"),
                                         parallel_handling="Zhv is divided once by ntnum")}}


def _zpu_record(item, buses):
    p = item.get("params", {})
    required = ("Sn", "r_pu", "x_pu", "r_pu_ji", "x_pu_ji", "uratio", "nphshift", "ag",
                "gi_pu", "bi_pu", "gj_pu", "bj_pu")
    if any(p.get(key) is None for key in required):
        return None
    left, right = buses.get(item.get("bus_from"), {}), buses.get(item.get("bus_to"), {})
    if not left.get("un_kv") or not right.get("un_kv"):
        return None
    sn = float(p["Sn"])
    if sn <= 0 or float(p["r_pu"]) < 0 or float(p["r_pu_ji"]) < 0:
        return None
    ratio = float(p["uratio"]) * complex(math.cos(-math.radians(float(p["nphshift"])*30 + float(p["ag"]))),
                                           math.sin(-math.radians(float(p["nphshift"])*30 + float(p["ag"]))))
    def admittance(g, b, kv):
        return _complex(complex(float(g), float(b)) * sn / float(kv)**2)
    return {"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
            "parameters": {
                "z1": _datum(_complex(complex(p["r_pu"], p["x_pu"])), "pu", item, "params.r_pu + j*params.x_pu",
                              base_mva=sn, base_kv=left["un_kv"]),
                "z1_reverse": _datum(_complex(complex(p["r_pu_ji"], p["x_pu_ji"])), "pu", item,
                                      "params.r_pu_ji + j*params.x_pu_ji", base_mva=sn, base_kv=right["un_kv"]),
                "ratio_pu": _datum(_complex(ratio), "pu", item, "params.uratio, nphshift, ag"),
                "y1_from": _datum(admittance(p["gi_pu"], p["bi_pu"], left["un_kv"]), "S", item,
                                     "params.gi_pu + j*params.bi_pu, Sn and Un bases"),
                "y1_to": _datum(admittance(p["gj_pu"], p["bj_pu"], right["un_kv"]), "S", item,
                                   "params.gj_pu + j*params.bj_pu, Sn and Un bases")}}


def _machine_record(item):
    p, t = item.get("params", {}), item.get("type_params", {})
    needed = ("xdss", "rstr", "sgn", "ugn", "cosn")
    if any(t.get(key) is None for key in needed) or p.get("qgini") is None:
        return None
    q = float(p["qgini"])
    if q == 0:
        return None
    n_units = p.get("ngnum", 1)
    if n_units is None or float(n_units) <= 0:
        return None
    n_units = float(n_units)
    z_base_unit = float(t["ugn"])**2 / float(t["sgn"])
    r_unit = float(t["rstr"]) * z_base_unit
    x_unit = float(t["xdss"]) * z_base_unit
    return {"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
            "bus_id": item["bus"], "kind": "synchronous", "parameters": {
                "x_d_subtransient_pu": _datum(t["xdss"], "pu", item, "type_params.xdss"),
                "sn_mva": _datum(float(t["sgn"]) * n_units, "MVA", item, "type_params.sgn * params.ngnum",
                                  parallel_handling="total power of parallel units"),
                "ur_kv": _datum(t["ugn"], "kV", item, "type_params.ugn"),
                "r_generator": _datum(t["rstr"], "pu", item, "type_params.rstr", base_mva=float(t["sgn"]) * n_units,
                                      base_kv=t["ugn"], parallel_handling="total base of parallel units"),
                "cos_phi_rated": _datum(t["cosn"], "1", item, "type_params.cosn"),
                "sin_phi_sign": _datum(1.0 if q > 0 else -1.0, "1", item, "sign(qgini)"),
                "z_base_unit_ohm": _datum(z_base_unit, "ohm", item, "type_params.ugn^2/type_params.sgn"),
                "r_subtransient_unit_ohm": _datum(r_unit, "ohm", item, "type_params.rstr*z_base_unit_ohm"),
                "x_subtransient_unit_ohm": _datum(x_unit, "ohm", item, "type_params.xdss*z_base_unit_ohm"),
                "parallel_units": _datum(n_units, "1", item, "params.ngnum", original_unit=item.get("units", {}).get("ngnum"),
                                         parallel_handling="parallel-unit admittances aggregated once")}}


def derive_supplement(model):
    "Derive available positive-sequence data only and document all omissions."
    buses = {item["id"]: item for item in model.get("buses", ())}
    elements, sources, missing, three_winding = {}, {}, [], {}
    for item in model.get("branches", ()):
        record = (_line_record(item) if item.get("class") == "ElmLne" else
                  _transformer_record(item, buses) if item.get("class") == "ElmTr2" else
                  _zpu_record(item, buses) if item.get("class") == "ElmZpu" else None)
        if record is None:
            missing.append({"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                            "reason": "Insufficient data or class outside the partial three-phase domain"})
        else:
            elements[item["id"]] = record
            if item.get("class") == "ElmLne" and any(item.get("params", {}).get(key) is None for key in
                                                        ("conductor_temperature_c", "alpha20_per_c", "resistance_reference_c")):
                missing.append({"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                                "reason": "Minimum case cannot be derived without conductor temperature, alpha20 and resistance reference"})
    for item in model.get("gens", ()):
        if item.get("class") == "ElmSym":
            record = _machine_record(item)
            if record is None:
                missing.append({"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                                "reason": "Machine is missing one or more of xdss, rstr, sgn, ugn, cosn and qgini"})
            else:
                sources[item["id"]] = record
    for item in model.get("branches3", ()):
        try:
            spec = _tr3_star_spec(item, buses, model.get("meta", {}).get("s_base_mva", 100.0))
            three_winding[item["id"]] = {
                "object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                "status": "available_positive_sequence",
                "pair_convention": spec["pair_convention"],
                "winding_impedances_pu": [_complex(value) for value in spec["winding_impedances_pu"]],
                "ratios": [_complex(value) for value in spec["ratios"]], "trace": spec["trace"]}
        except ValueError as exc:
            reason = str(exc)
            three_winding[item["id"]] = {"object_id": item["id"],
                                           "relative_path": item.get("relative_path", item.get("path")),
                                           "status": "missing", "reason": reason}
            missing.append({"object_id": item["id"], "relative_path": item.get("relative_path", item.get("path")),
                            "reason": "ElmTr3 no derivable: " + reason})
    return {"schema": DATA_SCHEMA, "elements": elements, "sources": sources,
            "assumptions": [{"kind": "three_phase_only", "message": "Zero and negative sequences were not derived"},
                            {"kind": "boundary_equivalent", "message": "An SC equivalent is not inferred from power-flow powers"}],
            "three_winding": three_winding, "missing": missing}


def _thevenin_record(host_bus, case, data):
    key = "maximum" if case == "maximum" else "minimum"
    value = data.get(key, {}) if isinstance(data, dict) else {}
    sk, xr = value.get("sk_mva"), value.get("x_r")
    c = value.get("c", data.get("c") if isinstance(data, dict) else None)
    if sk is None or xr is None or c is None or float(sk) <= 0 or float(xr) <= 0 or float(c) <= 0:
        raise ValueError('[invalid_input] ' + "Thevenin mode requires positive Sk'' [MVA], X/R and c for " + key)
    kv = float(host_bus["un_kv"])
    z_abs = float(c) * kv*kv/float(sk)
    r = z_abs/math.sqrt(1 + float(xr)**2)
    z = complex(r, r*float(xr))
    item = {"id": "BOUNDARY_THEVENIN", "relative_path": "entrada_usuario.BOUNDARY_THEVENIN"}
    return {"BOUNDARY_THEVENIN": {"kind": "external", "bus_id": host_bus["id"], "relative_path": item["relative_path"],
            "parameters": {"z1": {"value": _complex(z), "unit": "ohm", "source_path": "request.input.thevenin",
                                      "source_attribute": key + ".sk_mva, " + key + ".x_r and " + key + ".c",
                                      "formula": "abs(Zth)=c*Un^2/Sk'', R=abs(Zth)/sqrt(1+(X/R)^2), X=(X/R)*R"},
                           "c_factor": {"value": float(c), "unit": "1", "source_path": "request.input.thevenin",
                                        "source_attribute": key + ".c"}}}}


def _component(start, edges):
    seen, pending = {start}, [start]
    while pending:
        node = pending.pop()
        for other in edges.get(node, ()):
            if other not in seen:
                seen.add(other)
                pending.append(other)
    return seen


def _three_winding_blocked_buses(model, selected_buses, host_id, supported_ids=()):
    """Identify buses whose only boundary connection is an ElmTr3 with missing data."""
    normal, full = {}, {}
    supported_ids = set(supported_ids)
    def add(edges, left, right):
        if left in selected_buses and right in selected_buses:
            edges.setdefault(left, set()).add(right)
            edges.setdefault(right, set()).add(left)
    for branch in model.get("branches", ()):
        if branch.get("outserv") != 1:
            add(normal, branch.get("bus_from"), branch.get("bus_to"))
            add(full, branch.get("bus_from"), branch.get("bus_to"))
    for switch in model.get("switches", ()):
        if switch.get("outserv") != 1:
            add(normal, switch.get("bus1"), switch.get("bus2"))
            add(full, switch.get("bus1"), switch.get("bus2"))
    for branch in model.get("branches3", ()):
        if branch.get("outserv") == 1:
            continue
        ports = [branch.get(key) for key in ("bus_hv", "bus_mv", "bus_lv")]
        for left in ports:
            for right in ports:
                if left != right:
                    add(full, left, right)
                    if branch["id"] in supported_ids:
                        add(normal, left, right)
    reachable_normal = _component(host_id, normal)
    reachable_full = _component(host_id, full)
    return sorted(reachable_full - reachable_normal)


def _tr3_field(item, name, *, typed=False):
    values = item.get("type_params" if typed else "params", {})
    value = values.get(name)
    if value is None:
        raise ValueError('[invalid_input] ' + "Missing " + ("type_params." if typed else "params.") + name)
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('[invalid_input] ' + "Valor no finito en " + name)
    return value


def _tr3_source(item, field, *, typed=True):
    units = item.get("type_units" if typed else "units", {})
    values = item.get("type_params" if typed else "params", {})
    return {"field": field, "value": values.get(field), "unit": units.get(field, "1"),
            "source_path": "model.json#" + item["id"],
            "source_attribute": ("type_params." if typed else "params.") + field}


def _tr3_tap_ratios(item, voltages_kv, buses_kv):
    """Reproduce the complex ratios from elements.y_tr3 for the star network."""
    typed = item.get("type_params", {})
    ratios = [float(voltages_kv[index]) / float(buses_kv[index]) * cmath.exp(
        -1j * math.radians(_tr3_field(item, "nt3ag_" + key, typed=True) * 30.0))
              for index, key in enumerate("hml")]
    for suffix in ("", "2", "3"):
        if suffix == "" and "dutap" not in typed:
            continue
        if suffix and not typed.get("itapch" + suffix, 0):
            continue
        side = int(_tr3_field(item, "tap_side" + suffix, typed=True))
        if side not in (0, 1, 2):
            raise ValueError('[invalid_input] ' + "Unsupported ElmTr3 tap_side")
        delta = _tr3_field(item, "dutap" + suffix, typed=True) / 100.0
        neutral = _tr3_field(item, "nntap0" + suffix, typed=True)
        position = _tr3_field(item, "nntap" + suffix)
        lower, upper = typed.get("ntpmn" + suffix), typed.get("ntpmx" + suffix)
        if lower is not None and upper is not None:
            if float(lower) > float(upper):
                raise ValueError('[invalid_input] ' + "Inverted ElmTr3 tap limits")
            position = min(max(position, float(lower)), float(upper))
        phase = math.radians(_tr3_field(item, "phitr" + suffix, typed=True))
        factor = 1.0 + (position - neutral) * delta * cmath.exp(1j * phase)
        if abs(factor) == 0:
            raise ValueError('[invalid_input] ' + "ElmTr3 tap produces a zero ratio")
        ratios[side] *= factor
    for index, key in enumerate("hml"):
        if typed.get("du3tp_" + key) is None:
            continue
        position = _tr3_field(item, "n3tap_" + key)
        lower = _tr3_field(item, "n3tmn_" + key, typed=True)
        upper = _tr3_field(item, "n3tmx_" + key, typed=True)
        if lower > upper:
            raise ValueError('[invalid_input] ' + "Inverted ElmTr3 n3tmn/n3tmx limits")
        position = min(max(position, lower), upper)
        neutral = _tr3_field(item, "n3tp0_" + key, typed=True)
        step = _tr3_field(item, "du3tp_" + key, typed=True) / 100.0
        phase = math.radians(_tr3_field(item, "ph3tr_" + key, typed=True))
        factor = 1.0 + (position - neutral) * step * cmath.exp(1j * phase)
        if abs(factor) == 0:
            raise ValueError('[invalid_input] ' + "An individual ElmTr3 tap produces a zero ratio")
        ratios[index] *= factor
    return ratios


def _tr3_star_spec(item, buses, s_base_mva):
    """Derive a positive-sequence three-winding star using the power-flow convention.

Pairs are H-M, M-L and L-H. Synthetic branches preserve the series stamp
from y_tr3 with nominal and power-flow tap ratios.
"""
    if not math.isfinite(float(s_base_mva)) or float(s_base_mva) <= 0:
        raise ValueError('[invalid_input] ' + "Invalid S_base_mva for ElmTr3")
    keys, bus_keys = "hml", ("bus_hv", "bus_mv", "bus_lv")
    port_ids = [item.get(key) for key in bus_keys]
    if any(port not in buses for port in port_ids):
        raise ValueError('[invalid_input] ' + "ElmTr3 winding bus is missing")
    buses_kv = []
    for port in port_ids:
        value = buses[port].get("un_kv")
        if value is None or not math.isfinite(float(value)) or float(value) <= 0:
            raise ValueError('[invalid_input] ' + "Missing ElmTr3 bus nominal voltage")
        buses_kv.append(float(value))
    rated = [_tr3_field(item, "strn3_" + key, typed=True) for key in keys]
    voltages = [_tr3_field(item, "utrn3_" + key, typed=True) for key in keys]
    if any(value <= 0 for value in rated + voltages):
        raise ValueError('[invalid_input] ' + "ElmTr3 rated power or voltage is not positive")
    pair_specs = ((0, 1, "h"), (1, 2, "m"), (2, 0, "l"))
    pair_impedances = []
    for left, right, key in pair_specs:
        pair_mva = min(rated[left], rated[right])
        uk = _tr3_field(item, "uktr3_" + key, typed=True) / 100.0
        typed = item.get("type_params", {})
        loss_name = "pcut3_" + key if typed.get("pcut3_" + key) is not None else "pcutr3_" + key
        pcu_kw = _tr3_field(item, loss_name, typed=True)
        r_pu = pcu_kw / (1000.0 * pair_mva)
        reported_r = typed.get("uktrr3_" + key)
        if reported_r is not None and not math.isclose(100.0 * r_pu, float(reported_r), abs_tol=1e-5):
            raise ValueError('[invalid_input] ' + "Incompatible ElmTr3 pcut3 and uktrr3")
        if uk < abs(r_pu):
            raise ValueError('[invalid_input] ' + "uktr3 is smaller than the resistive component")
        pair_impedances.append(complex(r_pu, math.sqrt(max(uk * uk - r_pu * r_pu, 0.0)))
                               * float(s_base_mva) / pair_mva)
    z_hm, z_ml, z_lh = pair_impedances
    winding_impedances = ((z_hm + z_lh - z_ml) / 2.0,
                           (z_hm + z_ml - z_lh) / 2.0,
                           (z_ml + z_lh - z_hm) / 2.0)
    if any(abs(value) == 0 for value in winding_impedances):
        raise ValueError('[invalid_input] ' + "ElmTr3 has zero winding impedance")
    ratios = _tr3_tap_ratios(item, voltages, buses_kv)
    trace = []
    for key in keys:
        for field in ("strn3_" + key, "uktr3_" + key, "utrn3_" + key, "nt3ag_" + key,
                      "pcut3_" + key, "pcutr3_" + key, "uktrr3_" + key,
                      "du3tp_" + key, "n3tmn_" + key, "n3tmx_" + key,
                      "n3tp0_" + key, "ph3tr_" + key):
            if item.get("type_params", {}).get(field) is not None:
                trace.append(_tr3_source(item, field, typed=True))
        if item.get("params", {}).get("n3tap_" + key) is not None:
            trace.append(_tr3_source(item, "n3tap_" + key, typed=False))
    for suffix in ("", "2", "3"):
        for field in ("dutap" + suffix, "tap_side" + suffix, "nntap0" + suffix, "phitr" + suffix,
                      "ntpmn" + suffix, "ntpmx" + suffix):
            if item.get("type_params", {}).get(field) is not None:
                trace.append(_tr3_source(item, field, typed=True))
        if item.get("params", {}).get("nntap" + suffix) is not None:
            trace.append(_tr3_source(item, "nntap" + suffix, typed=False))
    return {"star_bus_id": "SC_STAR_" + item["id"], "star_un_kv": buses_kv[0],
            "port_ids": port_ids, "port_keys": keys, "winding_impedances_pu": winding_impedances,
            "ratios": ratios, "trace": trace,
            "pair_convention": "H-M=uktr3_h, M-L=uktr3_m, L-H=uktr3_l, base=min(strn3 of each pair)"}


def prepare_partial(model, *, mode, case, thevenin=None, bus_ids=None, host_bus_id=None, generator_ids=None):
    "Return a reduced model, supplementary data and coverage for a three-phase fault.\n\nBoundary buses, selected buses, generators and Thevenin parameters are explicit inputs."
    if mode not in ("thevenin", "dynamic_equivalent"):
        raise ValueError('[invalid_input] ' + "boundary_mode must be thevenin or dynamic_equivalent")
    result = copy.deepcopy(model)
    internal = set(bus_ids) if bus_ids is not None else {b["id"] for b in model.get("buses", ()) if b.get("scope") == "sistema_interno"}
    host_id = host_bus_id or next((item.get("host_bus") for item in model.get("interface", ()) if item.get("host_bus")), None)
    if host_id not in internal:
        raise ValueError('[invalid_input] ' + "No internal boundary bus was found in the interface")
    selected_buses = internal if mode == "thevenin" else {b["id"] for b in model.get("buses", ())}
    result["buses"] = [b for b in result.get("buses", ()) if b["id"] in selected_buses]
    classes = {"ElmLne", "ElmTr2", "ElmZpu"}
    kept = []
    for branch in result.get("branches", ()):
        if branch.get("class") not in classes or branch.get("bus_from") not in selected_buses or branch.get("bus_to") not in selected_buses:
            continue
        kept.append(branch)
    result["branches"] = kept
    result["branches3"] = [branch for branch in result.get("branches3", ())
                           if all(branch.get(key) in selected_buses for key in ("bus_hv", "bus_mv", "bus_lv"))]
    result["switches"] = [s for s in result.get("switches", ())
                          if s.get("bus1") in selected_buses and s.get("bus2") in selected_buses]
    if mode == "thevenin":
        result["gens"] = []
    else:
        result["gens"] = [g for g in result.get("gens", ()) if g.get("class") == "ElmSym" and
                          (g["id"] in set(generator_ids) if generator_ids is not None else g.get("bus") in selected_buses)]
    full = derive_supplement(model)
    ideal_branch_ids = {b["id"] for b in result["branches"] if ideal_line_candidate(b) or ideal_zpu_candidate(b)}
    unsupported_branches = [{"object_id": b["id"], "relative_path": b.get("relative_path", b.get("path")),
                             "reason": "Impedance or class is outside the existing short-circuit solver domain"}
                            for b in result["branches"] if b["id"] not in full["elements"] and b["id"] not in ideal_branch_ids]
    result["branches"] = [b for b in result["branches"] if b["id"] in full["elements"] or b["id"] in ideal_branch_ids]
    allowed = {b["id"] for b in result["branches"]}
    data = {"schema": DATA_SCHEMA, "elements": {k: v for k, v in full["elements"].items() if k in allowed},
            "sources": {k: v for k, v in full["sources"].items() if k in {g["id"] for g in result["gens"]}},
            "assumptions": list(full["assumptions"])}
    host = next(b for b in result["buses"] if b["id"] == host_id)
    if mode == "thevenin":
        data["sources"].update(_thevenin_record(host, case, thevenin or {}))
        label = "Positive-sequence three-phase short circuit, partial study with a user-supplied boundary equivalent, not validated against the external reference"
    else:
        label = "Machines from the boundary equivalent dynamic equivalent, not accredited as a short-circuit equivalent"
    converters = [g for g in model.get("gens", ()) if g.get("class") in ("ElmGenstat", "ElmPvsys")]
    cut = ([{"circuit": link.get("circuit"), "link_id": link.get("link_id"), "boundary_bus": link.get("boundary_bus"),
             "host_bus": host_id, "reason": "boundary equivalent link removed when replacing the equivalent with one Thevenin source"}
            for link in model.get("interface", ()) if link.get("host_bus") == host_id]
           if mode == "thevenin" else [])
    tr3_supported, tr3_coverage = set(), []
    buses_by_id = {bus["id"]: bus for bus in result.get("buses", ())}
    for branch3 in result.get("branches3", ()):
        if branch3.get("outserv", 0) == 1:
            continue
        try:
            _tr3_star_spec(branch3, buses_by_id, model.get("meta", {}).get("s_base_mva", 100.0))
            tr3_supported.add(branch3["id"])
            tr3_coverage.append({"object_id": branch3["id"],
                                 "relative_path": branch3.get("relative_path", branch3.get("path")),
                                 "status": "supported_positive_sequence",
                                 "reason": "Expand as a leakage star with provenance for each field"})
        except ValueError as exc:
            tr3_coverage.append({"object_id": branch3["id"],
                                 "relative_path": branch3.get("relative_path", branch3.get("path")),
                                 "status": "not_evaluable",
                                 "reason": str(exc)})
    three_winding_required = (_three_winding_blocked_buses(result, selected_buses, host_id, tr3_supported)
                              if mode == "thevenin" else [])
    tr3_missing_reasons = [item["reason"] for item in tr3_coverage if item["status"] == "not_evaluable"]
    reduced_topology = build_topology(result)
    contracted_ideal = [{"object_id": edge["id"], "class": edge["class"], "bus_from": edge["bus_from"],
                         "bus_to": edge["bus_to"], "reason": "Contracted by electrical topology before short-circuit stamping"}
                        for edge in reduced_topology["ideal_edges"]]
    coverage = {"mode": mode, "study_label": label, "equivalent_label": label, "host_bus_id": host_id,
                "boundary_cut": {"removed_bus_ids": sorted({b["id"] for b in model.get("buses", ()) if b["id"] not in selected_buses}) if mode == "thevenin" else [],
                           "removed_boundary_links": cut, "replacement_source_id": "BOUNDARY_THEVENIN" if mode == "thevenin" else None,
                           "reason": "Avoid adding both the explicit boundary equivalent network and the boundary equivalent" if mode == "thevenin" else "Not applicable. The boundary equivalent dynamic equivalent is retained"},
                "blocked_three_winding_bus_ids": three_winding_required,
                "blocked_three_winding_reasons": {bus_id: tr3_missing_reasons
                                                    for bus_id in three_winding_required},
                "excluded_converters": [{"object_id": g["id"], "relative_path": g.get("relative_path", g.get("path")),
                                         "reason": "Current-limited converter contribution is not implemented"} for g in converters],
                "three_winding": tr3_coverage,
                "contracted_ideal_edges": contracted_ideal,
                "excluded_unsupported_branches": unsupported_branches,
                "missing": full["missing"]}
    return result, data, coverage


def contract_ideal_for_solver(model, supplement):
    "Fuse ideal nodes without claiming individual coupler-current coverage."
    topology = build_topology(model)
    aliases = dict(topology["bus_to_node"])
    by_id = {item["id"]: item for item in model.get("buses", ())}
    groups = {}
    for bus_id, node_id in aliases.items():
        groups.setdefault(node_id, []).append(bus_id)
    for node_id, bus_ids in groups.items():
        voltages = {float(by_id[bus_id]["un_kv"]) for bus_id in bus_ids}
        if len(voltages) != 1:
            raise ValueError('[invalid_input] ' + "Ideal connection joins different nominal voltages en nodo " + node_id)
    result = copy.deepcopy(model)
    result["buses"] = [copy.deepcopy(by_id[node_id]) for node_id in sorted(groups)]
    active_ids = set(topology["active"]["branches"])
    branches = []
    for branch in result.get("branches", ()):
        if branch["id"] not in active_ids:
            continue
        branch["bus_from"] = aliases[branch["bus_from"]]
        branch["bus_to"] = aliases[branch["bus_to"]]
        if branch["bus_from"] != branch["bus_to"]:
            branches.append(branch)
    elements = copy.deepcopy(supplement.get("elements", {}))
    star_buses, star_branches, star_records, star_assumptions = [], [], {}, []
    s_base_mva = float(model.get("meta", {}).get("s_base_mva", 100.0))
    for branch3 in result.get("branches3", ()):
        if branch3["id"] not in set(topology["active"]["branches3"]):
            continue
        try:
            spec = _tr3_star_spec(branch3, by_id, s_base_mva)
        except ValueError:
            continue
        star_id = spec["star_bus_id"]
        star_buses.append({"id": star_id, "object_id": star_id,
                           "name": "Node estrella SC " + branch3.get("name", branch3["id"]),
                           "scope": "derived_sc", "path": "derived/sc_sweep/" + branch3["id"] + "/star",
                           "relative_path": "derived/sc_sweep/" + branch3["id"] + "/star",
                           "un_kv": spec["star_un_kv"], "synthetic": True,
                           "source_object_id": branch3["id"],
                           "source_attribute": "auxiliary calculation bus on the H-winding un_kv base"})
        for index, key in enumerate(spec["port_keys"]):
            source_bus_id = spec["port_ids"][index]
            from_bus_id = aliases[source_bus_id]
            from_kv = float(by_id[source_bus_id]["un_kv"])
            star_kv = float(spec["star_un_kv"])
            winding_z_pu = spec["winding_impedances_pu"][index]
            branch_id = "SC_TR3_" + branch3["id"] + "_" + key
            relative_path = "derived/sc_sweep/" + branch3["id"] + "/winding_" + key
            if from_kv <= 1.0:
                raise ValueError('[invalid_input] ' + "Unsupported low-voltage base in the ElmTr3 adapter")
            # shortcircuit exige ElmTr2 cuando las bases difieren y aplica K_T.
            # La preimagen analítica deja la impedancia efectiva igual a la
            # fuga del flujo sin convertir esa corrección de adaptador en dato.
            transformer_z_pu = winding_z_pu
            transformer_from_id = from_bus_id
            if winding_z_pu.imag < 0:
                # Un triángulo de ensayos puede producir una fuga algebraica
                # negativa aunque las tres impedancias entre devanados sean
                # positivas. ElmTr2 rechaza X propia negativa. Se separa una
                # rama serie de igual base que resta exactamente esa parte.
                transformer_z_pu = complex(winding_z_pu.real, -winding_z_pu.imag)
                series_id = branch_id + "_series"
                series_bus_id = series_id + "_bus"
                series_path = relative_path + "/series"
                series_z_pu = (winding_z_pu - transformer_z_pu) * abs(spec["ratios"][index])**2
                series_z = series_z_pu * from_kv**2 / s_base_mva
                star_buses.append({"id": series_bus_id, "object_id": series_bus_id,
                                   "name": "SC series node " + key + " of " + branch3.get("name", branch3["id"]),
                                   "scope": "derived_sc", "path": series_path + "/bus",
                                   "relative_path": series_path + "/bus", "un_kv": from_kv,
                                   "synthetic": True, "source_object_id": branch3["id"],
                                   "source_attribute": "negative ElmTr3 leakage algebraic decomposition"})
                star_branches.append({"id": series_id, "object_id": series_id, "class": "ElmLne",
                                      "name": "3W series correction " + key + " of " + branch3.get("name", branch3["id"]),
                                      "scope": "derived_sc", "path": series_path, "relative_path": series_path,
                                      "bus_from": from_bus_id, "bus_to": series_bus_id, "outserv": 0,
                                      "derived_from": branch3["id"], "tr3_port": key})
                star_records[series_id] = {
                    "object_id": series_id, "relative_path": series_path,
                    "derived_from_object_id": branch3["id"], "positive_sequence_only": True,
                    "trace": spec["trace"], "parameters": {
                        "z1": _datum(_complex(series_z), "ohm", branch3,
                                     "negative ElmTr3 leakage algebraic correction",
                                     formula="Z_series=(Z_leg-Z_transformer)*abs(a_port)^2*Un_port^2/S_base",
                                     base_mva=s_base_mva, base_kv=from_kv)}}
                transformer_from_id = series_bus_id
            cmax, numerator = 1.10, 0.95 * 1.10
            denominator = numerator - 0.6 * transformer_z_pu.imag
            if denominator <= 0:
                raise ValueError('[invalid_input] ' + "ElmTr3 leakage is outside the K_T correction domain")
            input_x_pu = transformer_z_pu.imag / denominator
            kt = numerator / (1.0 + 0.6 * input_x_pu)
            input_z_pu = complex(transformer_z_pu.real / kt, input_x_pu)
            z_input = input_z_pu * from_kv**2 / s_base_mva
            z_transformer_effective = transformer_z_pu * from_kv**2 / s_base_mva
            z_total_effective = winding_z_pu * from_kv**2 / s_base_mva
            star_branches.append({"id": branch_id, "object_id": branch_id, "class": "ElmTr2",
                                  "name": "3W leakage " + key + " of " + branch3.get("name", branch3["id"]),
                                  "scope": "derived_sc", "path": relative_path, "relative_path": relative_path,
                                  "bus_from": transformer_from_id, "bus_to": star_id, "outserv": 0,
                                  "derived_from": branch3["id"], "tr3_port": key})
            star_records[branch_id] = {
                "object_id": branch_id, "relative_path": relative_path,
                "derived_from_object_id": branch3["id"],
                "derived_from_relative_path": branch3.get("relative_path", branch3.get("path")),
                "positive_sequence_only": True,
                "transformer_role": "network",
                "pair_convention": spec["pair_convention"],
                "trace": spec["trace"],
                "parameters": {
                    "z1": _datum(_complex(z_input), "ohm", branch3,
                                 "ElmTr3 star, winding impedance " + key,
                                 formula="Z_input=Z_leg/K_T, Z_leg_ohm=Z_leg_pu*Un_port^2/S_base",
                                 base_mva=s_base_mva, base_kv=from_kv),
                    "z1_effective": _datum(_complex(z_transformer_effective), "ohm", branch3,
                                           "ElmTr3 leakage component remaining after adapter K_T",
                                           formula="Z_transformer_effective=K_T*Z_input",
                                           base_mva=s_base_mva, base_kv=from_kv),
                    "z1_total_effective": _datum(_complex(z_total_effective), "ohm", branch3,
                                                 "total ElmTr3 leakage, identical to power-flow leakage",
                                                 formula="Z_total_effective=Z_leg",
                                                 base_mva=s_base_mva, base_kv=from_kv),
                    "tap_pu": _datum(_complex(spec["ratios"][index]), "pu", branch3,
                                       "a_port, where a includes utrn3/un_kv, nt3ag*30deg and taps",
                                       formula="tap_pu=a_port"),
                    "vector_group": _datum("SC-3W-positive", "1", branch3,
                                             "positive-sequence adapter, vector groups are not used for 3ph faults"),
                    "sn_mva": _datum(s_base_mva, "MVA", branch3,
                                      "model system base for adapter K_T compensation"),
                    "ur_kv": _datum(from_kv, "kV", branch3,
                                     "winding bus un_kv for adapter K_T compensation"),
                    "adapter_kt": _datum(kt, "1", branch3,
                                           "K_T computed by the engine to preserve power-flow Z_leg"),
                    "parallel_units": _datum(1, "1", branch3,
                                              "one equivalent star per ElmTr3 without duplicated windings"),
                }}
        star_assumptions.append({"kind": "three_winding_star", "object_id": branch3["id"],
                                 "relative_path": branch3.get("relative_path", branch3.get("path")),
                                 "message": "ElmTr3 expanded as a positive-sequence star, magnetization omitted in the short-circuit profile"})
    result["branches"] = branches
    result["buses"].extend(star_buses)
    result["branches"].extend(star_branches)
    result["branches3"] = []
    result["switches"] = []
    for generator in result.get("gens", ()):
        generator["bus"] = aliases[generator["bus"]]
    data = copy.deepcopy(supplement)
    kept_ids = {branch["id"] for branch in branches}
    data["elements"] = {item_id: record for item_id, record in elements.items() if item_id in kept_ids}
    data["elements"].update(star_records)
    data.setdefault("assumptions", []).extend(star_assumptions)
    for record in data.get("sources", {}).values():
        if record.get("bus_id") in aliases:
            record["bus_id"] = aliases[record["bus_id"]]
    return result, data, aliases, {node_id: sorted(bus_ids) for node_id, bus_ids in groups.items()}


def load_ref_sc_export(folder, supplement):
    "Enrich supplementary data from a reference simulator export with a normalized parameter table."
    path = Path(folder) / "supplemental.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema") != DATA_SCHEMA:
        raise ValueError('[invalid_input] ' + "REF supplemental.json does not use power_machine-sc-data-v1")
    merged = copy.deepcopy(supplement)
    for section in ("elements", "sources"):
        merged.setdefault(section, {}).update(document.get(section, {}))
    merged.setdefault("assumptions", []).append({"kind": "ref_export", "path": str(path)})
    return merged


def compare_ref_rows(rows, ref_csv, *, tolerance_relative=0.02):
    "Compare initial symmetrical current only when both sides contain finite values."
    if tolerance_relative <= 0:
        raise ValueError('[invalid_input] ' + "Relative tolerance must be positive")
    reference = {}
    with Path(ref_csv).open(encoding="utf-8", newline="") as stream:
        for row in csv.DictReader(stream):
            try:
                reference[row["object_id"]] = float(row["Ikss_kA"])
            except (KeyError, TypeError, ValueError):
                continue
    results = []
    for row in rows:
        actual, expected = row.get("ik_initial_sym_ka"), reference.get(row.get("object_id"))
        if actual is None or expected in (None, 0):
            continue
        relative = abs(actual-expected)/abs(expected)
        results.append({"object_id": row["object_id"], "ref_ikss_ka": expected, "sweep_ik_initial_ka": actual,
                        "relative_error": relative, "within_tolerance": relative <= tolerance_relative})
    return {"metric": "abs(Ik''_sweep-Ik''_REF)/abs(Ik''_REF)", "tolerance_relative": tolerance_relative,
            "rows": results, "status": "not_evaluable" if not results else "compared"}
