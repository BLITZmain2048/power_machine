'Synchronous support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass
import math
from typing import Mapping

import numpy as np


def _required(data: Mapping[str, object], key: str) -> float:
    value = data.get(key)
    if value is None:
        raise _coded_error(ValueError, 'missing_synchronous_data', f'Missing TypSym parameter {key}. Never replace it with zero')
    result = float(value)
    if not math.isfinite(result):
        raise _coded_error(ValueError, 'invalid_synchronous_data', f'TypSym parameter {key} must be finite')
    return result


def short_circuit_times(params: Mapping[str, object]) -> tuple[float, float, float | None, float]:
    "Return t'd, t''d, t'q and t''q in seconds from explicit TypSym data. Prefer converted exported time constants. Use the declared conversion only when required."
    turbo = int(_required(params, "iturbo")) == 1
    keys = ("tds", "tdss", "tqs", "tqss")
    if all(params.get(k) is not None for k in keys):
        return (float(params["tds"]), float(params["tdss"]),
                float(params["tqs"]) if turbo else None, float(params["tqss"]))
    xd, xds, xdss = (_required(params, k) for k in ("xd", "xds", "xdss"))
    xq, xqss = (_required(params, k) for k in ("xq", "xqss"))
    xqs = _required(params, "xqs") if turbo else xq
    d0, dd0, qq0 = (_required(params, k) for k in ("tds0", "tdss0", "tqss0"))
    q0 = _required(params, "tqs0") if turbo else None

    def convert(x: float, xs: float, xss: float, ts0: float, tss0: float):
        if not bool(int(params.get("i_exactConv", 0))):
            return ts0 * xs / x, tss0 * xss / xs
        # (47): suma y producto de las raíces de cortocircuito.
        product = ts0 * tss0 * xss / x
        aa = x / xs
        bb = 1.0 - x / xs + x / xss
        roots = np.roots([bb, - (ts0 + tss0), aa * product])
        real = [float(z.real) for z in roots if abs(z.imag) < 1e-10 and z.real > 0]
        if len(real) != 2:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Exact time-constant conversion has no physical roots')
        tss = min(real)
        return product / tss, tss

    td, tdd = convert(xd, xds, xdss, d0, dd0)
    if turbo:
        tq, tqq = convert(xq, xqs, xqss, q0, qq0)
    else:
        tq, tqq = None, qq0 * xqss / xq
    return td, tdd, tq, tqq


def _two_rotor_loops(x: float, xs: float, xss: float, xl: float, xrl: float,
                     ts: float, tss: float, omega_nom: float) -> tuple[float, float, float, float]:
    """Conversión exacta de circuito equivalente, TechRef (49)–(53)."""
    x1 = x - xl + xrl
    x2 = x1 - (x - xl) ** 2 / x
    x3 = (x2 - x1 * xss / x) / (1.0 - xss / x)
    t1 = x / xs * ts + (1.0 - x / xs + x / xss) * tss
    t2 = ts + tss
    a = (x2 * t1 - x1 * t2) / (x1 - x2)
    b = x3 / (x3 - x2) * ts * tss
    discriminant = a * a / 4.0 - b
    if discriminant < 0:
        raise _coded_error(ValueError, 'invalid_synchronous_data', 'Equivalent circuit constants must be real')
    tf = -a / 2.0 + math.sqrt(discriminant)
    tk = -a / 2.0 - math.sqrt(discriminant)
    common = (t1 - t2) / (x1 - x2)
    xf = (tf - tk) / (common + tk / x3)
    xk = (tk - tf) / (common + tf / x3)
    rf, rk = xf / (omega_nom * tf), xk / (omega_nom * tk)
    if min(xf, xk, rf, rk) <= 0 or not all(map(math.isfinite, (xf, xk, rf, rk))):
        raise _coded_error(ValueError, 'invalid_synchronous_data', 'Converted rotor parameters are not physical')
    return xf, xk, rf, rk


@dataclass(frozen=True)
class EquivalentCircuit:
    'Equivalent-circuit parameters for one unit on TypSym.sgn and nominal-frequency bases.'

    xadu: float
    xaqu: float
    xl: float
    xrl: float
    xrlq: float
    rstr: float
    xfd: float
    x1d: float
    rfd: float
    r1d: float
    x1q: float
    r1q: float
    x2q: float | None
    r2q: float | None
    tag_s: float
    cosn: float
    dpu: float
    dkd: float
    dpe: float
    turbo: bool
    times_s: tuple[float, float, float | None, float]

    @classmethod
    def from_typ(cls, p: Mapping[str, object], f_nom_hz: float = 50.0):
        omega = 2.0 * math.pi * f_nom_hz
        iturbo = int(_required(p, "iturbo"))
        if iturbo not in (0, 1):
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Unrecognized TypSym iturbo')
        turbo = iturbo == 1
        xd, xq, xl = (_required(p, k) for k in ("xd", "xq", "xl"))
        xds, xdss, xqss = (_required(p, k) for k in ("xds", "xdss", "xqss"))
        if not (xd > xds > xdss > xl >= 0 and xq > xqss > xl):
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Invalid TypSym reactance ordering')
        times = short_circuit_times(p)
        xrl, xrlq = _required(p, "xrl"), _required(p, "xrlq")
        xf, x1d, rf, r1d = _two_rotor_loops(
            xd, xds, xdss, xl, xrl, times[0], times[1], omega)
        if turbo:
            xqs = _required(p, "xqs")
            if not xq > xqs > xqss:
                raise _coded_error(ValueError, 'invalid_synchronous_data', 'Invalid q-axis reactance ordering')
            x1q, x2q, r1q, r2q = _two_rotor_loops(
                xq, xqs, xqss, xl, xrlq,
                times[2], times[3], omega)
        else:
            x1q = (xq - xl) * (xqss - xl) / (xq - xqss)
            r1q = xqss / xq * (xq - xl + x1q) / (omega * times[3])
            x2q = r2q = None
        cosn = _required(p, "cosn")
        if not 0 < cosn <= 1:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'cosn is out of range')
        # tag es la conversión REF a Pgn y prevalece sobre h/iner_inp.
        if p.get("tag") is not None:
            tag = _required(p, "tag")
        elif p.get("h") is not None and p.get("iner_inp") == "DEF":
            tag = 2.0 * _required(p, "h") / cosn
        else:
            raise _coded_error(ValueError, 'missing_synchronous_data', 'Missing converted tag or interpretable iner_inp base')
        if tag <= 0:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Inertia must be positive')
        return cls(xd-xl, xq-xl, xl, xrl,
                   xrlq, _required(p, "rstr"),
                   xf, x1d, rf, r1d, x1q, r1q, x2q, r2q,
                   tag, cosn, _required(p, "dpu"), float(p.get("dkd", 0.0)),
                   float(p.get("dpe", 0.0)), turbo, times)


class SynchronousMachine:
    'RMS synchronous-machine DynamicDevice. init receives total injected load-flow MVA. Voltage uses bus uknom-base p.u. Current injection uses the network base. Retain the exported machine selectors and unit count.'

    n_alg = 0

    def __init__(self, gen: Mapping[str, object], *, bus_un_kv: float,
                 s_base_mva: float = 100.0, f_nom_hz: float = 50.0,
                 saturation: str | None = None, saturation_curve: str | None = None,
                 speed_effect: str | None = None,
                 dynamic_attributes: Mapping[str, object] | None = None):
        self.object_id = str(gen["id"])
        self.relative_path = str(gen["path"])
        self.bus_id = str(gen["bus"])
        self.name = str(gen["name"])
        self.typ = dict(gen["type_params"])
        # Los selectores RMS pertenecen a TypSym y no al CSV portable de LF.
        # Solo se injertan atributos PRESENT del inventario dinámico.
        self.typ.update(dynamic_attributes or {})
        self.elm = dict(gen["params"])
        model_inp = self.typ.get("model_inp", self.elm.get("model_inp"))
        if model_inp not in (None, 0, "0", "standard", "Standard", "det"):
            raise _coded_error(ValueError, 'invalid_synchronous_data', f'model_inp does not match the standard model: {model_inp}')
        self.circuit = EquivalentCircuit.from_typ(self.typ, f_nom_hz)
        self.n_states = 6 if self.circuit.turbo else 5
        self.discrete_state: dict[str, object] = {}
        self.bus_un_kv = float(bus_un_kv)
        self.ugn_kv = _required(self.typ, "ugn")
        self.sgn_mva = _required(self.typ, "sgn")
        self.n_units = int(self.elm.get("ngnum", gen.get("n_units", 1)))
        self.s_base_mva = float(s_base_mva)
        if min(self.bus_un_kv, self.ugn_kv, self.sgn_mva, self.s_base_mva, self.n_units) <= 0:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Bases and unit count must be positive')
        self.omega_nom = 2.0 * math.pi * f_nom_hz
        if saturation is None:
            selector = self.typ.get("isat")
            if selector is None:
                saturation = "dq_magnitude" if self.circuit.turbo else "d_magnitude"
                self.discrete_state["saturation_assumed"] = True
            elif int(selector) == 0:
                saturation = "none"
            elif int(selector) in (1, 3):
                salient = int(self.typ.get("isatSalient", 0))
                saturation = {0: "dq_magnitude", 4: "dq_equal",
                              1: "d_magnitude", 2: "dq_components",
                              3: "d_component"}.get(salient)
                if saturation is None:
                    raise _coded_error(ValueError, 'invalid_synchronous_data', f'Unsupported isatSalient={salient}')
            else:
                raise _coded_error(ValueError, 'invalid_synchronous_data', f'Unsupported isat={selector}')
        if saturation_curve is None:
            selector = self.typ.get("isat")
            saturation_curve = "exponential" if selector is not None and int(selector) == 3 else "quadratic"
            if selector is None:
                self.discrete_state["saturation_curve_assumed"] = True
        if saturation not in ("none", "dq_magnitude", "dq_equal", "d_magnitude",
                              "dq_components", "d_component"):
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Unsupported saturation selector')
        if saturation_curve not in ("quadratic", "exponential"):
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Unsupported saturation curve')
        if speed_effect is None:
            selector = self.typ.get("i_speedVar")
            if selector is None:
                speed_effect = "neglected"
                self.discrete_state["speed_effect_assumed"] = True
            else:
                # REF-05: i_speedVar=1 aplica n a psi'', pero no a x''·i.
                # TechRef §6.1.2.1 (73)–(74), variante parcialmente despreciada;
                # checked against reference d/q currents and rotor fluxes. No se cambian
                # los otros códigos heredados, no contrastados por esta campaña.
                speed_effect = {0: "considered", 1: "partial", 2: "partial"}.get(int(selector))
                if speed_effect is None:
                    raise _coded_error(ValueError, 'invalid_synchronous_data', f'Unsupported i_speedVar={selector}')
        if speed_effect not in ("considered", "neglected", "partial"):
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Unsupported speed effect selector')
        self.saturation = saturation
        self.saturation_curve = saturation_curve
        self.speed_effect = speed_effect
        for missing in ("dkd", "dpe", "addmt", "ifdBase", "efdBaseRatio"):
            if missing not in self.typ and missing not in self.elm:
                self.discrete_state[f"{missing}_assumed"] = True
        self.ve = 1.0
        self.pt = 0.0
        self.xmdm = 0.0
        self.fref = 1.0
        self.nref = 1.0
        self.addmt = float(self.elm.get("addmt", 0.0))
        self.ifd_base = float(self.elm.get("ifdBase", 1.0))
        self.efd_base_ratio = float(self.elm.get("efdBaseRatio", 1.0))
        if self.ifd_base <= 0 or self.efd_base_ratio <= 0:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Excitation base must be positive')
        self._evaluation_key: tuple[bytes, complex] | None = None
        self._evaluation_value: dict[str, float | complex] | None = None

    @property
    def order(self) -> int:
        return self.n_states

    def _csat(self, psi: float, sg10: float, sg12: float) -> float:
        if sg10 == 0 and sg12 == 0:
            return 0.0
        if sg10 <= 0 or sg12 <= 0 or psi <= 0:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Incomplete SG10/SG12 saturation data')
        if self.saturation_curve == "exponential":
            exponent = math.log(1.2 * sg12 / sg10) / math.log(1.2)
            return sg10 * psi ** exponent / psi
        ratio = math.sqrt(sg12 / sg10)
        ag = (1.2 - 1.2 * ratio) / (1.0 - 1.2 * ratio)
        bg = sg10 / (1.0 - ag) ** 2
        return bg * max(psi - ag, 0.0) ** 2 / psi

    def saturation_factors(self, psi_ad: float, psi_aq: float) -> tuple[float, float]:
        'Return magnetic saturation factors with fluxes on the single-unit base.'
        c = self.circuit
        if self.saturation == "none":
            return 1.0, 1.0
        sg10, sg12 = _required(self.typ, "sg10"), _required(self.typ, "sg12")
        if sg10 == 0 and sg12 == 0:
            return 1.0, 1.0
        if self.saturation in ("dq_components", "d_component"):
            sd = 1.0 / (1.0 + self._csat(abs(psi_ad), sg10, sg12))
            if self.saturation == "d_component":
                return sd, 1.0
            q10, q12 = self.typ.get("sg10q"), self.typ.get("sg12q")
            if q10 is None or q12 is None:
                raise _coded_error(ValueError, 'missing_synchronous_data', 'Separate q-axis saturation requires SG10q/SG12q')
            return sd, 1.0 / (1.0 + self._csat(abs(psi_aq), float(q10), float(q12)))
        csat = self._csat(math.hypot(psi_ad, psi_aq), sg10, sg12)
        sd = 1.0 / (1.0 + csat)
        if self.saturation == "d_magnitude":
            return sd, 1.0
        if self.saturation == "dq_equal":
            return sd, sd
        return sd, 1.0 / (1.0 + c.xaqu / c.xadu * csat)

    def _axis(self, psi_fd: float, psi_1d: float, psi_1q: float,
              psi_2q: float | None, id_pu: float, iq_pu: float,
              sat_d: float, sat_q: float) -> dict[str, float]:
        c = self.circuit
        xad, xaq = c.xadu * sat_d, c.xaqu * sat_q
        ad = xad+c.xrl+c.xfd
        bd = xad+c.xrl
        dd = xad+c.xrl+c.x1d
        determinant_d = ad*dd-bd*bd
        rhs_fd, rhs_1d = psi_fd+xad*id_pu, psi_1d+xad*id_pu
        ifd = (dd*rhs_fd-bd*rhs_1d)/determinant_d
        i1d = (ad*rhs_1d-bd*rhs_fd)/determinant_d
        if c.turbo:
            aq = xaq+c.xrlq+c.x1q
            bq = xaq+c.xrlq
            dq = xaq+c.xrlq+c.x2q
            determinant_q = aq*dq-bq*bq
            rhs_1q, rhs_2q = psi_1q+xaq*iq_pu, psi_2q+xaq*iq_pu
            i1q = (dq*rhs_1q-bq*rhs_2q)/determinant_q
            i2q = (aq*rhs_2q-bq*rhs_1q)/determinant_q
        else:
            i1q = (psi_1q + xaq * iq_pu) / (xaq + c.xrlq + c.x1q)
            i2q = 0.0
        psi_ad = xad * (-id_pu + ifd + i1d)
        psi_aq = xaq * (-iq_pu + i1q + i2q)
        psi_d = psi_ad - c.xl * id_pu
        psi_q = psi_aq - c.xl * iq_pu
        return dict(ifd=ifd, i1d=i1d, i1q=i1q, i2q=i2q,
                    psi_ad=psi_ad, psi_aq=psi_aq, psi_d=psi_d, psi_q=psi_q)

    def _evaluate(self, x: np.ndarray, v_bus: complex) -> dict[str, float | complex]:
        x = np.asarray(x, dtype=float)
        if x.size != self.n_states:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Incorrect machine state vector length')
        key = (x.tobytes(), complex(v_bus))
        if key == self._evaluation_key:
            return self._evaluation_value
        psi_fd, psi_1d, psi_1q = x[:3]
        psi_2q = float(x[3]) if self.circuit.turbo else None
        speed, phi = x[-2:]
        if speed <= 0:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Machine speed must be positive')
        rotation = complex(math.cos(phi), math.sin(phi))
        u = complex(v_bus) * (self.bus_un_kv / self.ugn_kv) * rotation.conjugate()
        c = self.circuit
        nflux = speed if self.speed_effect != "neglected" else 1.0
        nreact = speed if self.speed_effect == "considered" else 1.0
        sat_d = sat_q = 1.0
        id_pu = iq_pu = 0.0
        for _ in range(80):
            # Dependencia afín de flujos y voltaje con las corrientes del estator.
            xad, xaq = c.xadu*sat_d, c.xaqu*sat_q
            ad, bd, dd = xad+c.xrl+c.xfd, xad+c.xrl, xad+c.xrl+c.x1d
            det_d = ad*dd-bd*bd
            ifd0 = (dd*psi_fd-bd*psi_1d)/det_d
            i1d0 = (ad*psi_1d-bd*psi_fd)/det_d
            psi_d0 = xad*(ifd0+i1d0)
            xdss = c.xl+xad*(1.0-xad*(c.xfd+c.x1d)/det_d)
            if c.turbo:
                aq, bq, dq = xaq+c.xrlq+c.x1q, xaq+c.xrlq, xaq+c.xrlq+c.x2q
                det_q = aq*dq-bq*bq
                i1q0 = (dq*psi_1q-bq*psi_2q)/det_q
                i2q0 = (aq*psi_2q-bq*psi_1q)/det_q
                psi_q0 = xaq*(i1q0+i2q0)
                xqss = c.xl+xaq*(1.0-xaq*(c.x1q+c.x2q)/det_q)
            else:
                aq = xaq+c.xrlq+c.x1q
                psi_q0 = xaq*psi_1q/aq
                xqss = c.xl+xaq*(1.0-xaq/aq)
            rhs_d = -u.real-nflux*psi_q0
            rhs_q = nflux*psi_d0-u.imag
            off_q, off_d = nreact*xqss, nreact*xdss
            det_stator = c.rstr*c.rstr+off_q*off_d
            new_id = (c.rstr*rhs_d+off_q*rhs_q)/det_stator
            new_iq = (c.rstr*rhs_q-off_d*rhs_d)/det_stator
            axis = self._axis(psi_fd, psi_1d, psi_1q, psi_2q,
                              new_id, new_iq, sat_d, sat_q)
            new_sd, new_sq = self.saturation_factors(axis["psi_ad"], axis["psi_aq"])
            if max(abs(new_sd-sat_d), abs(new_sq-sat_q)) < 1e-13:
                id_pu, iq_pu = new_id, new_iq
                break
            sat_d = 0.5*(sat_d+new_sd)
            sat_q = 0.5*(sat_q+new_sq)
        else:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Algebraic saturation did not converge')
        axis = self._axis(psi_fd, psi_1d, psi_1q, psi_2q,
                          id_pu, iq_pu, sat_d, sat_q)
        current_machine = complex(id_pu, iq_pu) * rotation
        voltage_machine = complex(v_bus) * (self.bus_un_kv / self.ugn_kv)
        power_machine = voltage_machine * current_machine.conjugate()
        torque = (iq_pu*axis["psi_d"] - id_pu*axis["psi_q"]) / c.cosn
        result = {**axis, "id": id_pu, "iq": iq_pu, "speed": speed,
                "phi": phi, "current_machine": current_machine,
                "power_machine": power_machine, "torque": torque,
                "sat_d": sat_d, "sat_q": sat_q}
        self._evaluation_key, self._evaluation_value = key, result
        return result

    def f(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        'Return flux and speed derivatives in p.u./s and rotor-angle derivative in rad/s.'
        a, c = self._evaluate(x, v), self.circuit
        vfd = c.rfd / c.xadu * self.ve * self.ifd_base * self.efd_base_ratio
        d = [self.omega_nom * (vfd - c.rfd*a["ifd"]),
             -self.omega_nom * c.r1d*a["i1d"],
             -self.omega_nom * c.r1q*a["i1q"]]
        if c.turbo:
            d.append(-self.omega_nom * c.r2q*a["i2q"])
        speed = a["speed"]
        tm = self.pt/speed - self.xmdm - c.dpu*speed + self.addmt
        damping = c.dkd*(speed-self.nref) + c.dpe*(speed-self.nref)/speed
        d.extend([(tm-a["torque"]-damping)/c.tag_s,
                  self.omega_nom*(speed-self.fref)])
        return np.asarray(d, dtype=float)

    def g(self, t: float, x: np.ndarray, z: np.ndarray, v: complex) -> np.ndarray:
        'Return no local algebraic variables after eliminating the RMS stator equations.'
        return np.empty(0, dtype=float)

    def current_injection(self, x: np.ndarray, v: complex) -> complex:
        'Return current injection on the declared system MVA and bus-voltage bases.'
        a = self._evaluate(x, v)
        return a["current_machine"] * (self.sgn_mva*self.n_units/self.s_base_mva) * (
            self.bus_un_kv/self.ugn_kv)

    def jacobian_local(self, t: float, x: np.ndarray, z: np.ndarray,
                       v: complex, *, rel_step: float = 1e-6) -> np.ndarray:
        'Return central differences of [f, Re(I), Im(I)]. Batch state and voltage perturbations using the same saturation equations as the residual.'
        point = np.r_[np.asarray(x, dtype=float), float(v.real), float(v.imag)]
        if point.size != self.n_states+2 or rel_step <= 0:
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Invalid local Jacobian point or step')
        steps = np.maximum(1e-8, rel_step*np.maximum(1.0, abs(point)))
        perturbations = np.eye(point.size) * steps
        samples = np.concatenate((point+perturbations, point-perturbations))
        values = np.empty((2*point.size, self.n_states+2))
        empty = np.empty(0)
        for index, sample in enumerate(samples):
            state = sample[:self.n_states]
            voltage = complex(sample[-2], sample[-1])
            current = self.current_injection(state, voltage)
            values[index, :self.n_states] = self.f(t, state, empty, voltage)
            values[index, -2:] = current.real, current.imag
        n = point.size
        result = ((values[:n]-values[n:])/(2*steps[:, None])).T
        if not np.isfinite(result).all():
            raise _coded_error(ValueError, 'invalid_synchronous_data', 'Local Jacobian must be finite')
        return result

    def outputs(self, x: np.ndarray, v: complex) -> dict[str, float | complex]:
        'Return RMS machine variables and total power, positive for generation.'
        a = self._evaluate(x, v)
        s = a["power_machine"] * self.sgn_mva * self.n_units
        ie = self.circuit.xadu * a["ifd"] / self.ifd_base
        return {"xspeed": a["speed"], "phi": a["phi"], "cur1": abs(a["current_machine"]),
                "cur1r": a["current_machine"].real, "cur1i": a["current_machine"].imag,
                "P1": s.real, "Q1": s.imag, "P1_mw": s.real, "Q1_mvar": s.imag,
                "ie": ie,
                "id_pu": a["id"], "iq_pu": a["iq"], "xme": a["torque"]}

    def init(self, v_bus: complex, i_or_s_from_lf: complex) -> np.ndarray:
        'Initialize from total injected S=P+jQ in MVA, not from current.'
        from .machine_init import initialize_machine
        return initialize_machine(self, v_bus, complex(i_or_s_from_lf)).state

    def events(self) -> tuple[()]:
        return ()
