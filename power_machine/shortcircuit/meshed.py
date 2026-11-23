"Explicit data, numerical methods and traceable results for shortcircuit studies."
from copy import deepcopy
import math

from . import iec60909
from .data import impedance_ohm, scalar
from .schema import SCError, diagnostic
from .sequences import solve_column, stamp_island


def meshed_quantities(qty, diagnostics, contributions, network, nodes, options, fault):
    if any("native_matrix" in b for b in network["branches"]) or network.get("shunts"):
        diagnostics.append(diagnostic("unsupported_native_peak_profile", "Initial symmetrical current includes native passive elements. Equivalent-frequency peak and breaking corrections are not covered for these circuits"))
        return
    nf = network["topology"]["bus_to_node"][fault.bus_id]
    virtual = deepcopy(network)
    for collection in ("branches", "sources"):
        for item in virtual[collection]:
            if item.get("excluded"):
                continue
            z = item["z_ohm"][1]
            r = z.real
            if collection == "sources" and item["kind"] == "synchronous":
                r = impedance_ohm(item["record"], "r_kappa_ohm", 1, item["item"], allow_zero=True).real*item["factor"]
            item["z_ohm"][1] = complex(r, 0.4*z.imag)
    y, _ = stamp_island(virtual, nodes, 1)
    column, check = solve_column(y, nodes.index(nf), diagnostics)
    ze = column[nodes.index(nf)]
    rx = 0.4*ze.real/ze.imag
    kappa = iec60909.peak_factor(rx, 1)
    ik = qty["Ik_initial_sym_ka"]
    qty.update(RtoX=rx, kappa=kappa, ip_peak_ka=math.sqrt(2)*kappa*ik,
               equivalent_frequency_hz=0.4*options.frequency_hz, peak_numerical_check=check)
    compatible = all(b["item"]["class"] in ("ElmLne", "ElmTr2") and abs(b["tap"]-1) < 1e-12
                     for b in network["branches"])
    if not compatible:
        diagnostics.append(diagnostic("unsupported_meshed_breaking_transfer", "Ib is restricted to reciprocal branches with a unit SC ratio and zero vector group"))
    elif options.min_break_time_s is not None:
        uk_fault = network["buses"][fault.bus_id]["un_kv"]
        decrement = 0j
        source_by_id = {s["item"]["id"]: s for s in network["sources"]}
        for c in contributions:
            if c["kind"] != "source":
                continue
            s = source_by_id[c["object_id"]]
            if s["kind"] not in ("synchronous", "external"):
                raise SCError("unsupported_meshed_source", "Meshed Ib covers synchronous machines only", s["item"], status="unsupported")
            # Bornes de UNA máquina, kA. El cuadrado impide usar la corriente
            # agregada de los paralelos como si correspondiera a una unidad.
            current = c["phase_currents_ka"]["a"] / s["unit_count"]
            mu = 1.0
            if s["kind"] == "synchronous":
                ir = scalar(s["record"], "ir_ka", s["item"], unit="kA", positive=True)
                mu = iec60909.synchronous_decay(abs(current)/ir, options.min_break_time_s)
            uk_source = network["buses"][s["bus_id"]]["un_kv"]
            # TechRef_CommonResultVariables_ShortCircuit PF2024, sec. 2.1.1.1
            # p. 4, red mallada. Z_G incluye la corrección de impedancia,
            # no la impedancia serie del transformador de unidad.
            # Z_G*I_G/E_G es adimensional, Un_G/Un_f refiere kA a la falla.
            emf_kv = qty["voltage_factor"]*uk_source/math.sqrt(3)
            transfer = s["z_ohm"][1]*current/emf_kv
            decrement += s["unit_count"]*(1-mu)*transfer*current*uk_source/uk_fault
            c["mu"] = mu
            c["breaking_transfer_complex"] = transfer
        qty["Ib_sym_ka"] = abs(qty["phase_currents_ka"]["a"]-decrement)
        qty["breaking_method"] = "Meshed network: |Ik - sum(n*(1-mu)*Z_G*I_G^2/E_G*Un_G/Un_f)|, PF2024 TechRef SC p4"
        diagnostics.append(diagnostic("meshed_breaking_documented", "Complex Ib reduction per SC TechRef section 2.1.1.1 p4, with mu and Ir per machine", level="info"))
    diagnostics.append(diagnostic("unsupported_thermal_profile", "Decaying-AC Ith is not evaluated by the C(1) profile"))
