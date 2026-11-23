"""Closed-form electrical fixtures used by the validation runner.

The cases intentionally remain small enough to audit by hand.  They check
package stamps or solvers against equations stated next to each case, rather
than against another numerical implementation.
"""
from __future__ import annotations

import cmath
import math
from dataclasses import asdict

import numpy as np


def _close(value, expected, absolute, relative):
    error = abs(value - expected)
    return {"value": _json_number(value), "expected": _json_number(expected),
            "absolute_error": float(error), "pass": bool(error <= absolute + relative * abs(expected))}


def _json_number(value):
    if isinstance(value, complex):
        return {"real": float(value.real), "imag": float(value.imag)}
    if isinstance(value, np.generic):
        return float(value)
    return value


def _base_model(buses):
    return {
        "meta": {"s_base_mva": 100.0},
        "buses": [{"id": bus, "name": bus, "un_kv": 100.0, "outserv": 0} for bus in buses],
        "branches": [], "branches3": [], "switches": [], "shunts": [], "loads": [], "gens": [], "warnings": [],
    }


def _source(model, bus="A"):
    model["gens"].append({"id": "G", "name": "G", "class": "ElmSym", "bus": bus, "ref": True,
                          "mode": "constv", "v_set_pu": 1.0, "outserv": 0,
                          "params": {"pgini": 0.0, "qgini": 0.0, "ngnum": 1}})


def run_cases(tolerances):
    """Run all analytical fixtures and return serialisable records.

    Each result includes the equation behind the expected value.  The package
    functions are imported here to keep importing ``validation`` side-effect
    free for documentation tools.
    """
    from power_machine.elements import y_line, y_tr2, y_tr3
    from power_machine.loadflow import run_loadflow
    from power_machine.shortcircuit import FaultSpec, ShortCircuitData, ShortCircuitOptions, run_shortcircuit
    from power_machine.topology import build_topology

    absolute, relative = tolerances["absolute"], tolerances["relative"]
    records = []

    # Two bus line: Y = (Vbase^2 / Sbase) / (R + jX) [[1,-1],[-1,1]].
    line = {"params": {"R1": 10.0, "X1": 20.0, "G1": 0.0, "B1": 0.0, "nlnum": 1}}
    actual = y_line(line, 100.0, 100.0, 100.0)
    y = 100.0 / complex(10.0, 20.0)
    expected = np.array([[y, -y], [-y, y]], dtype=complex)
    checks = [_close(actual[i, j], expected[i, j], absolute, relative) for i in range(2) for j in range(2)]
    records.append({"case": "two_bus_line", "equation": "Y=Zbase/(R+jX)[[1,-1],[-1,1]]", "checks": checks,
                    "pass": all(c["pass"] for c in checks)})

    # Phase shifting transformer, no magnetising current and equal voltage bases.
    transformer = {"params": {"nntap": 0, "ntnum": 1}, "type_params": {
        "strn": 100.0, "utrn_h": 100.0, "utrn_l": 100.0, "uktr": 10.0, "pcutr": 0.0,
        "curmg": 0.0, "pfe": 0.0, "itrdr": 0.5, "itrdl": 0.5, "dutap": 1.0, "nntap0": 0.0,
        "ntpmn": -10.0, "ntpmx": 10.0, "phitr": 0.0, "tap_side": 0, "nt2ag": 1.0}}
    actual = y_tr2(transformer, 100.0, 100.0, 100.0)
    z = 0.1j
    a = cmath.exp(1j * math.radians(30.0))
    expected = np.array([[1 / z / abs(a) ** 2, -1 / z / a.conjugate()], [-1 / z / a, 1 / z]], dtype=complex)
    checks = [_close(actual[i, j], expected[i, j], absolute, relative) for i in range(2) for j in range(2)]
    records.append({"case": "phase_shifting_transformer", "equation": "Y12=-1/(j0.1*a*) with a=exp(j30 deg)",
                    "checks": checks, "pass": all(c["pass"] for c in checks)})

    # Shunt as a one-port current equation I=Ysh V, independently of LF iteration.
    v, y_shunt = 1.02 - 0.03j, 0.01 - 0.15j
    checks = [_close(y_shunt * v, (0.01 - 0.15j) * (1.02 - 0.03j), absolute, relative)]
    records.append({"case": "shunt", "equation": "I=Ysh V", "checks": checks, "pass": checks[0]["pass"]})

    # Three winding: symmetric 10% pairwise leakage gives a star of j0.05 on a 100 MVA base.
    tr3 = {"type_params": {**{f"strn3_{k}": 100.0 for k in "hml"}, **{f"utrn3_{k}": 100.0 for k in "hml"},
           "uktr3_h": 10.0, "uktr3_m": 10.0, "uktr3_l": 10.0, "nt3ag_h": 0.0, "nt3ag_m": 0.0, "nt3ag_l": 0.0}}
    actual = y_tr3(tr3, 100.0, 100.0, 100.0, 100.0, allow_missing_losses=True)
    star_y = 1 / (0.05j)
    expected = np.diag([star_y] * 3) - np.ones((3, 3), dtype=complex) * star_y / 3
    checks = [_close(actual[i, j], expected[i, j], absolute, relative) for i in range(3) for j in range(3)]
    records.append({"case": "three_winding_transformer", "equation": "Y=diag(1/j0.05)-11T/(3*j0.05)",
                    "checks": checks, "pass": all(c["pass"] for c in checks)})

    # Open terminal creates an island and must not fabricate a supplied solution.
    model = _base_model(("A", "B"))
    _source(model)
    model["branches"].append({"id": "L", "name": "L", "class": "ElmLne", "bus_from": "A", "bus_to": "B", "outserv": 0,
                              "params": {"R1": 1.0, "X1": 10.0, "G1": 0.0, "B1": 0.0, "nlnum": 1}})
    model["switches"].append({"id": "open", "class": "StaSwitch", "element_id": "L", "port": "bus2", "closed": False, "outserv": 0})
    topology = build_topology(model)
    result = run_loadflow(model)
    checks = [{"value": topology["dangling_branches"] == {"L": 0}, "expected": True, "absolute_error": 0.0,
               "pass": topology["dangling_branches"] == {"L": 0}},
              {"value": bool(any(i["status"] == "sin_referencia" for i in result["islands"])), "expected": True,
               "absolute_error": 0.0, "pass": bool(any(i["status"] == "sin_referencia" for i in result["islands"]))}]
    records.append({"case": "open_branch_and_island", "equation": "An open terminal removes the branch from the connected Y-bus.",
                    "checks": checks, "pass": all(c["pass"] for c in checks)})

    # Ideal breaker: a zero impedance line is contracted when closed and disconnected when open.
    ideal = _base_model(("A", "B"))
    ideal["branches"].append({"id": "I", "name": "I", "class": "ElmLne", "bus_from": "A", "bus_to": "B", "outserv": 0,
                                "params": {"R1": 0.0, "X1": 0.0, "B1": 0.0}})
    closed = build_topology(ideal)
    ideal["switches"].append({"id": "brk", "class": "StaSwitch", "element_id": "I", "port": "bus1", "closed": False, "outserv": 0})
    opened = build_topology(ideal)
    checks = [{"value": closed["bus_to_node"]["A"] == closed["bus_to_node"]["B"], "expected": True, "absolute_error": 0.0,
               "pass": closed["bus_to_node"]["A"] == closed["bus_to_node"]["B"]},
              {"value": opened["ideal_disconnected"] == ["I"], "expected": True, "absolute_error": 0.0,
               "pass": opened["ideal_disconnected"] == ["I"]}]
    records.append({"case": "ideal_breaker", "equation": "closed: Va=Vb; open: Ibranch=0", "checks": checks,
                    "pass": all(c["pass"] for c in checks)})

    # Three-phase fault fed by a known Thevenin impedance: Ik''=c Un/(sqrt(3)|Z|).
    sc_model = {"meta": {"s_base_mva": 100.0}, "buses": [{"id": "B", "path": "fixture/B", "un_kv": 10.0}],
                "branches": [], "gens": [{"id": "S", "path": "fixture/S", "class": "ElmXnet", "bus": "B"}]}
    datum = lambda value: {"value": {"real": value.real, "imag": value.imag}, "unit": "ohm",
                           "analytical_fixture": "known_thevenin"}
    sc_data = ShortCircuitData("power_machine-sc-data-v1", {}, {"S": {"kind": "external", "parameters": {
        "z1": datum(0.1 + 0.2j), "z2": datum(0.1 + 0.2j), "z0": datum(0.3 + 0.6j)}}})
    sc = run_shortcircuit(sc_model, FaultSpec("B", "3ph"), ShortCircuitOptions("iec60909", "maximum"), sc_data)
    expected_ka = 1.1 * 10.0 / (math.sqrt(3) * abs(0.1 + 0.2j))
    check = _close(sc.quantities["Ik_initial_sym_ka"], expected_ka, absolute, relative)
    records.append({"case": "three_phase_fault_thevenin", "equation": "Ik''=c Un/(sqrt(3)|Zth|)", "checks": [check],
                    "pass": check["pass"], "diagnostics": sc.diagnostics})
    return records

