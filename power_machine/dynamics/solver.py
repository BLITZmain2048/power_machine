'Solver support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass
import math

import numpy as np

from power_machine.loadflow import _stamp
from .interfaces import PortMeasurements, complex_to_rect, rect_to_complex
from .linear_system import LinearFactorization, BACKENDS


class SolverConvergenceError(RuntimeError):
    'A Newton step or algebraic-network solve did not converge.'


@dataclass
class DAEState:
    'Time in seconds, real differential states, local algebraic variables and bus-base p.u. voltages.'

    t: float
    x: np.ndarray
    z: np.ndarray
    v: np.ndarray

    def copy(self) -> "DAEState":
        return DAEState(float(self.t), self.x.copy(), self.z.copy(), self.v.copy())


@dataclass(frozen=True)
class StepMetrics:
    time_s: float
    step_s: float
    method: str
    iterations: int
    scaled_residual: float
    damping: float


class PostEventSteps:
    'Post-event intervals h/2, 2h/3, then h for the documented alpha_rms=1 profile.'

    def __init__(self, nominal_s: float):
        self.nominal_s, self.position = float(nominal_s), 2

    def restart(self) -> None:
        self.position = 0

    def next(self) -> float:
        factor = (.5, 2/3)[self.position] if self.position < 2 else 1.
        self.position += 1
        return self.nominal_s*factor


class DAESolver:
    """Solve dx/dt=f and [g, I_device-YV]=0 in rectangular coordinates.

    Newton variables are [x, z, Re(V), Im(V)], using local derivatives and
    analytical Ybus blocks. Scale residuals by atol+rtol*max(1,abs(variable)).
    Currents use network-base p.u. Retain load-flow angles and reference
    dynamics. linear_solver='compatibility' preserves reference rounding.
    'lu' uses optional SciPy LU or NumPy solve, 'scipy' requires SciPy,
    'numpy' never forms an inverse, and 'auto' uses SciPy when installed.
    retry_steps=False preserves the reference step schedule. When enabled,
    reject failed steps transactionally and halve h down to min_step_s,
    at most max_step_retries times. step_retries records stable codes.
    """

    def __init__(self, network, devices, *, step_s: float = 0.001,
                 atol: float = 1e-10, rtol: float = 1e-8, max_iter: int = 20,
                 restart_euler: bool = True, fd_rel_step: float = 1e-6,
                 linear_solver: str = "compatibility", retry_steps: bool = False,
                 min_step_s: float = 1e-6, max_step_retries: int = 12):
        if (any(not math.isfinite(v) or v <= 0 for v in (step_s, atol, rtol, fd_rel_step))
                or max_iter < 1):
            raise _coded_error(ValueError, 'invalid_solver_data', 'Invalid solver parameters')
        if linear_solver not in BACKENDS:
            raise _coded_error(ValueError, 'invalid_linear_backend', '[invalid_linear_backend] Unknown RMS linear backend')
        if (not math.isfinite(min_step_s) or min_step_s <= 0
                or not math.isfinite(max_step_retries) or max_step_retries < 0
                or isinstance(max_step_retries, bool) or int(max_step_retries) != max_step_retries):
            raise _coded_error(ValueError, 'invalid_retry_options', '[invalid_retry_options] Invalid RMS step retry options')
        self.linear_solver = linear_solver
        self.retry_steps = bool(retry_steps)
        self.min_step_s = float(min_step_s)
        self.max_step_retries = int(max_step_retries)
        self.step_retries = []
        self._retry_active = False
        self.network = network
        self.devices = list(devices)
        self.in_service = {device.object_id: True for device in self.devices}
        self.step_s = float(step_s)
        self.atol, self.rtol = float(atol), float(rtol)
        self.max_iter = int(max_iter)
        self.restart_euler = bool(restart_euler)
        self.fd_rel_step = float(fd_rel_step)
        self.restart_pending = False
        self.x_slices = []
        self.z_slices = []
        self._bus_indexes = []
        self._multiports = []
        self._port_indexes = []
        self._injection_indexes = []
        self._branch_stamps = {}
        nx = nz = 0
        ids = set()
        branches = {row["id"]: row for group in ("branches", "branches3")
                    for row in getattr(network, "model", {}).get(group, ())}
        buses = {row["id"]: row for row in getattr(network, "model", {}).get("buses", ())}
        for device in self.devices:
            if device.object_id in ids:
                raise _coded_error(ValueError, 'invalid_solver_data', f'Duplicate device ID: {device.object_id}')
            ids.add(device.object_id)
            multi = callable(getattr(device, "current_injections_multi", None))
            self._multiports.append(multi)
            if multi:
                read_ids = tuple(dict.fromkeys(getattr(device, "read_bus_ids", ())))
                branch_ids = tuple(dict.fromkeys(getattr(device, "read_branch_ids", ())))
                inject_ids = tuple(dict.fromkeys(getattr(device, "injection_bus_ids", ())))
                current_ids = tuple(dict.fromkeys(getattr(device, "read_bus_current_ids", ())))
                if not inject_ids or not read_ids:
                    raise _coded_error(ValueError, 'invalid_solver_data', f'Incomplete bus ports: {device.object_id}')
                for branch_id in branch_ids:
                    if branch_id not in branches:
                        raise _coded_error(ValueError, 'missing_solver_data', f'Missing measured branch: {branch_id}')
                    if branch_id not in self._branch_stamps:
                        branch = branches[branch_id]
                        terminal_ids, _, matrix = _stamp(branch, buses, network.s_base_mva)
                        self._branch_stamps[branch_id] = (
                            tuple(terminal_ids),
                            tuple(self.network.index_of_bus(bid) for bid in terminal_ids),
                            matrix)
                    read_ids = tuple(dict.fromkeys((*read_ids,
                                                    *self._branch_stamps[branch_id][0])))
                dependency = {self.network.index_of_bus(bid) for bid in
                              (*read_ids, *inject_ids)}
                if current_ids:
                    # Una maniobra puede crear nuevas conexiones de Ybus.
                    dependency.update(range(len(self.network.node_ids)))
                self._port_indexes.append(tuple(sorted(dependency)))
                self._injection_indexes.append(tuple(self.network.index_of_bus(bid)
                                                     for bid in inject_ids))
                self._bus_indexes.append(self.network.index_of_bus(read_ids[0]))
            else:
                index = self.network.index_of_bus(device.bus_id)
                self._bus_indexes.append(index)
                self._port_indexes.append((index,))
                self._injection_indexes.append((index,))
            if device.n_states < 0 or device.n_alg < 0:
                raise _coded_error(ValueError, 'invalid_solver_data', 'Device dimensions must be nonnegative')
            self.x_slices.append(slice(nx, nx + device.n_states))
            self.z_slices.append(slice(nz, nz + device.n_alg))
            nx += device.n_states
            nz += device.n_alg
        self.nx, self.nz = nx, nz
        self._step_factor: LinearFactorization | None = None
        self._step_key: tuple | None = None
        self._last_state: DAEState | None = None
        self._last_f: np.ndarray | None = None
        self._previous_state: DAEState | None = None
        self._slow_steps = 0
        self.grouped_multiport_differences = True
        self._difference_groups = {}
        self.reference_speed_index = None
        self._reference_consumers = tuple(d for d in self.devices
                                         if callable(getattr(d, 'set_reference_frequency', None)))
        self.internal_events_enabled = True
        self.internal_events = []
        self._state_surfaces = None
        self._internal_restart = 2
        self._implicit_weight = .5

    def _prepare_state_surfaces(self):
        from .dsl_switching import device_blocks, device_discrete_inputs, inspect_block
        self._state_surfaces = []
        self._expression_surfaces = []
        self.switching_inventory = []
        for dev, xs in zip(self.devices, self.x_slices):
            discrete_inputs = device_discrete_inputs(dev)
            for block, offset in device_blocks(dev):
                inventory, surfaces = inspect_block(block, discrete_inputs.get(block.relative_path, ()))
                self.switching_inventory.extend(inventory)
                for surface in surfaces:
                    self._state_surfaces.append((dev, block, xs.start+offset+surface.state_index, surface))
                for surface in block._expression_surfaces:
                    self._expression_surfaces.append((dev, block, None, surface))
                    block.memory.switch_watch.add(surface.key)
                    block._evaluation_cache = None
                    if hasattr(dev, '_cache'):
                        dev._cache = None

    def _set_switch_modes(self, modes):
        """Invalida cachés solo si cambia una rama, nunca confirma memoria."""
        changed = False
        for dev, block, index, surface in (*self._state_surfaces, *self._expression_surfaces):
            value = modes.get((id(block.memory), surface.key))
            old = block.memory.switch_modes.get(surface.key)
            if value is not None:
                block.memory.switch_modes[surface.key] = value
            else:
                block.memory.switch_modes.pop(surface.key, None)
            if old != value:
                changed = True
                block._evaluation_cache = None
                for child in block._children.values():
                    child._evaluation_cache = None
                if hasattr(dev, '_cache'):
                    dev._cache = None
        if changed:
            self._last_f = None
            if self.linear_solver != "compatibility":
                self._step_factor = self._step_key = None

    def step(self, state: DAEState, step_s: float | None = None) -> tuple[DAEState, StepMetrics]:
        'Advance to an accepted step or the first internal switching surface. Optional retries restore all runtime state before reducing the step.'
        if self.retry_steps and not self._retry_active:
            return self._retry_step(state, step_s, self._step_with_internal_events)
        return self._step_with_internal_events(state, step_s)

    def _retry_step(self, state, step_s, operation):
        from .checkpoint import RuntimeCheckpoint
        h = self.step_s if step_s is None else float(step_s)
        if not math.isfinite(h) or h <= 0:
            raise _coded_error(ValueError, 'invalid_step', '[invalid_step] RMS step must be positive and finite')
        checkpoint = RuntimeCheckpoint(self, state)
        attempts = []
        for attempt in range(self.max_step_retries + 1):
            self._retry_active = True
            try:
                result = operation(state, h)
            except (SolverConvergenceError, FloatingPointError) as exc:
                checkpoint.restore()
                attempts.append({"code": "rms_step_rejected", "time_s": float(state.t),
                                 "attempted_step_s": h, "reason": str(exc)})
                if attempt == self.max_step_retries or h / 2 < self.min_step_s:
                    self.step_retries.extend(attempts)
                    raise _coded_error(SolverConvergenceError, 'rms_retry_exhausted', f'[rms_retry_exhausted] Step retry failed at t={state.t:.12g} s, h={h:.12g} s') from exc
                h /= 2
                self._step_factor = self._step_key = None
            except Exception:
                checkpoint.restore()
                raise
            else:
                self._retry_active = False
                self.step_retries.extend(attempts)
                if attempts:
                    self.step_retries.append({"code": "rms_step_retry_accepted", "time_s": float(result[0].t),
                                              "accepted_step_s": float(result[1].step_s)})
                return result
        raise AssertionError("Unreachable RMS retry state")

    def _step_with_internal_events(self, state: DAEState, step_s: float | None = None) -> tuple[DAEState, StepMetrics]:
        """Congela ramas de estado y aterriza en su primer cruce interno."""
        if not self.internal_events_enabled:
            return self._step_continuous(state, step_s)
        if self._state_surfaces is None:
            self._prepare_state_surfaces()
        h = self.step_s if step_s is None else float(step_s)
        if not self._state_surfaces and not self._expression_surfaces:
            return self._step_continuous(state, h)
        modes = {}
        for dev, block, index, surface in self._state_surfaces:
            if self.in_service[dev.object_id]:
                modes[id(block.memory), surface.key] = surface.mode(float(state.x[index])-surface.level)
        self._set_switch_modes(modes)
        if state is self._last_state and self._last_f is not None:
            f = self._last_f
        else:
            f, _, _ = self._evaluate(state.t, state.x, state.z, state.v)
        for dev, block, index, surface in self._state_surfaces:
            margin = float(state.x[index])-surface.level
            if abs(margin) <= 1e-12 and abs(f[index]) > 1e-12:
                modes[id(block.memory), surface.key] = surface.mode(math.copysign(1., f[index]))
        expression_start = {}
        for dev, block, index, surface in self._expression_surfaces:
            value = block.memory.switch_values.get(surface.key)
            if value is not None and self.in_service[dev.object_id]:
                expression_start[id(block.memory), surface.key] = value[0]
                modes[id(block.memory), surface.key] = value[1]
        self._set_switch_modes(modes)
        weight = (1., .75)[self._internal_restart] if self._internal_restart < 2 else .5
        self._implicit_weight = weight
        # Se repite únicamente integración tentativa. El runner confirma DSL
        # y buffers después de recibir el estado aceptado.
        initial_cache = (self._last_state, self._previous_state, self._last_f,
                         self._step_factor, self._step_key, self._slow_steps, self.restart_pending)
        trial_h = h
        crossings = []
        low_h, high_h = 0., None
        low_values = expression_start.copy()
        for dev, block, index, surface in self._state_surfaces:
            low_values[id(block.memory), surface.key] = float(state.x[index])-surface.level
        high_values = None
        pending_departures = []
        for iteration in range(64):
            (self._last_state, self._previous_state, self._last_f, self._step_factor,
             self._step_key, self._slow_steps, self.restart_pending) = initial_cache
            solved, metric = self._step_continuous(state, trial_h)
            # Leer el extremo convergido, nunca una perturbación de Jacobiana.
            self._evaluate(solved.t, solved.x, solved.z, solved.v)
            crossings = []
            trial_values = {}
            for dev, block, index, surface in self._state_surfaces:
                if not self.in_service[dev.object_id]:
                    continue
                a = float(state.x[index])-surface.level
                b = float(solved.x[index])-surface.level
                trial_values[id(block.memory), surface.key] = b
                if abs(a) <= 1e-12 or a*b > 0 and abs(b) > 1e-12 or a == b:
                    continue
                fraction = a/(a-b)
                if fraction > 0 and fraction <= 1+1e-9:
                    crossings.append((min(1., fraction), dev, block, index, surface, b-a))
            departed = False
            for dev, block, index, surface in self._expression_surfaces:
                token = id(block.memory), surface.key
                if token not in expression_start or surface.key not in block.memory.switch_values:
                    continue
                a = expression_start[token]
                b = block.memory.switch_values[surface.key][0]
                trial_values[token] = b
                if abs(a) <= 1e-12:
                    if abs(b) > 1e-12 and surface.mode(b) != modes[token]:
                        before = modes[token]
                        modes[token] = surface.mode(b)
                        pending_departures.append({**surface.metadata, 'time_s': state.t,
                            'mode_before': before, 'mode_after': modes[token],
                            'localization_iterations': iteration+1, 'departure': True})
                        departed = True
                    continue
                if a*b <= 0 or abs(b) <= 1e-12:
                    fraction = a/(a-b)
                    if fraction > 0 and fraction <= 1+1e-9:
                        crossings.append((min(1., fraction), dev, block, None, surface, b-a))
            if departed:
                self._set_switch_modes(modes)
                initial_cache = (None, None, None, None, None, 0, self.restart_pending)
                low_h, high_h = 0., None
                trial_h = h
                continue
            if not crossings:
                if high_h is None:
                    break
                low_h, low_values = trial_h, trial_values
            else:
                first = min(c[0] for c in crossings)
                if (1-first)*trial_h <= 1e-11 or (
                        high_h is not None and high_h-low_h <= 1e-11):
                    break
                high_h, high_values = trial_h, trial_values
            # Secante dentro de un intervalo conservado. Si se estanca, bisecar.
            estimates = []
            for key, a in low_values.items():
                b = high_values.get(key)
                if b is not None and a*b <= 0 and a != b:
                    estimates.append(low_h+(high_h-low_h)*a/(a-b))
            candidate = min(estimates, default=(low_h+high_h)/2)
            if iteration % 4 == 3 or not low_h < candidate < high_h:
                candidate = (low_h+high_h)/2
            trial_h = candidate
        else:
            raise _coded_error(SolverConvergenceError, 'rms_not_converged', 'DSL crossing localization did not converge')
        if crossings:
            first = min(c[0] for c in crossings)
            for fraction, dev, block, index, surface, direction in crossings:
                if abs(fraction-first)*trial_h > 1e-10:
                    continue
                # El estado se sitúa en la superficie ya localizada, con error
                # temporal <= 1e-11 s. No se recorta un paso que la sobrepasa.
                if index is not None:
                    solved.x[index] = surface.level
                modes[id(block.memory), surface.key] = surface.mode(math.copysign(1., direction))
                self.internal_events.append({**surface.metadata, 'time_s': solved.t,
                    'state_index_global': index, 'mode_before': block.memory.switch_modes[surface.key],
                    'mode_after': modes[id(block.memory), surface.key], 'localization_iterations': iteration+1})
            self._set_switch_modes(modes)
            self.restart_discrete()
            self._internal_restart = 0
        elif pending_departures:
            self.restart_discrete()
            self._internal_restart = 0
        elif self._internal_restart < 2:
            self._internal_restart += 1
        self._implicit_weight = .5
        self.internal_events.extend(pending_departures)
        return solved, metric

    def set_frequency_reference(self, x: np.ndarray) -> None:
        'Set current reference speed, including during tentative Newton iterations.'
        if self.reference_speed_index is not None:
            for device in self._reference_consumers:
                if self.in_service[device.object_id]:
                    device.set_reference_frequency(float(x[self.reference_speed_index]))

    def disconnect_device(self, object_id: str) -> None:
        'Remove injected p.u. current, freeze x and constrain z to zero without shifting other states. Disconnected controls must not commit memory.'
        if object_id not in self.in_service:
            raise _coded_error(ValueError, 'invalid_solver_data', f'Unknown device: {object_id}')
        k = next(i for i, d in enumerate(self.devices) if d.object_id == object_id)
        xs = self.x_slices[k]
        if (self.reference_speed_index is not None
                and xs.start <= self.reference_speed_index < xs.stop):
            raise _coded_error(ValueError, 'invalid_solver_data', 'Cannot disconnect the angular reference machine')
        self.in_service[object_id] = False
        self.restart_discrete()

    def trip_generator(self, object_id: str) -> None:
        'Disconnect a generator and rebuild the network. Reject reference-machine trips and unsupported individual trips of a shared multiport plant.'
        for device in self.devices:
            if any(g.object_id == object_id for g in getattr(device, 'generators', ())):
                raise _coded_error(ValueError, 'invalid_solver_data', 'Multiport plant disconnection is unsupported. Its units share one device and PPC')
        generator = next((g for g in self.network.model.get('gens', ())
                          if g['id'] == object_id), None)
        if generator is None:
            raise _coded_error(ValueError, 'invalid_solver_data', f'Unknown generator: {object_id}')
        if generator.get('ref') or int(generator.get('params', {}).get('ip_ctrl', 0)) == 1:
            raise _coded_error(ValueError, 'invalid_solver_data', 'Cannot disconnect the angular reference machine')
        self.disconnect_device(object_id)
        generator['outserv'] = 1
        self.network.switching_active = True
        self.network.rebuild()
        self.refresh_network_indexes()

    def restart_discrete(self) -> None:
        'Invalidate the derivative, predictor and linear-system cache after a discontinuity.'
        self._step_factor = self._step_key = None
        self._last_state = self._previous_state = self._last_f = None
        self._slow_steps = 0
        self.restart_pending = True

    def _ports(self, device, v: np.ndarray) -> PortMeasurements:
        """Construye solo las lecturas declaradas por el dispositivo."""
        bus_ids = tuple(dict.fromkeys((*device.read_bus_ids,
                                       *getattr(device, "read_bus_current_ids", ()))))
        voltage = {bid: complex(v[self.network.index_of_bus(bid)]) for bid in bus_ids}
        current = {}
        power = {}
        if getattr(device, "read_bus_current_ids", ()):
            yv = self.network.ybus @ v
            for bid in device.read_bus_current_ids:
                i = complex(yv[self.network.index_of_bus(bid)])
                current[bid] = i
                power[bid] = voltage[bid] * i.conjugate()
        branch_current = {}
        branch_power = {}
        active = set(self.network.topology["active"]["branches"]) if getattr(
            self.network, "topology", None) else set(self._branch_stamps)
        for bid in device.read_branch_ids:
            terminals, indexes, matrix = self._branch_stamps[bid]
            flow = matrix @ v[list(indexes)] if bid in active else np.zeros(len(indexes), complex)
            for end, (bus_id, index) in enumerate(zip(terminals, indexes)):
                key = (bid, end)
                branch_current[key] = complex(flow[end])
                branch_power[key] = complex(v[index]) * branch_current[key].conjugate()
                voltage[bus_id] = complex(v[index])
        return PortMeasurements(voltage, current, power, branch_current, branch_power)

    def refresh_network_indexes(self) -> None:
        'Reindex device ports after a switch fuses or separates network nodes.'
        for branch_id, (terminals, _, matrix) in tuple(self._branch_stamps.items()):
            self._branch_stamps[branch_id] = (
                terminals, tuple(self.network.index_of_bus(bid) for bid in terminals), matrix)
        for k, (device, multi) in enumerate(zip(self.devices, self._multiports)):
            if not multi:
                index = self.network.index_of_bus(device.bus_id)
                self._bus_indexes[k] = index
                self._port_indexes[k] = (index,)
                self._injection_indexes[k] = (index,)
                continue
            read_ids = tuple(device.read_bus_ids)
            injection_ids = tuple(device.injection_bus_ids)
            branch_ids = tuple(device.read_branch_ids)
            dependency = {self.network.index_of_bus(bid) for bid in
                          (*read_ids, *injection_ids)}
            for branch_id in branch_ids:
                dependency.update(self._branch_stamps[branch_id][1])
            if getattr(device, "read_bus_current_ids", ()):
                dependency.update(range(len(self.network.node_ids)))
            self._bus_indexes[k] = self.network.index_of_bus(read_ids[0])
            self._port_indexes[k] = tuple(sorted(dependency))
            self._injection_indexes[k] = tuple(self.network.index_of_bus(bid)
                                               for bid in injection_ids)
        self._step_factor = self._step_key = None
        self._last_state = self._previous_state = None
        self._last_f = None

    def _multi_value(self, device, t, x, z, v):
        ports = self._ports(device, v)
        f = np.asarray(device.f_multi(t, x, z, ports), dtype=float).reshape(-1)
        g = np.asarray(device.g_multi(t, x, z, ports), dtype=float).reshape(-1) if device.n_alg else np.empty(0)
        injection = device.current_injections_multi(x, z, ports)
        if set(injection) != set(device.injection_bus_ids):
            raise _coded_error(ValueError, 'invalid_solver_data', f'Incomplete multiport injections: {device.object_id}')
        currents = np.asarray([complex(injection[bid]) for bid in device.injection_bus_ids])
        return f, g, currents

    def _active_limit_signature(self) -> tuple:
        """Estado de límites que un dispositivo exponga de forma opcional."""
        signature = []
        for device in self.devices:
            if not self.in_service[device.object_id]:
                continue
            active = getattr(device, "active_limit_signature", None)
            if active is not None:
                signature.append((device.object_id, active()))
        return tuple(signature)

    def _check_state(self, state: DAEState) -> None:
        if (state.x.size != self.nx or state.z.size != self.nz
                or state.v.size != len(self.network.node_ids)):
            raise _coded_error(ValueError, 'invalid_solver_data', 'State dimensions do not match devices/network')

    def prescribed_voltages(self, t):
        """Return bus-base phasors from an optional playback provider."""
        provider = getattr(self, 'voltage_provider', None)
        return {} if provider is None else provider(t)

    def boundary_currents(self, state):
        """Omitted KCL residual, positive out of the subsystem."""
        provider = getattr(self, 'voltage_provider', None)
        self.voltage_provider = None
        try:
            _, _, residual = self._evaluate(state.t, state.x, state.z, state.v)
        finally:
            self.voltage_provider = provider
        return rect_to_complex(residual)

    def _evaluate(self, t: float, x: np.ndarray, z: np.ndarray,
                  v: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        self.set_frequency_reference(x)
        derivatives, algebraic = [], []
        injection = np.zeros(len(self.network.node_ids), dtype=complex)
        for device, xs, zs, bus_index, multi, inject_indexes in zip(
                self.devices, self.x_slices, self.z_slices, self._bus_indexes,
                self._multiports, self._injection_indexes):
            if not self.in_service[device.object_id]:
                derivatives.append(np.zeros(device.n_states))
                if device.n_alg:
                    algebraic.append(z[zs])
                continue
            bus_v = complex(v[bus_index])
            if multi:
                dx, g, currents = self._multi_value(device, t, x[xs], z[zs], v)
            else:
                dx = np.asarray(device.f(t, x[xs], z[zs], bus_v), dtype=float).reshape(-1)
            if dx.size != device.n_states:
                raise _coded_error(ValueError, 'invalid_solver_data', f'Incorrect f/g dimension: {device.object_id}')
            derivatives.append(dx)
            if device.n_alg:
                if not multi:
                    g = np.asarray(device.g(t, x[xs], z[zs], bus_v), dtype=float).reshape(-1)
                if g.size != device.n_alg:
                    raise _coded_error(ValueError, 'invalid_solver_data', f'Incorrect f/g dimension: {device.object_id}')
                algebraic.append(g)
            if multi:
                for index, current in zip(inject_indexes, currents):
                    injection[index] += current
            else:
                injection[bus_index] += complex(device.current_injection(x[xs], bus_v))
        f = np.concatenate(derivatives) if derivatives else np.empty(0)
        g = np.concatenate(algebraic) if algebraic else np.empty(0)
        network_residual = injection - self.network.ybus @ v
        # A bolted three-phase fault replaces KCL by V=0. The fault current
        # balances the omitted KCL equation, without a surrogate impedance.
        for i in getattr(self.network, 'solid_fault_indexes', ()):
            network_residual[i] = v[i]
        for bid, value in self.prescribed_voltages(t).items():
            i = self.network.index_of_bus(bid)
            if i in getattr(self.network, 'solid_fault_indexes', ()):
                raise ValueError('A boundary cannot also have a bolted fault')
            network_residual[i] = v[i] - value
        net = complex_to_rect(network_residual)
        if not (np.isfinite(f).all() and np.isfinite(g).all() and np.isfinite(net).all()):
            raise _coded_error(ValueError, 'invalid_solver_data', 'DAE residuals must be finite')
        return f, g, net

    def _scale(self, old: DAEState, x: np.ndarray, algebraic_only: bool = False) -> np.ndarray:
        x_scale = self.atol + self.rtol * np.maximum(1.0, np.maximum(abs(old.x), abs(x)))
        alg_scale = np.full(self.nz + 2 * len(self.network.node_ids), self.atol + self.rtol)
        return alg_scale if algebraic_only else np.r_[x_scale, alg_scale]

    def _local_jacobian(self, t: float, x: np.ndarray, z: np.ndarray,
                        v: np.ndarray, *, old: DAEState | None = None,
                        h: float = 0.0, f_old: np.ndarray | None = None,
                        method: str = "algebraic") -> np.ndarray:
        """Ensambla dR/du: solo perturba el dispositivo y la barra afectados.

        La corriente es inyección positiva en p.u.; la derivada de red es
        -Ybus fija entre eventos. Las diferencias locales usan el mismo paso
        central que el Newton global original.
        """
        self.set_frequency_reference(x)
        nv = len(self.network.node_ids)
        dynamic = old is not None
        offset = self.nx if dynamic else 0
        size = offset + self.nz + 2 * nv
        jac = np.zeros((size, size), dtype=float)
        admittance = -self.network.ybus
        real, imag = admittance.real, admittance.imag
        voltage_start = offset + self.nz
        jac[offset+self.nz:offset+self.nz+nv, voltage_start:voltage_start+nv] = real
        jac[offset+self.nz:offset+self.nz+nv, voltage_start+nv:] = -imag
        jac[offset+self.nz+nv:, voltage_start:voltage_start+nv] = imag
        jac[offset+self.nz+nv:, voltage_start+nv:] = real
        for device, xs, zs, bus_index, multi, port_indexes, inject_indexes in zip(
                self.devices, self.x_slices, self.z_slices, self._bus_indexes,
                self._multiports, self._port_indexes, self._injection_indexes):
            nx_local, nz_local = xs.stop-xs.start, zs.stop-zs.start
            if not self.in_service[device.object_id]:
                if dynamic:
                    jac[xs, xs] = np.eye(nx_local)
                alg_slice = slice(offset+zs.start, offset+zs.stop)
                jac[alg_slice, alg_slice] = np.eye(nz_local)
                continue
            if multi:
                rows = (list(range(xs.start, xs.stop)) if dynamic else [])
                rows += list(range(offset+zs.start, offset+zs.stop))
                rows += [offset+self.nz+i for i in inject_indexes]
                rows += [offset+self.nz+nv+i for i in inject_indexes]
                cols = (list(range(xs.start, xs.stop)) if dynamic else [])
                cols += list(range(offset+zs.start, offset+zs.stop))
                cols += [voltage_start+i for i in port_indexes]
                cols += [voltage_start+nv+i for i in port_indexes]
                local_x, local_z = x[xs].copy(), z[zs].copy()
                point = np.r_[(local_x if dynamic else np.empty(0)), local_z,
                              v[list(port_indexes)].real, v[list(port_indexes)].imag]

                def multi_residual(values):
                    cursor = 0
                    lx = values[:nx_local] if dynamic else local_x
                    if dynamic:
                        cursor = nx_local
                    lz = values[cursor:cursor+nz_local]
                    cursor += nz_local
                    voltage = v.copy()
                    count = len(port_indexes)
                    voltage[list(port_indexes)] = (values[cursor:cursor+count]
                                                   + 1j*values[cursor+count:cursor+2*count])
                    derivative, algebraic, currents = self._multi_value(
                        device, t, lx, lz, voltage)
                    if dynamic:
                        factor = h if method == "euler" else h*self._implicit_weight
                        constant = np.zeros(nx_local) if method == "euler" else h*f_old[xs]*(1-self._implicit_weight)
                        first = lx-old.x[xs]-constant-factor*derivative
                    else:
                        first = np.empty(0)
                    return np.r_[first, algebraic, currents.real, currents.imag]

                dependency_method = getattr(device, 'jacobian_dependencies', None)
                groups = [(j,) for j in range(len(cols))]
                supports = None
                if self.grouped_multiport_differences and dependency_method is not None:
                    all_bus_ids = set(device.read_bus_ids) | set(device.injection_bus_ids)
                    branch_buses = {bid: self._branch_stamps[bid][0] for bid in device.read_branch_ids}
                    for bids in branch_buses.values():
                        all_bus_ids.update(bids)
                    start = (nx_local if dynamic else 0)+nz_local
                    bus_columns = {bid: (start+port_indexes.index(self.network.index_of_bus(bid)),
                        start+len(port_indexes)+port_indexes.index(self.network.index_of_bus(bid)))
                        for bid in all_bus_ids}
                    group_key = device.object_id, dynamic, tuple(sorted(bus_columns.items()))
                    if group_key not in self._difference_groups:
                        dependencies = dependency_method(dynamic=dynamic,
                            bus_columns=bus_columns, branch_buses=branch_buses)
                        if len(dependencies) != len(rows):
                            raise _coded_error(ValueError, 'invalid_solver_data', 'Incorrect multiport dependency rows')
                        support = [{i for i, deps in enumerate(dependencies) if j in deps}
                                   for j in range(len(cols))]
                        colored, occupied = [], []
                        for j in sorted(range(len(cols)), key=lambda j: len(support[j]), reverse=True):
                            if not support[j]:
                                continue
                            for k, taken in enumerate(occupied):
                                if taken.isdisjoint(support[j]):
                                    colored[k].append(j)
                                    taken.update(support[j])
                                    break
                            else:
                                colored.append([j])
                                occupied.append(set(support[j]))
                        self._difference_groups[group_key] = colored, [sorted(s) for s in support]
                    groups, supports = self._difference_groups[group_key]
                local_jac = np.zeros((len(rows), len(cols)), dtype=float)
                deltas = np.maximum(1e-8, self.fd_rel_step*np.maximum(1., abs(point)))
                for group in groups:
                    plus, minus = point.copy(), point.copy()
                    plus[group] += deltas[group]
                    minus[group] -= deltas[group]
                    difference = multi_residual(plus)-multi_residual(minus)
                    for j in group:
                        selected = supports[j] if supports is not None else slice(None)
                        local_jac[selected, j] = difference[selected]/(2*deltas[j])
                np.add.at(jac, (np.asarray(rows)[:, None], np.asarray(cols)[None, :]),
                          local_jac)
                continue
            rows = (list(range(xs.start, xs.stop)) if dynamic else [])
            rows += list(range(offset+zs.start, offset+zs.stop))
            rows += [offset+self.nz+bus_index, offset+self.nz+nv+bus_index]
            cols = (list(range(xs.start, xs.stop)) if dynamic else [])
            cols += list(range(offset+zs.start, offset+zs.stop))
            cols += [voltage_start+bus_index, voltage_start+nv+bus_index]
            local_x = x[xs].copy()
            local_z = z[zs].copy()
            bus_v = complex(v[bus_index])
            point = np.r_[(local_x if dynamic else np.empty(0)), local_z,
                          bus_v.real, bus_v.imag]

            local_method = getattr(device, "jacobian_local", None)
            if callable(local_method):
                raw = np.asarray(local_method(t, local_x, local_z, bus_v,
                                              rel_step=self.fd_rel_step), dtype=float)
                dimension = nx_local+nz_local+2
                if raw.shape != (dimension, dimension) or not np.isfinite(raw).all():
                    raise _coded_error(ValueError, 'invalid_solver_data', f'Incorrect local Jacobian: {device.object_id}')
                if dynamic:
                    local_jac = raw.copy()
                    factor = h if method == "euler" else h*self._implicit_weight
                    local_jac[:nx_local] *= -factor
                    local_jac[:nx_local, :nx_local] += np.eye(nx_local)
                else:
                    local_jac = raw[nx_local:, nx_local:].copy()
                jac[np.ix_(rows, cols)] += local_jac
                continue

            def local_residual(values):
                cursor = 0
                if dynamic:
                    lx = values[:nx_local]
                    cursor = nx_local
                else:
                    lx = local_x
                lz = values[cursor:cursor+nz_local]
                lv = complex(values[-2], values[-1])
                if dynamic:
                    derivative = np.asarray(device.f(t, lx, lz, lv), dtype=float).reshape(-1)
                    factor = h if method == "euler" else h*self._implicit_weight
                    constant = np.zeros(nx_local) if method == "euler" else h*f_old[xs]*(1-self._implicit_weight)
                    first = lx-old.x[xs]-constant-factor*derivative
                else:
                    first = np.empty(0)
                g = np.asarray(device.g(t, lx, lz, lv), dtype=float).reshape(-1)
                current = complex(device.current_injection(lx, lv))
                return np.r_[first, g, current.real, current.imag]

            local_jac = np.empty((len(rows), len(cols)), dtype=float)
            for j in range(len(cols)):
                step = max(1e-8, self.fd_rel_step*max(1.0, abs(point[j])))
                plus, minus = point.copy(), point.copy()
                plus[j] += step
                minus[j] -= step
                local_jac[:, j] = (local_residual(plus)-local_residual(minus))/(2*step)
            jac[np.ix_(rows, cols)] += local_jac
        if dynamic and self.reference_speed_index is not None:
            # La referencia es un acoplamiento entre dispositivos. Sustituir
            # su columna completa evita omitirla en las diferencias locales.
            j = self.reference_speed_index
            delta = max(1e-8, self.fd_rel_step*max(1., abs(x[j])))
            plus, minus = x.copy(), x.copy()
            plus[j] += delta
            minus[j] -= delta
            fp,gp,np_ = self._evaluate(t, plus, z, v)
            fm,gm,nm = self._evaluate(t, minus, z, v)
            factor = h if method == 'euler' else h*self._implicit_weight
            jac[:,j] = np.r_[-factor*(fp-fm), gp-gm, np_-nm]/(2*delta)
            jac[j,j] += 1.
            self.set_frequency_reference(x)
        constrained = set(getattr(self.network, 'solid_fault_indexes', ()))
        constrained.update(self.network.index_of_bus(bid) for bid in self.prescribed_voltages(t))
        for i in constrained:
            for component in (i, nv+i):
                row = offset+self.nz+component
                jac[row, :] = 0.
                jac[row, voltage_start+component] = 1.
        return jac

    def _newton(self, residual, guess: np.ndarray, scale: np.ndarray,
                jacobian, *, cache_key: tuple | None = None) -> tuple[np.ndarray, int, float, float]:
        u = np.asarray(guess, dtype=float).copy()
        last_alpha = 1.0
        factor = self._step_factor if cache_key == self._step_key else None
        accepted_residual = None
        accepted_error = None
        for iteration in range(self.max_iter + 1):
            if accepted_residual is None:
                r = residual(u)
                error = float(np.max(abs(r / scale))) if r.size else 0.0
            else:
                r, error = accepted_residual, accepted_error
                accepted_residual = accepted_error = None
            if error <= 1.0:
                if cache_key is not None and factor is not None:
                    self._step_factor, self._step_key = factor, cache_key
                return u, iteration, error, last_alpha
            if iteration == self.max_iter:
                break
            for refresh in range(2):
                if factor is None or iteration >= 4:
                    try:
                        factor = LinearFactorization(jacobian(u), self.linear_solver)
                    except np.linalg.LinAlgError as exc:
                        raise _coded_error(SolverConvergenceError, 'singular_dae_jacobian', 'Singular DAE Jacobian') from exc
                try:
                    delta = -factor.solve(r)
                except np.linalg.LinAlgError as exc:
                    raise _coded_error(SolverConvergenceError, 'singular_dae_jacobian', '[singular_dae_jacobian] Singular DAE Jacobian') from exc
                alpha = 1.0
                accepted = False
                for _ in range(20):
                    candidate = u + alpha * delta
                    try:
                        trial = residual(candidate)
                        trial_error = float(np.max(abs(trial / scale))) if trial.size else 0.0
                    except (ValueError, FloatingPointError):
                        trial_error = float("inf")
                    if np.isfinite(trial_error) and trial_error < error:
                        u, last_alpha, accepted = candidate, alpha, True
                        accepted_residual, accepted_error = trial, trial_error
                        break
                    alpha *= 0.5
                if accepted:
                    break
                factor = None
            if not accepted:
                self._step_factor = self._step_key = None
                raise _coded_error(SolverConvergenceError, 'rms_not_converged', f'Newton found no residual descent. Scaled residual={error:.6g}')
        self._step_factor = self._step_key = None
        self._slow_steps = 0
        raise _coded_error(SolverConvergenceError, 'rms_not_converged', f'Newton exceeded {self.max_iter} iterations. Scaled residual={error:.6g}')

    def solve_equilibrium(self, state: DAEState) -> tuple[DAEState, dict]:
        'Solve f=g=KCL=0 from inc/load flow without changing parameters or setpoints. Preserve declared fixed discrete states. Audit the least-squares correction against the load-flow voltage.'
        initial = np.r_[state.x, state.z, state.v.real, state.v.imag]
        point = initial.copy()
        free = np.ones(len(point), dtype=bool)
        for dev, xs in zip(self.devices, self.x_slices):
            for i in getattr(dev, 'equilibrium_fixed_state_indexes', ()):
                free[xs.start+i] = False

        def unpack(values):
            return DAEState(state.t, values[:self.nx].copy(),
                values[self.nx:self.nx+self.nz].copy(),
                rect_to_complex(values[self.nx+self.nz:]))

        def residual(values):
            st = unpack(values)
            return np.concatenate(self._evaluate(st.t, st.x, st.z, st.v))

        for iteration in range(self.max_iter+1):
            error = residual(point)
            if np.max(abs(error)) < 1e-9:
                result = unpack(point)
                delta_v = float(np.max(abs(result.v-state.v)))
                if delta_v > 1e-4:
                    raise _coded_error(SolverConvergenceError, 'rms_not_converged', f'Equilibrium is too far from load flow: delta V={delta_v}')
                return result, {'iterations': iteration, 'max_residual': float(np.max(abs(error))),
                    'max_voltage_correction_pu': delta_v,
                    'max_state_correction': float(np.max(abs(result.x-state.x))),
                    'max_algebraic_correction': float(np.max(abs(result.z-state.z), initial=0.)),
                    'fixed_discrete_states': int(sum(~free))}
            st = unpack(point)
            jac = self._local_jacobian(st.t, st.x, st.z, st.v, old=st,
                h=1., f_old=np.zeros(self.nx), method='euler')
            jac[:self.nx] *= -1
            jac[:self.nx, :self.nx] += np.eye(self.nx)
            reduced = jac[np.ix_(free, free)]
            scale = np.maximum(np.max(abs(reduced), axis=1), 1.)
            step = np.linalg.lstsq(reduced/scale[:, None], -error[free]/scale, rcond=1e-12)[0]
            for damping in (1., .5, .25, .125, .0625, .03125):
                trial = point.copy()
                trial[free] += damping*step
                if np.linalg.norm(residual(trial)[free]/scale) < np.linalg.norm(error[free]/scale):
                    point = trial
                    break
            else:
                raise _coded_error(SolverConvergenceError, 'rms_not_converged', 'Equilibrium initialization found no residual descent')
        raise _coded_error(SolverConvergenceError, 'rms_not_converged', 'Equilibrium initialization did not converge')

    def solve_algebraic(self, state: DAEState) -> tuple[DAEState, StepMetrics]:
        'Solve z and V with x fixed during initialization or after an event.'
        self._check_state(state)
        nv = len(self.network.node_ids)

        def residual(u):
            z = u[:self.nz]
            v = rect_to_complex(u[self.nz:])
            _, g, net = self._evaluate(state.t, state.x, z, v)
            return np.r_[g, net]

        guess = np.r_[state.z, complex_to_rect(state.v)]
        scale = self._scale(state, state.x, algebraic_only=True)
        result, iters, error, alpha = self._newton(
            residual, guess, scale,
            lambda u: self._local_jacobian(state.t, state.x, u[:self.nz],
                                           rect_to_complex(u[self.nz:])))
        self._step_factor = self._step_key = None
        self._slow_steps = 0
        self._last_state = self._previous_state = None
        self._last_f = None
        solved = DAEState(state.t, state.x.copy(), result[:self.nz].copy(),
                          rect_to_complex(result[self.nz:]))
        return solved, StepMetrics(state.t, 0.0, "algebraic", iters, error, alpha)

    def initialize(self, lf_voltages: dict[str, complex],
                   lf_device_values: dict[str, complex]) -> tuple[DAEState, StepMetrics]:
        'Initialize x from explicit load-flow inputs and solve the algebraic network without changing x. Each device declares whether its LF input is current or power.'
        x = []
        for device in self.devices:
            if device.object_id not in lf_device_values:
                raise _coded_error(ValueError, 'missing_solver_data', f'Missing load-flow value for {device.object_id}')
            if callable(getattr(device, "init_multi", None)):
                value = device.init_multi(lf_voltages,
                                          complex(lf_device_values[device.object_id]))
            else:
                value = device.init(complex(lf_voltages[device.bus_id]),
                                    complex(lf_device_values[device.object_id]))
            state = np.asarray(value, dtype=float).reshape(-1)
            if state.size != device.n_states:
                raise _coded_error(ValueError, 'invalid_solver_data', f'init returned an incorrect dimension: {device.object_id}')
            x.append(state)
        initial = DAEState(0.0, np.concatenate(x) if x else np.empty(0),
                           np.zeros(self.nz), self.network.voltage_vector(lf_voltages))
        return self.solve_algebraic(initial)

    def _step_continuous(self, state: DAEState, step_s: float | None = None) -> tuple[DAEState, StepMetrics]:
        """Avanza un paso; tras un evento usa Euler implícito una sola vez."""
        self._check_state(state)
        h = self.step_s if step_s is None else float(step_s)
        if h <= 0:
            raise _coded_error(ValueError, 'invalid_solver_data', 'step_s must be positive')
        method = "euler" if self.restart_pending and self.restart_euler else "trapezoidal"
        t_new = state.t + h
        if (state is self._last_state and self._last_f is not None
                and abs(state.t-self._last_state.t) < 1e-12):
            f_old = self._last_f
        else:
            f_old, _, _ = self._evaluate(state.t, state.x, state.z, state.v)
        nv = len(self.network.node_ids)
        last_f = None

        def residual(u):
            nonlocal last_f
            x = u[:self.nx]
            z = u[self.nx:self.nx + self.nz]
            v = rect_to_complex(u[self.nx + self.nz:])
            f, g, net = self._evaluate(t_new, x, z, v)
            last_f = f
            if method == "euler":
                dynamic = x-state.x-h*f
            elif self._implicit_weight == .5:
                dynamic = x-state.x-h*((f_old+f)/2)
            else:
                dynamic = x-state.x-h*((1-self._implicit_weight)*f_old+self._implicit_weight*f)
            return np.r_[dynamic, g, net]

        guess = np.r_[state.x + h * f_old, state.z, complex_to_rect(state.v)]
        if (method == "trapezoidal" and self._previous_state is not None
                and state is self._last_state
                and abs((state.t-self._previous_state.t)-h) < 1e-12):
            previous = self._previous_state
            guess = np.r_[2*state.x-previous.x, 2*state.z-previous.z,
                          complex_to_rect(2*state.v-previous.v)]
        scale = self._scale(state, guess[:self.nx])
        key = (h, method, self._implicit_weight, id(self.network.ybus), self._active_limit_signature(), self.linear_solver)
        if self.linear_solver != "compatibility":
            key += (getattr(self.network, "revision", 0), tuple(self.in_service.items()),
                    tuple(sorted(getattr(self, "bolted_bus_ids", ()))),)
        result, iters, error, alpha = self._newton(
            residual, guess, scale,
            lambda u: self._local_jacobian(
                t_new, u[:self.nx], u[self.nx:self.nx+self.nz],
                rect_to_complex(u[self.nx+self.nz:]), old=state,
                h=h, f_old=f_old, method=method), cache_key=key)
        solved = DAEState(t_new, result[:self.nx].copy(),
                          result[self.nx:self.nx + self.nz].copy(),
                          rect_to_complex(result[self.nx + self.nz:]))
        self.restart_pending = False
        # Dos iteraciones de Newton modificado son baratas frente a reconstruir
        # los 399 estados acoplados. El propio Newton refresca ante falta de
        # descenso o a partir de su cuarta iteración; aquí anticipamos solo
        # deterioro sostenido de tres iteraciones.
        if iters >= 3:
            self._slow_steps += 1
            if self._slow_steps >= 3:
                self._step_factor = self._step_key = None
                self._slow_steps = 0
        else:
            self._slow_steps = 0
        self._previous_state, self._last_state, self._last_f = state, solved, last_f
        return solved, StepMetrics(t_new, h, method, iters, error, alpha)
