'Events support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass, field

import numpy as np

from .solver import DAEState, StepMetrics


@dataclass(frozen=True)
class Event:
    'Discrete change at absolute time_s on a branch, load, switch, generator or bus ID. kind selects the supported switching, load, trip or fault operation. Event values preserve their declared units.'

    time_s: float
    kind: str
    target_id: str
    value: complex | float | None = None


@dataclass
class Trajectory:
    samples: list[DAEState] = field(default_factory=list)
    metrics: list[StepMetrics] = field(default_factory=list)
    discontinuities: list[dict] = field(default_factory=list)


class EventTimeline:
    'Stable ordering of simultaneous events, with absolute time in seconds.'

    def __init__(self, events=()):
        self.events = sorted(list(events), key=lambda event: event.time_s)
        for event in self.events:
            if not np.isfinite(event.time_s) or event.time_s < 0:
                raise _coded_error(ValueError, 'invalid_events_data', 'Invalid event time')
            if event.kind not in ("branch_open", "branch_close",
                                  "element_switch_open", "element_switch_close",
                                  "switch_open", "switch_close", "load_factor",
                                  "fault_apply", "fault_clear", "gen_trip"):
                raise _coded_error(ValueError, 'invalid_events_data', f'Unsupported event type: {event.kind}')

    @staticmethod
    def _apply(network, event: Event) -> None:
        if event.kind == "branch_open":
            network.set_branch_open(event.target_id, True)
        elif event.kind == "branch_close":
            network.set_branch_open(event.target_id, False)
        elif event.kind == "element_switch_open":
            network.set_element_switching(event.target_id, True)
        elif event.kind == "element_switch_close":
            network.set_element_switching(event.target_id, False)
        elif event.kind == "switch_open":
            network.set_switch_open(event.target_id, True)
        elif event.kind == "switch_close":
            network.set_switch_open(event.target_id, False)
        elif event.kind == "load_factor":
            if event.value is None:
                raise _coded_error(ValueError, 'missing_events_data', 'load_factor requires a value')
            network.set_load_factor(event.target_id, float(event.value))
        elif event.kind == "fault_apply":
            if event.value is None:
                raise _coded_error(ValueError, 'missing_events_data', 'fault_apply requires an impedance')
            network.set_fault(event.target_id, complex(event.value))
        else:
            network.set_fault(event.target_id, None)

    def run(self, solver, initial: DAEState, end_s: float) -> Trajectory:
        'Integrate and solve z,V again at each discontinuity without changing x. Pre-event and post-event samples share the event time.'
        if end_s < initial.t or not np.isfinite(end_s):
            raise _coded_error(ValueError, 'invalid_events_data', 'Invalid end time')
        tolerance = 1e-12
        events = [event for event in self.events
                  if initial.t - tolerance <= event.time_s <= end_s + tolerance]
        if len(events) != len(self.events):
            raise _coded_error(ValueError, 'invalid_events_data', 'Event is outside the simulation interval')
        result = Trajectory(samples=[initial.copy()])
        state = initial.copy()
        position = 0
        while state.t < end_s - tolerance or position < len(events):
            next_event_s = events[position].time_s if position < len(events) else end_s
            target_s = min(next_event_s, end_s)
            if target_s > state.t + tolerance:
                h = min(solver.step_s, target_s - state.t)
                state, metrics = solver.step(state, h)
                if abs(state.t - target_s) <= tolerance:
                    state.t = target_s
                result.samples.append(state.copy())
                result.metrics.append(metrics)
                continue
            if position < len(events) and abs(events[position].time_s - state.t) <= tolerance:
                first = position
                old_x = state.x.copy()
                old_bus_voltage = solver.network.bus_voltages(state.v)
                while position < len(events) and abs(events[position].time_s - state.t) <= tolerance:
                    if events[position].kind == 'gen_trip':
                        solver.trip_generator(events[position].target_id)
                    else:
                        self._apply(solver.network, events[position])
                    position += 1
                # La fusión/separación puede reordenar nodos; el solver guarda
                # los índices de los dispositivos desde su construcción.
                solver.refresh_network_indexes()
                trial = DAEState(state.t, old_x, state.z.copy(),
                                 solver.network.voltage_vector(old_bus_voltage))
                state, metrics = solver.solve_algebraic(trial)
                solver.restart_pending = True
                result.samples.append(state.copy())
                result.metrics.append(metrics)
                result.discontinuities.append({"time_s": state.t,
                                               "before_index": len(result.samples) - 2,
                                               "after_index": len(result.samples) - 1,
                                               "events": events[first:position]})
                continue
            if abs(state.t - end_s) <= tolerance:
                break
            raise _coded_error(RuntimeError, 'invalid_events_data', 'The timeline could not advance')
        return result
