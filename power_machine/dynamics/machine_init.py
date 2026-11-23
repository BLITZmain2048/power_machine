'Machine init support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import cmath
from dataclasses import dataclass
import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from .synchronous import SynchronousMachine


@dataclass(frozen=True)
class MachineInitialState:
    'Initial states and inputs on the base of one machine unit.'

    state: np.ndarray
    ve0: float
    pt0: float
    xmdm0: float
    residual: float
    stator_loss_mw: float


def initialize_machine(machine: SynchronousMachine, v_bus: complex,
                       s_lf_mva: complex, *, residual_tol: float = 1e-9) -> MachineInitialState:
    'Compute rotor fluxes, ve0 and pt0 from the load-flow voltage phasor and total injected MW+jMvar. Bus voltage uses uknom-base p.u. Preserve machine ratings and unit count.'
    c = machine.circuit
    v_machine = complex(v_bus) * machine.bus_un_kv / machine.ugn_kv
    if abs(v_machine) == 0:
        raise _coded_error(ValueError, 'invalid_machine_init_data', 'Cannot initialize ElmSym without bus voltage')
    s_unit = complex(s_lf_mva) / (machine.sgn_mva * machine.n_units)
    i_machine = (s_unit / v_machine).conjugate()
    sat_d = sat_q = 1.0
    phi = cmath.phase(v_machine + complex(c.rstr, c.xaqu+c.xl)*i_machine) - math.pi/2
    for _ in range(200):
        xq = c.xl + c.xaqu*sat_q
        new_phi = cmath.phase(v_machine + complex(c.rstr, xq)*i_machine) - math.pi/2
        rotor_v = v_machine * cmath.exp(-1j*new_phi)
        rotor_i = i_machine * cmath.exp(-1j*new_phi)
        id_pu, iq_pu = rotor_i.real, rotor_i.imag
        psi_ad = rotor_v.imag + c.rstr*iq_pu + c.xl*id_pu
        psi_aq = -c.xaqu*sat_q*iq_pu
        new_sd, new_sq = machine.saturation_factors(psi_ad, psi_aq)
        if max(abs(new_sd-sat_d), abs(new_sq-sat_q),
               abs(math.remainder(new_phi-phi, 2*math.pi))) < 1e-14:
            sat_d, sat_q, phi = new_sd, new_sq, new_phi
            break
        sat_d = 0.5*(sat_d+new_sd)
        sat_q = 0.5*(sat_q+new_sq)
        phi = new_phi
    else:
        raise _coded_error(ValueError, 'invalid_machine_init_data', f'Initial saturation did not converge: {machine.name}')
    # Recalcular exactamente con los factores convergidos.
    phi = cmath.phase(v_machine + complex(c.rstr, c.xl+c.xaqu*sat_q)*i_machine) - math.pi/2
    rotor_v = v_machine * cmath.exp(-1j*phi)
    rotor_i = i_machine * cmath.exp(-1j*phi)
    id_pu, iq_pu = rotor_i.real, rotor_i.imag
    xad, xaq = c.xadu*sat_d, c.xaqu*sat_q
    psi_ad = rotor_v.imag + c.rstr*iq_pu + c.xl*id_pu
    ifd = id_pu + psi_ad/xad
    psi_fd = -xad*id_pu + (xad+c.xrl+c.xfd)*ifd
    psi_1d = -xad*id_pu + (xad+c.xrl)*ifd
    psi_1q = -xaq*iq_pu
    state = [psi_fd, psi_1d, psi_1q]
    if c.turbo:
        state.append(psi_1q)
    state.extend([1.0, phi])
    state = np.asarray(state, dtype=float)
    # TechRef (131)–(136), (67), (109). El torque incluye pérdidas de cobre.
    ve0 = c.xadu*ifd/(machine.ifd_base*machine.efd_base_ratio)
    psi_d = psi_ad-c.xl*id_pu
    psi_q = -xaq*iq_pu-c.xl*iq_pu
    te = (iq_pu*psi_d-id_pu*psi_q)/c.cosn
    xmdm0 = machine.xmdm
    pt0 = te+c.dpu+xmdm0-machine.addmt
    machine.ve, machine.pt, machine.fref = ve0, pt0, 1.0
    d = machine.f(0.0, state, np.empty(0), v_bus)
    predicted = machine.outputs(state, v_bus)
    power_error = abs(complex(predicted["P1_mw"], predicted["Q1_mvar"])-s_lf_mva)
    current_error = abs(machine.current_injection(state, v_bus) -
                        (complex(s_lf_mva)/machine.s_base_mva/complex(v_bus)).conjugate())
    residual = max(float(np.max(np.abs(d))), power_error/(machine.sgn_mva*machine.n_units),
                   current_error)
    if residual > residual_tol:
        raise _coded_error(ValueError, 'invalid_machine_init_data', f'Initialization is not at equilibrium {machine.name}: {residual:.3g}')
    # P_cobre = r |I|² por unidad y por unidad física; pt incluye esta pérdida.
    stator_loss_mw = c.rstr*abs(i_machine)**2*machine.sgn_mva*machine.n_units
    return MachineInitialState(state, ve0, pt0, xmdm0, residual, stator_loss_mw)
