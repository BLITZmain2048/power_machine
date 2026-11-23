'Ieee39 native support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import numpy as np

from .native_registry import NativeControllerCatalog, LOAD_MODELS
from .solver import DAESolver, DAEState, SolverConvergenceError


class NativeControlledMachine:
    'Machine with AVR and governor using explicit IEEE wiring Efd to ve and pm to pt. Vt=abs(Vbus)*Ubus/Ugn. Governor droop uses absolute speed and wref=1. The angular reference only enters angle derivatives.'

    n_alg = 0

    def __init__(self, machine, avr=None, governor=None):
        self.machine, self.avr, self.governor = machine, avr, governor
        self.object_id = machine.object_id
        self.relative_path = machine.relative_path
        self.bus_id = machine.bus_id
        self.n_states = machine.n_states + (avr.n_states if avr else 0) + (
            governor.n_states if governor else 0)
        self.discrete_state = {}
        self.avr_inputs, self.gov_inputs = {}, {}
        self.detached = False
        self.frozen_limits = None
        self.cache_controls = True
        self._control_cache = None

    def bounds(self):
        'Return local indices and original bounds, in p.u. except G3 gate rate in p.u./s.'
        rows = []
        m = self.machine.n_states
        if self.avr:
            p = self.avr.parameters
            rows.append((m+1, p["Vrmin"], p["Vrmax"]))
            m += self.avr.n_states
        if self.governor:
            p = self.governor.parameters
            rows.append((m+self.governor.gate_index, p["Pmin"], p["Pmax"]))
            if self.governor.n_states == 4:
                rows.append((m+2, p["Uc"], p["Uo"]))
        return rows

    def raw_limit_rates(self, x, v):
        'Return interior IEEE limit-state derivatives. Keep the original bounds and ramps. Freeze only the selected non-windup branch during Newton.'
        xm, xa, xg = self.parts(x)
        rates = {}
        m = self.machine.n_states
        if self.avr:
            p = self.avr.parameters
            measured = xa[0] if p["Tr"] > 0 else abs(v)*self.machine.bus_un_kv/self.machine.ugn_kv
            u = self.avr_inputs
            vf = p["Kf"]/p["Tf"]*(xa[2]-xa[3])
            rates[m+1] = (p["Ka"]*(u["Vref"]+u["dVref"]+u["Vs"]-measured-vf)-xa[1])/p["Ta"]
            m += 4
        if self.governor:
            p = self.governor.parameters
            u = self.gov_inputs
            dw = xm[-2]-u["wref"]
            if self.governor.n_states == 6:
                ratio = p["T2"]/p["T1"]
                lead = p["K"]*(ratio*dw+(1-ratio)*xg[0])
                rates[m+1] = np.clip((u["Pref"]-lead-xg[1])/p["T3"], p["Uc"], p["Uo"])
            else:
                gate = np.clip(xg[0], p["Pmin"], p["Pmax"])
                error = u["Pref"]-dw-p["Sigma"]*gate-p["Delta"]*(gate-xg[3])
                rates[m] = np.clip(xg[2], p["Uc"], p["Uo"])
                rates[m+2] = (error/p["Tg"]-xg[2])/p["Tp"]
        return rates

    def active_limit_signature(self):
        return tuple(sorted((self.frozen_limits or {}).items()))

    def terminal_voltage(self, x, v):
        'After terminal opening solve I=0 for internal terminal voltage without network injection. Preserve rotor speed and exciter dynamics. Open-terminal angle publication follows the documented benchmark convention.'
        if not self.detached:
            return v
        xm = self.parts(x)[0]
        currents = [self.machine.current_injection(xm, voltage) for voltage in (0j, 1+0j, 1j)]
        i0, ir, ii = currents
        matrix = np.array([[(ir-i0).real, (ii-i0).real],
                           [(ir-i0).imag, (ii-i0).imag]])
        result = np.linalg.solve(matrix, -np.array([i0.real, i0.imag]))
        return complex(*result)

    def parts(self, x):
        m = self.machine.n_states
        a = self.avr.n_states if self.avr else 0
        return x[:m], x[m:m+a], x[m+a:]

    def set_reference_frequency(self, speed_pu):
        self.machine.fref = float(speed_pu)

    def init(self, v, power_mva):
        xm = self.machine.init(v, power_mva)
        xa, xg = np.empty(0), np.empty(0)
        if self.avr:
            xa, self.avr_inputs = self.avr.initialize({
                "Efd": self.machine.ve,
                "Vt": abs(v)*self.machine.bus_un_kv/self.machine.ugn_kv})
        if self.governor:
            xg, self.gov_inputs = self.governor.initialize({"pm": self.machine.pt, "dw": 0.})
        return np.r_[xm, xa, xg]

    def controls(self, t, x, v):
        """Evaluate native controls without committing memory.

        Cache only identical control states, inputs, parameters and voltage
        bases. Return independent derivative arrays and restore field voltage
        and mechanical power only for controllers that are present.
        """
        xm, xa, xg = self.parts(x)
        key = None
        if self.cache_controls:
            key = (float(t), xa.tobytes(), xg.tobytes(), float(xm[-2]), complex(v),
                   id(self.avr), id(self.governor),
                   self.machine.bus_un_kv, self.machine.ugn_kv,
                   tuple(self.avr_inputs.items()), tuple(self.gov_inputs.items()),
                   tuple(self.avr.parameters.items()) if self.avr else (),
                   tuple(self.governor.parameters.items()) if self.governor else (),
                   getattr(self.avr, "profile", None), getattr(self.avr, "frozen_measurement", None))
            if self._control_cache is not None and self._control_cache[0] == key:
                _, ve, pt, da, dg = self._control_cache
                if self.avr is not None:self.machine.ve = ve
                if self.governor is not None:self.machine.pt = pt
                return da.copy(), dg.copy()
        da, dg = np.empty(0), np.empty(0)
        if self.avr:
            inputs = {**self.avr_inputs,
                      "Vt": abs(v)*self.machine.bus_un_kv/self.machine.ugn_kv}
            out, da = self.avr.evaluate(t, xa, inputs)
            self.machine.ve = out["Efd"]
        if self.governor:
            out, dg = self.governor.evaluate(t, xg, {**self.gov_inputs, "w": float(xm[-2])})
            self.machine.pt = out["pm"]
        if key is not None:
            self._control_cache = (key, self.machine.ve, self.machine.pt, da.copy(), dg.copy())
        return da, dg

    def f(self, t, x, z, v):
        v = self.terminal_voltage(x, v)
        da, dg = self.controls(t, x, v)
        dm = self.machine.f(t, self.parts(x)[0], z, v)
        if self.detached:
            dm[-1] = 0.
        dx = np.concatenate((dm, da, dg))
        if self.frozen_limits is not None:
            rates = self.raw_limit_rates(x, v)
            for index, blocked in self.frozen_limits.items():
                dx[index] = 0. if blocked else rates[index]
        return dx

    def g(self, t, x, z, v):
        return np.empty(0)

    def current_injection(self, x, v):
        if self.detached:
            return 0j
        return self.machine.current_injection(self.parts(x)[0], v)

    def outputs(self, x, v):
        v = self.terminal_voltage(x, v)
        self.controls(0., x, v)
        return {**self.machine.outputs(self.parts(x)[0], v), "ve": self.machine.ve,
                "pt": self.machine.pt}

    def project_state(self, x):
        xm, xa, xg = self.parts(x)
        return np.r_[xm, self.avr.project_state(xa) if self.avr else xa,
                     self.governor.project_state(xg) if self.governor else xg]

    def jacobian_local(self, t, x, z, v, *, rel_step=1e-6):
        'Return local central differences of [f, Re(I), Im(I)] with coupled controls. Restore ve and pt at the base point after perturbing. Do not commit memory.'
        point = np.empty(self.n_states+2)
        point[:-2], point[-2], point[-1] = x, v.real, v.imag
        jac = np.empty((self.n_states+2, self.n_states+2))
        pair = np.empty((2, self.n_states+2))
        for j in range(len(point)):
            h = rel_step*max(1., abs(point[j]))
            for side, sign in enumerate((1., -1.)):
                p = point.copy()
                p[j] += sign*h
                voltage = complex(p[-2], p[-1])
                dx = self.f(t, p[:-2], z, voltage)
                current = self.current_injection(p[:-2], voltage)
                pair[side, :-2] = dx
                pair[side, -2:] = current.real, current.imag
            jac[:, j] = (pair[0]-pair[1])/(2*h)
        self.controls(t, x, v)
        return jac


class NativeIEEESolver(DAESolver):
    'IEEE solver extension with exact V=0 bolted faults and accepted-step limits. Replace only the fault-node KCL rows. Do not add an artificial small impedance.'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.internal_events_enabled = False
        self.bolted_bus_ids = set()
        self.projections = []
        self.limit_events = []

    def _evaluate(self, t, x, z, v):
        f, g, net = super()._evaluate(t, x, z, v)
        nv = len(v)
        for bid in self.bolted_bus_ids:
            i = self.network.index_of_bus(bid)
            net[i], net[nv+i] = v[i].real, v[i].imag
        return f, g, net

    def _local_jacobian(self, t, x, z, v, **kwargs):
        jac = super()._local_jacobian(t, x, z, v, **kwargs)
        offset = self.nx if kwargs.get("old") is not None else 0
        start = offset+self.nz
        nv = len(v)
        for bid in self.bolted_bus_ids:
            i = self.network.index_of_bus(bid)
            for index in (start+i, start+nv+i):
                jac[index, :] = 0.
                jac[index, index] = 1.
        return jac

    def step(self, state, step_s=None):
        'Hold non-windup branches and locate the first physical limit crossing. Time secants target a 1e-9 p.u. surface residual. Recheck release on accepted steps.'
        if self.retry_steps and not self._retry_active:
            return self._retry_step(state, step_s, self._native_step_once)
        return self._native_step_once(state, step_s)

    def _native_step_once(self, state, step_s=None):
        """Congela no-windup y aterriza antes de sobrepasar una cota física.

        Localización por secante temporal, residuo de superficie 1e-9 pu.
        Modos bloqueados se revisan en cada paso, liberación hasta un paso
        nominal después de cambiar el signo. No usa el solver del editor DSL.
        """
        h = self.step_s if step_s is None else float(step_s)
        surfaces = []
        for dev, xs, bi in zip(self.devices, self.x_slices, self._bus_indexes):
            if not hasattr(dev, "bounds"):
                continue
            local = state.x[xs]
            v = dev.terminal_voltage(local, state.v[bi])
            rates = dev.raw_limit_rates(local, v)
            dev.frozen_limits = {}
            for index, low, high in dev.bounds():
                rate = rates[index]
                blocked = ((local[index] >= high-1e-10 and rate > 0.) or
                           (local[index] <= low+1e-10 and rate < 0.))
                dev.frozen_limits[index] = bool(blocked)
                surfaces.append((dev, xs.start+index, low, high, blocked))
        restart = self.restart_pending
        solved = None
        try:
            for iteration in range(40):
                # Cada ensayo parte del mismo extremo izquierdo, sin predictor
                # ni derivada de otro modo de limitador.
                self.restart_pending = restart
                self._last_state = self._previous_state = self._last_f = None
                solved, metric = super().step(state, h)
                crossings = []
                for dev, index, low, high, blocked in surfaces:
                    if blocked:
                        continue
                    start, end = state.x[index], solved.x[index]
                    if end > high+1e-9 and start < high-1e-10:
                        crossings.append(((high-start)/(end-start), dev, index, high))
                    elif end < low-1e-9 and start > low+1e-10:
                        crossings.append(((low-start)/(end-start), dev, index, low))
                if not crossings:
                    break
                fraction, dev, index, bound = min(crossings, key=lambda r: r[0])
                h *= max(.01, min(.999999, fraction))
                self._step_factor = self._step_key = None
            else:
                raise _coded_error(RuntimeError, 'invalid_ieee39_native_data', 'Limit localization did not converge')
            for dev, index, low, high, blocked in surfaces:
                if not blocked and any(abs(solved.x[index]-bound) <= 1e-9
                                       and abs(state.x[index]-bound) > 1e-9 for bound in (low, high)):
                    nearest = min((low, high), key=lambda bound: abs(solved.x[index]-bound))
                    solved.x[index] = nearest
                    self.limit_events.append({"time_s": solved.t, "object_id": dev.object_id,
                                              "state_index": index, "bound": nearest})
        finally:
            for dev in self.devices:
                if hasattr(dev, "frozen_limits"):
                    dev.frozen_limits = None
            self._last_state = self._previous_state = self._last_f = None
        changed = False
        for dev, xs in zip(self.devices, self.x_slices):
            if self.in_service[dev.object_id] and hasattr(dev, "project_state"):
                projected = dev.project_state(solved.x[xs])
                delta = float(np.max(abs(projected-solved.x[xs]), initial=0.))
                if delta:
                    self.projections.append({"time_s": solved.t, "object_id": dev.object_id,
                                             "max_correction_pu": delta})
                    solved.x[xs] = projected
                    changed = True
        if changed:
            solved, _ = self.solve_algebraic(solved)
            self.restart_discrete()
        return solved, metric


class IEEE39NativeRMS:
    'Assemble 166 states from a consistent load flow and read-only IEEE39 PF data.'

    def __init__(self, pf_dir, *, step_s=.001, stage="full", avr_profile="dyn_a",
                 linear_solver="compatibility", retry_steps=False,
                 min_step_s=1e-6, max_step_retries=12):
        """Read explicit PF case data and configure a native RMS solver.

        Step and minimum step are in seconds. Backend and transactional
        retry options follow DAESolver. The default preserves reference
        arithmetic and the fixed event-aware step schedule.
        """
        from .native_case import NativeDynamicCase, values
        if stage not in ("loads", "avr", "full"):
            raise _coded_error(ValueError, 'invalid_ieee39_native_data', 'Unknown stage')
        self.case = NativeDynamicCase(pf_dir)
        self.model, self.network = self.case.model, self.case.network
        self.catalog = NativeControllerCatalog(self.case.inventory, avr_profile=avr_profile)
        self.avr_profile = avr_profile
        self.devices, self.loads = [], {}
        self.initial_values = {}
        for gen in self.model["gens"]:
            if gen["outserv"]:
                continue
            machine = self.case.machines[gen["id"]]
            comp, avr, gov = self.catalog.bind(gen["path"], machine)
            dev = NativeControlledMachine(machine, avr if stage != "loads" else None,
                                          gov if stage == "full" else None)
            dev.comp = comp
            self.devices.append(dev)
            init = self.case.initializations[gen["id"]]
            # Potencia terminal total desde la inicialización consistente, sin ngnum extra.
            out = machine.outputs(init.state, self.case.voltages[gen["bus"]])
            self.initial_values[gen["id"]] = complex(out["P1_mw"], out["Q1_mvar"])
        for load in self.model["loads"]:
            if load["outserv"]:
                continue
            load["type_params"].update(values(self.case.inventory["types"][load["type_id"]]["fields"]))
            dev = LOAD_MODELS["general_load_t1"].from_model(load, s_base_mva=100.)
            dev.relative_path = load["relative_path"]
            self.loads[load["id"]] = dev
            self.devices.append(dev)
            self.initial_values[load["id"]] = complex(load["p_mw"], load["q_mvar"])
        self.solver = NativeIEEESolver(self.network, self.devices, step_s=step_s,
                                       atol=1e-11, rtol=1e-9, linear_solver=linear_solver,
                                       retry_steps=retry_steps, min_step_s=min_step_s,
                                       max_step_retries=max_step_retries)
        for dev, xs in zip(self.devices, self.solver.x_slices):
            if hasattr(dev, "machine") and dev.machine.elm["ip_ctrl"] == 1:
                self.solver.reference_speed_index = xs.start+dev.machine.n_states-2
        self.initial, _ = self.solver.initialize(self.case.voltages, self.initial_values)
        self.manifest = self.state_manifest()
        f, g, net = self.solver._evaluate(0., self.initial.x, self.initial.z, self.initial.v)
        self.equilibrium = {"state_count": len(f), "max_derivative_per_s": float(max(abs(f))),
                            "max_kcl_pu": float(max(abs(net))),
                            "derivatives": f.tolist(), "lf_projection": self.case.lf_projection}
        if max(self.equilibrium["max_derivative_per_s"], self.equilibrium["max_kcl_pu"]) >= 1e-8:
            raise _coded_error(ValueError, 'invalid_ieee39_native_data', 'Equilibrio cerrado no cumple 1e-8: ' + str(self.equilibrium))
        if stage == "full" and len(f) != 166:
            raise _coded_error(ValueError, 'invalid_ieee39_native_data', 'Expected 166 states')

    def state_manifest(self):
        rows = []
        def append(component, names, kind):
            for name in names:
                rows.append({"index": len(rows), "object_id": component.object_id,
                             "relative_path": component.relative_path, "variable": name,
                             "unit": ("rad" if name == "phi" else
                                      "p.u./s" if name == "gate_speed_pu_per_s" else "p.u."),
                             "kind": kind})
        for dev in self.devices:
            if hasattr(dev, "machine"):
                names = ["psifd", "psi1d", "psi1q"]
                if dev.machine.circuit.turbo:
                    names.append("psi2q")
                append(dev.machine, names+["speed", "phi"], "machine")
                if dev.avr:
                    append(dev.avr, dev.avr.state_names, "avr")
                if dev.governor:
                    append(dev.governor, dev.governor.state_names, "governor")
            else:
                append(dev, ("xu",), "load")
        return rows

    def apply_events(self, trial, time_s, state):
        'Apply simultaneous exported PF events while retaining the pre-event side.'
        from .native_case import values
        by_path = {e["path"]: e for group in ("buses", "branches", "gens", "loads")
                   for e in self.model[group]}
        applied = []
        for event in self.case.inventory["trials"][trial]["events"]:
            a = values(event["fields"])
            if a["outserv"] or abs(a["time"]-time_s) > 1e-10:
                continue
            target = a["p_target"]
            if event["class"] == "EvtShc":
                line = by_path[target]
                if a["shcLocation"] != 0 or a["R_f"] != 0 or a["X_f"] != 0:
                    raise _coded_error(NotImplementedError, 'unsupported_ieee39_native_data', 'Only a bolted fault at the initial terminal is supported')
                if a["i_shc"] == 0:
                    self.solver.bolted_bus_ids.add(line["bus_from"])
                elif a["i_shc"] == 4:
                    self.solver.bolted_bus_ids.discard(line["bus_from"])
                else:
                    raise _coded_error(NotImplementedError, 'unsupported_ieee39_native_data', 'Unknown fault type')
            elif event["class"] == "EvtSwitch":
                if a["i_switch"] != 0:
                    raise _coded_error(NotImplementedError, 'unsupported_ieee39_native_data', 'Closing event was not requested')
                item = by_path.get(target)
                if item and item["class"] == "ElmSym":
                    dev = next(d for d in self.devices if d.object_id == item["id"])
                    if dev.machine.elm["ip_ctrl"] == 1:
                        raise _coded_error(ValueError, 'invalid_ieee39_native_data', 'Cannot open the angular reference')
                    dev.detached = True
                    next(g for g in self.network.model["gens"] if g["id"] == item["id"])["outserv"] = 1
                    self.network.rebuild()
                else:
                    matches = [b for b in self.model["branches"] if target in
                               (b["params"].get("bus1"), b["params"].get("bus2"))]
                    if len(matches) != 1:
                        raise _coded_error(ValueError, 'invalid_ieee39_native_data', 'Cubicle does not identify a unique branch')
                    # Dos aperturas simultáneas, ambos bornes desconectados.
                    self.network.set_branch_open(matches[0]["id"])
            elif event["class"] == "EvtLod":
                if a["iopt_type"] != 0:
                    raise _coded_error(NotImplementedError, 'unsupported_ieee39_native_data', 'Only percentage steps are supported')
                self.loads[by_path[target]["id"]].set_step_percent(
                    dp_percent=a["dP"], dq_percent=a["dQ"])
            else:
                raise _coded_error(NotImplementedError, 'unsupported_ieee39_native_data', event['class'])
            applied.append({"time_s": time_s, "object_id": event["object_id"],
                            "relative_path": event["relative_path"], "class": event["class"]})
        if applied:
            self.solver.refresh_network_indexes()
            self.solver.restart_discrete()
            state, _ = self.solver.solve_algebraic(state)
        return state, applied

    def linearize(self, *, rel_step=1e-6):
        'Return A=fx-fv*solve(gv,gx) for the continuous DAE at constant references. Use the solver Jacobian only to assemble derivatives, not as a discretized RMS step.'
        s = self.initial
        solver = self.solver
        solver.fd_rel_step = rel_step
        f, _, _ = solver._evaluate(0., s.x, s.z, s.v)
        raw = solver._local_jacobian(0., s.x, s.z, s.v, old=s, h=2.,
                                      f_old=f, method="trapezoidal")
        n = solver.nx
        fx, fv = np.eye(n)-raw[:n, :n], -raw[:n, n:]
        gx, gv = raw[n:, :n], raw[n:, n:]
        a = fx-fv@np.linalg.solve(gv, gx)
        solver._evaluate(0., s.x, s.z, s.v)
        return a, {"algebraic_condition": float(np.linalg.cond(gv)),
                   "rel_difference_step": rel_step,
                   "elimination_residual": float(np.max(abs(gv@np.linalg.solve(gv, gx)-gx)))}
