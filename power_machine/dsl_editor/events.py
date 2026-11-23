"""Extensión aislada de DAESolver para el calendario y la publicación DSL.

El motor RMS compartido permanece intacto. La localización de cruces de
estado usa su secante salvaguardada con bisección y tolerancia de 1e-11 s.
Las muestras ordinarias son límites derechos. Cada evento publica ambos
lados con el mismo estado continuo, sin mezclar ramas algebraicas.
"""
from contextlib import contextmanager
import math

from power_machine.dynamics.solver import DAESolver
from power_machine.dynamics.dsl_switching import truth


def time_tolerance(t_s):
    """Tolerancia del calendario [s], absoluta y escalada por precisión doble."""
    return max(1e-12, 16*math.ulp(float(t_s)))


def same_time(a_s, b_s):
    return abs(a_s-b_s) <= max(time_tolerance(a_s), time_tolerance(b_s))


def invalidate(block):
    block._evaluation_cache = None
    for child in block._children.values():
        invalidate(child)


class PreviewDAESolver(DAESolver):
    """DAE del preview con ramas temporales congeladas y reinicio Euler corto.

    Un salto descarta predictor, derivada y Jacobiana. El primer intervalo
    posterior se acota a h/16 y usa Euler implícito. Luego vuelve al trapecio.
    Sin eventos se conservan los pasos y las fórmulas originales.
    """
    def __init__(self, network, devices, *, time_guards=(), **options):
        super().__init__(network, devices, **options)
        self.time_guards = time_guards
        self.block = self.devices[0].block

    def next_time_event(self, after_s):
        return min((g["time_s"] for g in self.time_guards
                    if g["time_s"] > after_s+time_tolerance(after_s)), default=None)

    def guards_at(self, t_s):
        return [g["metadata"] for g in self.time_guards if same_time(g["time_s"], t_s)]

    def _time_modes(self, t_s, side):
        modes = {}
        for guard in self.time_guards:
            margin = guard["slope"]*(t_s-guard["time_s"])
            if same_time(t_s, guard["time_s"]):
                margin = math.copysign(1., guard["slope"])*side
            modes[id(guard["memory"]), guard["key"]] = truth(guard["op"], margin, 0.)
        return modes

    @contextmanager
    def _modes(self, modes, *, natural=False):
        memories = {id(self.block.memory): self.block.memory}
        memories.update((id(g["memory"]), g["memory"]) for g in self.time_guards)
        if self._state_surfaces is not None:
            memories.update((id(b.memory), b.memory) for _, b, _, _ in
                            (*self._state_surfaces, *self._expression_surfaces))
        saved = {token: memory.switch_modes.copy() for token, memory in memories.items()}
        if natural:
            for memory in memories.values():
                memory.switch_modes.clear()
        for (token, key), mode in modes.items():
            memories[token].switch_modes[key] = mode
        changed = any(memory.switch_modes != saved[token] for token, memory in memories.items())
        if changed:
            invalidate(self.block)
        try:
            yield
        finally:
            # El paso puede cambiar modos internos, preservar esos cambios.
            if natural:
                for token, memory in memories.items():
                    memory.switch_modes.clear()
                    memory.switch_modes.update(saved[token])
            else:
                for token, key in modes:
                    if key in saved[token]:
                        memories[token].switch_modes[key] = saved[token][key]
                    else:
                        memories[token].switch_modes.pop(key, None)
            if changed or natural:
                invalidate(self.block)

    def outputs(self, t_s, x, inputs, *, side=1, overrides=None):
        """Evaluación sin confirmar memoria, límites [unidades originales]."""
        modes = self._time_modes(t_s, side)
        coincident = [e for e in self.internal_events if same_time(e["time_s"], t_s)]
        modes.update(self._event_modes(coincident, after=side > 0))
        modes.update(overrides or {})
        with self._modes(modes, natural=True):
            return self.block.evaluate(t_s, x, inputs)[0]

    def accept(self, state, inputs):
        """Confirma memoria sobre el lado derecho del estado aceptado."""
        modes = self._time_modes(state.t, 1)
        modes.update(self._event_modes([e for e in self.internal_events
                                       if same_time(e["time_s"], state.t)], after=True))
        with self._modes(modes, natural=True):
            return self.block.accept_step(state.t, state.x, inputs)

    def _event_modes(self, events, *, after):
        """Un único lado para todas las superficies que conmutan juntas."""
        modes = {}
        if self._state_surfaces is None:
            return modes
        for event in events:
            for _, b, index, surface in (*self._state_surfaces, *self._expression_surfaces):
                if (surface.metadata.get("expression") == event.get("expression")
                        and surface.metadata.get("scope_path") == event.get("scope_path")
                        and surface.metadata.get("side") == event.get("side")
                        and index == event.get("state_index_global")):
                    modes[id(b.memory), surface.key] = event["mode_after" if after else "mode_before"]
        return modes

    def step(self, state, step_s=None):
        h_s = self.step_s if step_s is None else float(step_s)
        if self.restart_pending:
            h_s = min(h_s, self.step_s/16)
        event_s = self.next_time_event(state.t)
        if event_s is not None:
            h_s = min(h_s, event_s-state.t)
        first_event = len(self.internal_events)
        with self._modes(self._time_modes(state.t, 1)):
            solved, metrics = super().step(state, h_s)
        # El reinicio local usa Euler corto y luego trapecio, sin un segundo
        # intervalo con peso 0,75 del motor RMS compartido.
        if len(self.internal_events) > first_event:
            self._internal_restart = 2
        for event in self.internal_events[first_event:]:
            point = state if same_time(event["time_s"], state.t) else solved
            inputs = self.devices[0].inputs
            event.update(before_outputs=self.outputs(point.t, point.x, inputs, side=-1),
                         after_outputs=self.outputs(point.t, point.x, inputs, side=1),
                         before_states=dict(zip(self.block.state_names, map(float, point.x))),
                         after_states=dict(zip(self.block.state_names, map(float, point.x))))
        return solved, metrics
