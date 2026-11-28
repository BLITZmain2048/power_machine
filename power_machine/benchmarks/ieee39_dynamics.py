"""RMS clásico IEEE39, sin catálogo REF ni reguladores.

Reutiliza DynamicNetwork para las estampas y cargas Z del motor existente.
El integrador propio RK4 resuelve 20 estados sobre una reducción de Kron.
Base 100 MVA y 60 Hz. Pm y Pe positivas inyectadas, carga positiva consumida.
H_system_s = energía cinética nominal [MW s] / S_base [MVA]. Xd prima
está en p.u. de sistema. d(delta)/dt = 2*pi*f_nom*(omega_pu-1),
d(omega_pu)/dt = (Pm_pu-Pe_pu)/(2*H_system_s), sin amortiguamiento.
No hay tensión de excitación, AVR, governor ni datos aceptados contra REF.
"""

from __future__ import annotations
from power_machine.resources import required_readonly_path

import copy
import csv
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np

from .ieee39 import DEFAULT_MODEL, WORKSPACE, run_loadflow
from ..dynamics.network import DynamicNetwork


DEFAULT_DATA = required_readonly_path("cases", "ieee39", "dynamic_data.json")
EVENTS = ("BASE", "E39-DIN-01", "E39-DIN-02", "E39-DIN-03")
STATUS = "no_verificados_contra_REF"


def _inside(path: str | Path) -> Path:
    """Resuelve destinos dentro de la raíz, incluidos enlaces simbólicos."""
    resolved = Path(path).resolve()
    if not resolved.is_relative_to(WORKSPACE.resolve()):
        raise ValueError("El destino debe permanecer dentro de la carpeta de trabajo")
    return resolved


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def rk4_step(rhs, state: np.ndarray, step_s: float) -> np.ndarray:
    """RK4 autónomo, sin cruzar eventos. Estados en rad y p.u., paso en s."""
    if not math.isfinite(step_s) or step_s <= 0:
        raise ValueError("Paso de integración no positivo o no finito")
    k1 = rhs(state)
    k2 = rhs(state + step_s * k1 / 2)
    k3 = rhs(state + step_s * k2 / 2)
    k4 = rhs(state + step_s * k3)
    return state + step_s * (k1 + 2*k2 + 2*k3 + k4) / 6


@dataclass
class Sample:
    """Muestra a cada paso y a ambos lados de discontinuidades."""

    time_s: float
    side: str
    state: np.ndarray
    voltage_pu: np.ndarray
    generator_power_pu: np.ndarray
    bus_frequency_hz: np.ndarray
    active: np.ndarray
    coi_frequency_hz: float


@dataclass
class Run:
    samples: list[Sample]
    report: dict


class ClassicalSystem:
    """Red Z y máquinas clásicas. No modifica el modelo ni los datos recibidos."""

    def __init__(self, model: dict, dynamic_data: dict):
        self.model = copy.deepcopy(model)
        self.data = copy.deepcopy(dynamic_data)
        if self.data.get("schema") != "ieee39-classical-dynamics-v1":
            raise ValueError("Esquema de datos dinámicos no soportado")
        if self.data.get("datos_dinamicos") != STATUS:
            raise ValueError("Falta etiqueta de datos no verificados contra REF")
        self.s_base_mva = float(model["meta"]["s_base_mva"])
        self.f_nom_hz = float(model["meta"]["f_nom_hz"])
        if (self.s_base_mva != 100 or self.f_nom_hz != 60
                or self.data["s_base_mva"] != self.s_base_mva
                or self.data["f_nom_hz"] != self.f_nom_hz):
            raise ValueError("IEEE39 clásico requiere bases explícitas 100 MVA y 60 Hz")
        self.generators = sorted(self.model["gens"], key=lambda g: g["id"])
        self.gen_ids = [g["id"] for g in self.generators]
        if self.gen_ids != [f"G{n}" for n in range(30, 40)]:
            raise ValueError("Se requieren las diez máquinas G30 a G39")
        rows = self.data["machines"]
        by_id = {r["object_id"]: r for r in rows}
        if len(rows) != 10 or set(by_id) != set(self.gen_ids):
            raise ValueError("Inventario dinámico ausente, duplicado o incompatible")
        for g in self.generators:
            r = by_id[g["id"]]
            if r["bus_id"] != g["bus"] or r["relative_path"] != g["relative_path"]:
                raise ValueError(f"Trazabilidad incompatible para {g['id']}")
            for key in ("h_system_s", "xd_prime_system_pu", "d_system_pu"):
                if r.get(key) is None or not math.isfinite(float(r[key])):
                    raise ValueError(f"Parámetro ausente o no finito: {g['id']} {key}")
                if r.get("provenance", {}).get(key) not in self.data["sources"]:
                    raise ValueError(f"Procedencia ausente: {g['id']} {key}")
            if r["h_system_s"] <= 0 or r["xd_prime_system_pu"] <= 0 or r["d_system_pu"] != 0:
                raise ValueError("H y Xd prima deben ser positivos, D debe ser cero")
        self.h_system_s = np.array([by_id[g]["h_system_s"] for g in self.gen_ids])
        self.xd_prime_system_pu = np.array([by_id[g]["xd_prime_system_pu"] for g in self.gen_ids])
        lf = run_loadflow(self.model)
        if not lf["converged"]:
            raise ValueError("El LF IEEE39 no converge")
        self.lf_report = {key: lf[key] for key in ("iterations", "max_mismatch_mva", "converged")}
        self.lf_voltages = {b["id"]: b["u_pu"]*np.exp(1j*math.radians(b["angle_deg"]))
                            for b in lf["buses"]}
        lf_power = {g["id"]: complex(g["p_mw"], g["q_mvar"])/self.s_base_mva for g in lf["gens"]}
        self.network = DynamicNetwork(self.model, self.lf_voltages)
        self.gen_bus_indexes = np.array([self.network.index_of_bus(g["bus"]) for g in self.generators])
        v_gen = np.array([self.lf_voltages[g["bus"]] for g in self.generators])
        s_gen = np.array([lf_power[g] for g in self.gen_ids])
        i_gen = np.conj(s_gen / v_gen)
        emf = v_gen + 1j*self.xd_prime_system_pu*i_gen
        self.emf_magnitude_pu = np.abs(emf)
        self.pm_system_pu = s_gen.real.copy()
        self.initial_state = np.vstack((np.angle(emf), np.ones(10)))
        self.active = np.ones(10, dtype=bool)
        self.fault_bus = None
        self.rebuild()
        v, _ = self.electrical(self.initial_state)
        lf_v = self.network.voltage_vector(self.lf_voltages)
        self.initial_voltage_error_pu = float(np.max(np.abs(v-lf_v)))
        self.initial_derivative_max = float(np.max(np.abs(self.rhs(self.initial_state))))
        if self.initial_voltage_error_pu > 1e-7 or self.initial_derivative_max > 1e-6:
            raise ValueError("La inicialización no conserva el LF o el equilibrio clásico")

    @classmethod
    def from_files(cls, model_path=DEFAULT_MODEL, data_path=DEFAULT_DATA):
        return cls(json.loads(Path(model_path).read_text(encoding="utf-8")),
                   json.loads(Path(data_path).read_text(encoding="utf-8")))

    def rebuild(self) -> None:
        """Kron hacia FEM internas y mapa de recuperación de tensiones.

        Yred = diag(ygen) - C.T @ solve(Ybus+diag(ygen), C).
        Una falla sólida elimina la fila y columna de V=0 exactamente.
        Una máquina retirada no conserva ni fuente ni admitancia Norton.
        """
        y = self.active / (1j*self.xd_prime_system_pu)
        n = len(self.network.node_ids)
        coupling = np.zeros((n, 10), complex)
        coupling[self.gen_bus_indexes, np.arange(10)] = y
        algebraic = self.network.ybus.copy()
        algebraic[self.gen_bus_indexes, self.gen_bus_indexes] += y
        retained = np.ones(n, bool)
        if self.fault_bus is not None:
            retained[self.network.index_of_bus(self.fault_bus)] = False
        self.voltage_map = np.zeros_like(coupling)
        self.voltage_map[retained] = np.linalg.solve(algebraic[np.ix_(retained, retained)], coupling[retained])
        self.reduced_y = np.diag(y) - coupling.T @ self.voltage_map
        self.y_generator = y

    def electrical(self, state: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Devuelve fasores de barra y P+jQ en bornes, en p.u. de sistema."""
        emf = self.emf_magnitude_pu * np.exp(1j*state[0])
        voltage = self.voltage_map @ emf
        current = self.reduced_y @ emf
        return voltage, voltage[self.gen_bus_indexes] * np.conj(current)

    def rhs(self, state: np.ndarray) -> np.ndarray:
        """Derivadas de delta [rad/s] y omega [p.u./s], Pm fija, D=0."""
        emf = self.emf_magnitude_pu * np.exp(1j*state[0])
        pe_pu = (emf * np.conj(self.reduced_y @ emf)).real
        dx = np.vstack((2*math.pi*self.f_nom_hz*(state[1]-1),
                        (self.pm_system_pu-pe_pu)/(2*self.h_system_s)))
        dx[:, ~self.active] = 0
        return dx

    def coi_frequency(self, state: np.ndarray) -> float:
        """Frecuencia del centro de inercia de máquinas conectadas [Hz]."""
        h = self.h_system_s[self.active]
        return float(self.f_nom_hz * np.dot(h, state[1, self.active]) / h.sum())

    def sample(self, time_s: float, state: np.ndarray, side="regular") -> Sample:
        voltage, power = self.electrical(state)
        emf = self.emf_magnitude_pu * np.exp(1j*state[0])
        dv = self.voltage_map @ (1j*emf*self.rhs(state)[0])
        frequency = np.full(len(voltage), np.nan)
        usable = np.abs(voltage) > 1e-9
        frequency[usable] = self.f_nom_hz + np.imag(dv[usable]/voltage[usable])/(2*math.pi)
        # La derivada de fase no existe en el salto de un fasor.
        if side in ("pre_event", "post_event"):
            frequency[:] = np.nan
        return Sample(time_s, side, state.copy(), voltage, power, frequency,
                      self.active.copy(), self.coi_frequency(state))

    def apply(self, kind: str) -> None:
        """Eventos propios del estudio, conservando los estados continuos."""
        if kind == "fault":
            self.fault_bus = "B16"
        elif kind == "clear":
            self.fault_bus = None
            self.network.set_branch_open("L16-17")
        elif kind == "trip":
            self.active[self.gen_ids.index("G32")] = False
        elif kind == "load":
            load = next(l for l in self.network.model["loads"] if l["id"] == "LD04")
            # Modifica solo P nominal de la admitancia, Q nominal permanece.
            load["p_mw"] *= 1.1
            load["params"]["plini"] = load["p_mw"]
            self.network.rebuild()
        else:
            raise ValueError(f"Evento clásico desconocido: {kind}")
        self.rebuild()

    def power_balance(self, state: np.ndarray) -> dict:
        """Balance independiente con red completa y cargas Z [MW]."""
        voltage, power = self.electrical(state)
        demand_mw = 0.0
        for load in self.network.model["loads"]:
            bid = load["bus"]
            demand_mw += load["p_mw"]*load.get("scale", 1)*abs(voltage[self.network.index_of_bus(bid)]/self.lf_voltages[bid])**2
        passive_mw = float(np.vdot(voltage, self.network.ybus @ voltage).real*self.s_base_mva)
        pm_mw = float(self.pm_system_pu[self.active].sum()*self.s_base_mva)
        return {"pm_connected_mw": pm_mw, "load_actual_mw": float(demand_mw),
                "network_loss_mw": passive_mw-demand_mw,
                "imbalance_mw": pm_mw-passive_mw,
                "electrical_balance_error_mw": float(power.real.sum()*self.s_base_mva-passive_mw)}


def simulate(system: ClassicalSystem, event="BASE", *, end_s=10.0, step_s=0.001,
             clearing_s=0.1, stop_on_instability=False) -> Run:
    """Corrida nueva, aterriza eventos, almacena ambos lados del salto.

    Estabilidad informativa en horizonte finito: ninguna separación angular
    entre máquinas conectadas alcanza 360 grados. No implica estabilidad
    asintótica. No altera parámetros iniciales ni reutiliza red perturbada.
    """
    if event not in EVENTS:
        raise ValueError("Ensayo desconocido")
    if not all(math.isfinite(v) and v > 0 for v in (end_s, step_s, clearing_s)):
        raise ValueError("Tiempos y paso deben ser finitos y positivos")
    if not system.active.all() or system.fault_bus or system.network.open_branches:
        raise ValueError("Cree un ClassicalSystem nuevo para cada corrida")
    if next(l for l in system.network.model["loads"] if l["id"] == "LD04")["p_mw"] != next(l for l in system.model["loads"] if l["id"] == "LD04")["p_mw"]:
        raise ValueError("Cree un ClassicalSystem nuevo para cada corrida")
    schedule = {"BASE": [], "E39-DIN-01": [(1.0, "fault"), (1.0+clearing_s, "clear")],
                "E39-DIN-02": [(1.0, "trip")], "E39-DIN-03": [(1.0, "load")]}[event]
    state = system.initial_state.copy()
    samples = [system.sample(0.0, state)]
    t = 0.0
    position = 0
    max_spread_rad = float(np.ptp(state[0]))
    max_derivative = system.initial_derivative_max
    instability_time = None
    startup = None
    while t < end_s-1e-12:
        target = min(end_s, (math.floor((t+1e-10)/step_s)+1)*step_s)
        if position < len(schedule):
            target = min(target, schedule[position][0])
        h = target-t
        if h > 1e-12:
            state = rk4_step(system.rhs, state, h)
            t = target
            if not np.isfinite(state).all():
                raise FloatingPointError("Estado no finito durante integración")
        is_event = position < len(schedule) and abs(t-schedule[position][0]) < 1e-12
        samples.append(system.sample(t, state, "pre_event" if is_event else "regular"))
        if is_event:
            kind = schedule[position][1]
            system.apply(kind)
            position += 1
            samples.append(system.sample(t, state, "post_event"))
            if kind in ("trip", "load"):
                balance = system.power_balance(state)
                expected = system.f_nom_hz*(balance["imbalance_mw"]/system.s_base_mva)/(2*system.h_system_s[system.active].sum())
                observed = (system.coi_frequency(rk4_step(system.rhs, state, min(step_s, 0.001)))-system.coi_frequency(state))/min(step_s, 0.001)
                rel_error = abs(observed-expected)/abs(expected) if expected else 0.0
                nominal_delta_mw = -650.0 if kind == "trip" else -0.1*next(l["p_mw"] for l in system.model["loads"] if l["id"] == "LD04")
                startup = {**balance, "inertia_connected_system_s": float(system.h_system_s[system.active].sum()),
                           "expected_rocof_hz_s": float(expected), "observed_first_ms_rocof_hz_s": float(observed),
                           "relative_error": float(rel_error), "within_5_percent": bool(rel_error <= 0.05),
                           "nominal_delta_p_mw": nominal_delta_mw,
                           "nominal_fixed_load_rocof_hz_s": float(system.f_nom_hz*nominal_delta_mw/system.s_base_mva/(2*system.h_system_s[system.active].sum())),
                           "note": "El balance usa demanda Z y pérdidas posevento. No se supone que el déficit sea igual al escalón nominal con tensión fija."}
        max_derivative = max(max_derivative, float(np.max(np.abs(system.rhs(state)))))
        spread = float(np.ptp(state[0, system.active]))
        max_spread_rad = max(max_spread_rad, spread)
        if spread >= 2*math.pi and instability_time is None:
            instability_time = t
            if stop_on_instability:
                break
    base_state_error = float(max(np.max(np.abs(s.state-system.initial_state)) for s in samples))
    initial_v = samples[0].voltage_pu
    base_voltage_error = float(max(np.max(np.abs(s.voltage_pu-initial_v)) for s in samples))
    report = {"schema": "ieee39-classical-rms-run-v1", "event": event,
              "datos_dinamicos": STATUS, "ref_comparison": "sin_comparacion_con_referencia_externa",
              "method": "RK4_propio_Kron_estampas_DynamicNetwork", "states": 20,
              "s_base_mva": system.s_base_mva, "f_nom_hz": system.f_nom_hz,
              "step_s": step_s, "requested_end_s": end_s, "completed_end_s": t,
              "clearing_duration_s": clearing_s if event == "E39-DIN-01" else None,
              "load_model": "admitancia_constante_referida_al_LF",
              "lf": system.lf_report, "initial_derivative_max": system.initial_derivative_max,
              "initial_voltage_error_pu": system.initial_voltage_error_pu,
              "derivative_max_over_run": max_derivative,
              "base_max_state_change": base_state_error if event == "BASE" else None,
              "base_max_voltage_phasor_change_pu": base_voltage_error if event == "BASE" else None,
              "base_equilibrium_pass": bool(max_derivative < 1e-6 and base_state_error < 1e-6 and base_voltage_error < 1e-6) if event == "BASE" else None,
              "startup_swing_check": startup,
              "stable_finite_horizon": instability_time is None,
              "instability_time_s": instability_time, "max_pairwise_rotor_spread_deg": math.degrees(max_spread_rad),
              "stability_definition": "Separación angular sin envolver menor que 360 grados durante el horizonte, informativa, no asintótica",
              "OPENBLAS_NUM_THREADS": os.environ.get("OPENBLAS_NUM_THREADS"),
              "numpy_version": np.__version__,
              "signal_notes": ["Excitación no representada, campo CSV vacío. FEM interna adicional en p.u. de barra.",
                               "G32 desconectado tiene P=Q=0 y estados congelados solo como memoria, velocidad y ángulo exportados vacíos.",
                               "Frecuencia de barra por derivada analítica de fase, vacía en saltos y tensión nula, no es m:fehz nativa REF.",
                               "Q nominal de LD04 se conserva, Q efectiva varía con el cuadrado de la tensión."]}
    return Run(samples, report)


def critical_clearing_time(model: dict, dynamic_data: dict, *, lower_s=0.05,
                           upper_s=0.5, tolerance_s=0.001, end_s=10.0,
                           step_s=0.001) -> dict:
    """Bisección informativa, requiere extremo estable y extremo inestable.

    Asume transición monótona dentro del intervalo, no demuestra unicidad
    ni estabilidad fuera de los diez segundos. Cada ensayo reinicializa LF.
    """
    if not (0 < lower_s < upper_s < end_s-1 and tolerance_s > 0
            and all(math.isfinite(v) for v in (lower_s, upper_s, tolerance_s, end_s))):
        raise ValueError("Intervalo de despeje inválido")
    trials = []

    def stable(duration):
        run = simulate(ClassicalSystem(model, dynamic_data), "E39-DIN-01", end_s=end_s,
                       step_s=step_s, clearing_s=duration, stop_on_instability=True)
        value = run.report["stable_finite_horizon"]
        trials.append({"clearing_s": duration, "stable_finite_horizon": value,
                       "max_pairwise_rotor_spread_deg": run.report["max_pairwise_rotor_spread_deg"]})
        return value

    low_stable = stable(lower_s)
    high_stable = stable(upper_s)
    if not low_stable or high_stable:
        return {"status": "sin_intervalo_acotado", "estimate_s": None, "trials": trials,
                "note": "Se requieren límite inferior estable y superior inestable"}
    while upper_s-lower_s > tolerance_s:
        mid = (lower_s+upper_s)/2
        if stable(mid):
            lower_s = mid
        else:
            upper_s = mid
    return {"status": "informativo", "stable_lower_s": lower_s, "unstable_upper_s": upper_s,
            "estimate_s": (lower_s+upper_s)/2, "tolerance_s": tolerance_s,
            "end_s": end_s, "step_s": step_s, "trials": trials,
            "note": "Umbral de separación de 360 grados, horizonte finito y monotonía supuesta, datos no verificados contra REF"}


def export_run(run: Run, system: ClassicalSystem, output: str | Path, *,
               model_path=DEFAULT_MODEL, data_path=DEFAULT_DATA,
               output_step_s=0.01) -> dict:
    """CSV a 10 ms y ambos lados de eventos, precisión 17 cifras.

    Vacío representa dato no definido, nunca cero implícito. Se preservan
    object_id y relative_path en el manifiesto de columnas del reporte.
    """
    if not math.isfinite(output_step_s) or output_step_s <= 0:
        raise ValueError("Paso de salida no positivo o no finito")
    model_path, data_path = Path(model_path), Path(data_path)
    # Evita atribuir un hash de disco a otros parámetros en memoria.
    if json.loads(model_path.read_text(encoding="utf-8")) != system.model:
        raise ValueError("El modelo de disco no corresponde a la corrida")
    if json.loads(data_path.read_text(encoding="utf-8")) != system.data:
        raise ValueError("Los datos dinámicos de disco no corresponden a la corrida")
    output = _inside(output)
    output.mkdir(parents=True, exist_ok=True)
    columns = ["time_s", "sample_side", "coi_frequency_hz"]
    manifest = {}
    buses = {b["id"]: b for b in system.model["buses"]}
    gens = {g["id"]: g for g in system.generators}

    def add(entity, variable, unit, note=""):
        key = entity["id"]+"_"+variable
        columns.append(key)
        manifest[key] = {"object_id": entity["object_id"], "relative_path": entity["relative_path"],
                         "unit": unit, "note": note}

    for bid in ("B04", "B16", "B20", "B39"):
        add(buses[bid], "u_pu", "p.u.")
        add(buses[bid], "angle_deg", "deg", "Ángulo de fasor en marco síncrono de 60 Hz")
    for bid in ("B04", "B16"):
        add(buses[bid], "frequency_hz", "Hz", "Derivada analítica de fase, no estimador nativo REF")
    for gid in ("G30", "G32", "G39"):
        for variable, unit in (("in_service", "1"), ("speed_pu", "p.u."), ("rotor_angle_deg", "deg"),
                               ("p_mw", "MW"), ("q_mvar", "Mvar"), ("excitation_voltage_pu", "p.u."), ("internal_emf_pu", "p.u.")):
            add(gens[gid], variable, unit, "No representada por el modelo clásico" if variable == "excitation_voltage_pu" else "")

    def number(value):
        if value is None or not np.isfinite(value):
            return ""
        return "0" if value == 0 else format(float(value), ".17g")

    selected = [s for s in run.samples if s.side != "regular" or
                abs(s.time_s/output_step_s-round(s.time_s/output_step_s)) < 1e-8 or
                abs(s.time_s-run.samples[-1].time_s) < 1e-12]
    with (output/"signals.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for s in selected:
            row = [number(s.time_s), s.side, number(s.coi_frequency_hz)]
            for bid in ("B04", "B16", "B20", "B39"):
                v = s.voltage_pu[system.network.index_of_bus(bid)]
                row += [number(abs(v)), number(np.angle(v, deg=True)) if abs(v) > 1e-9 else ""]
            for bid in ("B04", "B16"):
                row.append(number(s.bus_frequency_hz[system.network.index_of_bus(bid)]))
            for gid in ("G30", "G32", "G39"):
                i = system.gen_ids.index(gid)
                row += [str(int(s.active[i])), number(s.state[1, i]) if s.active[i] else "",
                        number(math.degrees(s.state[0, i])) if s.active[i] else "",
                        number(s.generator_power_pu[i].real*system.s_base_mva),
                        number(s.generator_power_pu[i].imag*system.s_base_mva), "",
                        number(system.emf_magnitude_pu[i]) if s.active[i] else ""]
            writer.writerow(row)
    report = copy.deepcopy(run.report)
    report.update(model_sha256=_sha256(model_path), dynamic_data_sha256=_sha256(data_path),
                  implementation_sha256=_sha256(Path(__file__)), output_step_s=output_step_s,
                  output_samples=len(selected), integration_samples=len(run.samples), columns=manifest,
                  signals_sha256=_sha256(output/"signals.csv"))
    (output/"run_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)+"\n", encoding="utf-8")
    return report
