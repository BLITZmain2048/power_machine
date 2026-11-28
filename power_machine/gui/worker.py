"""Cálculo RMS aislado mediante spawn. Tk solo se usa en el proceso principal."""
from power_machine.gui.i18n import tr
import multiprocessing
from queue import Empty
import time
import traceback
import os
import csv
import json
from pathlib import Path
from copy import deepcopy
from .paths import run_directory
from .bus import bus


def calculation_identity(study):
    """Las páginas y la casilla de recálculo no alteran el estado eléctrico."""
    value = deepcopy(study)
    value.pop('plots', None)
    value.get('ldf_options', {}).pop('gui_recalculate_ic', None)
    return value


def simulate(root: str, study: dict, queue, cancel_event, commands=None, model=None):
    try:
        for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ[variable] = '1'
        from .native_case import is_native_case, simulate_native
        if model is not None and is_native_case(model, study) and commands is None:
            try:
                simulate_native(root, study, model, queue, cancel_event)
            except InterruptedError:
                queue.put(('done', {'completed': False, 'cancelled': True, 'steps': 0}))
            return
        from power_machine.study import StudyCase, Simulation
        case = StudyCase.from_json(study)
        simulation = Simulation(root, case, model=model,
                                log=lambda level, text: queue.put(('log', level, text)))
        queue.put(('ic', simulation.initial_conditions()))
        if commands is not None:
            queue.put(('prepared',))
            while not cancel_event.is_set():
                try:
                    command, saved_study = commands.get(timeout=.2)
                except Empty:
                    continue
                if command != 'run':
                    return
                # Solo se acepta una corrida con el mismo estado inicial.
                if calculation_identity(saved_study) != calculation_identity(study):
                    raise ValueError(tr('integration.initial_conditions_no_longer_match_the_case'))
                case = StudyCase.from_json(saved_study)
                break
            else:
                return
        directory = run_directory(root, case.name)
        case.save(directory / 'caso.pmstudy')
        summary = simulation.run(
            on_header=lambda keys: queue.put(('header', keys)),
            on_samples=lambda rows: queue.put(('samples', rows)),
            on_progress=lambda t, end, ms: queue.put(('progress', t, end, ms)),
            cancel=cancel_event, output_dir=directory, batch=10,
        )
        queue.put(('done', summary))
    except BaseException:
        queue.put(('error', traceback.format_exc()))


def simulate_benchmark(root, model, model_path, event, end_s, step_s, queue, cancel_event):
    """RMS clásico IEEE 39 en proceso separado, cancelable en cada etapa RK4."""
    started = time.monotonic()
    try:
        for variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
            os.environ[variable] = '1'
        from power_machine.benchmarks.ieee39_dynamics import ClassicalSystem, simulate as run
        root, model_path = Path(root).resolve(), Path(model_path).resolve()
        data_path = model_path.parent / 'dynamic_data.json'
        if cancel_event.is_set():
            raise InterruptedError(tr('integration.cancellation_requested'))
        system = ClassicalSystem(model, json.loads(data_path.read_text(encoding='utf-8-sig')))
        original_rhs = system.rhs
        def rhs(state):
            if cancel_event.is_set():
                raise InterruptedError(tr('integration.cancellation_requested'))
            return original_rhs(state)
        system.rhs = rhs
        queue.put(('log', 'INFO', event + tr('integration.classical_model_no_ref_validation')))
        result = run(system, event, end_s=end_s, step_s=step_s)
        if cancel_event.is_set():
            raise InterruptedError(tr('integration.cancellation_requested'))
        from power_machine.resources import writable_root, confined_output
        output_root = writable_root().resolve()
        directory = run_directory(root, event)
        if not directory.resolve().is_relative_to(output_root):
            raise ValueError(tr('integration.output_must_remain_within_the_working_folder'))
        confined_output(directory, output_root)
        # El exportador debe atribuir el modelo efectivamente calculado.
        actual_model = directory / 'model_snapshot.json'
        actual_model.write_text(json.dumps(model, ensure_ascii=False, allow_nan=False), encoding='utf-8')
        # Exportar las muestras públicas sin modificar el motor ni sus bases.
        import math
        buses = system.model['buses']
        keys = [f"{bus['path']}|m:u|p.u." for bus in buses]
        keys += [f"{bus['path']}|m:fehz|Hz" for bus in buses]
        generators=system.generators
        keys += [f"{g['path']}|s:phi|deg" for g in generators]
        keys += [f"{g['path']}|s:xspeed|p.u." for g in generators]
        from .plots import TIME_KEY
        queue.put(('header', [TIME_KEY] + keys))
        rows = []
        for sample in result.samples:
            indices = [system.network.index_of_bus(bus['id']) for bus in buses]
            values = [abs(sample.voltage_pu[i]) for i in indices]
            values += [sample.bus_frequency_hz[i] for i in indices]
            mean_angle=sum(sample.state[0])/len(sample.state[0])
            values += [math.degrees(v-mean_angle) for v in sample.state[0]]
            values += list(sample.state[1])
            rows.append([float(sample.time_s)] + [float(v) if v is not None and math.isfinite(v) else None for v in values])
        with (directory / 'signals.csv').open('w', encoding='utf-8', newline='') as stream:
            writer = csv.writer(stream)
            writer.writerow(['time_s'] + keys)
            writer.writerows(rows)
        queue.put(('samples', rows))
        report = dict(result.report)
        report['gui_signal_notes'] = {'s:phi': 'Rotor angle relative to the arithmetic mean of generator angles, in degrees', 's:xspeed': 'Generator speed in per unit'}
        (directory / 'run_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
        queue.put(('done', {**report, 'completed': True, 'cancelled': False,
                           'steps': len(result.samples), 'wall_s': time.monotonic() - started,
                           'signals_csv': str(directory / 'signals.csv')}))
    except InterruptedError:
        queue.put(('done', {'completed': False, 'cancelled': True,
                           'wall_s': time.monotonic() - started, 'steps': 0}))
    except Exception:
        queue.put(('error', traceback.format_exc()))


class SimulationManager:
    """Sondea la cola cada 125 ms y limita el trabajo por vuelta del bucle Tk."""
    def __init__(self, widget, on_message, *, event_bus=bus):
        self.widget, self.on_message = widget, on_message
        self.event_bus = event_bus
        self.context = multiprocessing.get_context('spawn')
        self.process = None
        self.queue = None
        self.cancel_event = None
        self.after_id = None
        self.terminal = False
        self.dead_since = None
        self.prepared = False
        self.commands = None
        self.study_identity = None

    @property
    def running(self):
        return self.process is not None and not self.prepared

    def start(self, root, study, *, prepare=False, model=None):
        if self.running:
            raise RuntimeError(tr('integration.a_simulation_is_already_active'))
        recalculate = study.get('ldf_options', {}).get('gui_recalculate_ic', True)
        identity = (calculation_identity(study), deepcopy(model))
        if self.prepared and not prepare and not recalculate and self.study_identity == identity:
            self.prepared = False
            self.commands.put(('run', study))
            self.on_message(('log', 'INFO', tr('integration.reusing_current_initial_conditions')))
            return
        if self.process is not None:
            self.shutdown()
        self.prepared = False
        self.study_identity = identity
        self.queue = self.context.Queue()
        self.commands = self.context.Queue() if prepare else None
        self.cancel_event = self.context.Event()
        self.terminal = False
        self.dead_since = None
        self.process = self.context.Process(target=simulate,
            args=(str(root), study, self.queue, self.cancel_event, self.commands, deepcopy(model)))
        self.process.start()
        self.event_bus.publish('worker.progress', kind='rms', completed=0, total=None, cancelling=False)
        self.after_id = self.widget.after(125, self.poll)

    def start_benchmark(self, root, model, model_path, event, *, end_s=10., step_s=.001):
        if self.running:
            raise RuntimeError(tr('integration.a_simulation_is_already_active'))
        self.shutdown()
        self.queue = self.context.Queue()
        self.cancel_event = self.context.Event()
        self.terminal, self.dead_since = False, None
        self.process = self.context.Process(target=simulate_benchmark,
            args=(str(root), deepcopy(model), str(model_path), event, end_s, step_s,
                  self.queue, self.cancel_event))
        self.process.start()
        self.event_bus.publish('worker.progress', kind='benchmark', completed=0, total=None, cancelling=False)
        self.after_id = self.widget.after(125, self.poll)

    def poll(self):
        self.after_id = None
        deadline = time.monotonic() + .04
        queue_empty = False
        while time.monotonic() < deadline:
            try:
                message = self.queue.get_nowait()
            except Empty:
                queue_empty = True
                break
            if message[0] in ('done', 'error'):
                self.terminal = True
                self.event_bus.publish('worker.progress', kind='rms', completed=1, total=1,
                                       cancelling=bool(message[1].get('cancelled')) if isinstance(message[1], dict) else False,
                                       terminal=message[0])
            elif message[0] == 'progress':
                _kind, current, end, _ms = message
                self.event_bus.publish('worker.progress', kind='rms', completed=current, total=end, cancelling=False)
            if message[0] == 'prepared':
                self.prepared = True
            self.on_message(message)
        if not self.process.is_alive():
            if not queue_empty:
                self.dead_since = None
            elif self.dead_since is None:
                self.dead_since = time.monotonic()
            # Una vuelta adicional permite drenar el hilo alimentador de Queue.
            # El presupuesto de 40 ms puede agotarse con muestras pendientes.
            # La muerte del hijo no autoriza a descartar ese remanente.
            if self.terminal or queue_empty and self.dead_since is not None and time.monotonic() - self.dead_since > .5:
                if not self.terminal:
                    self.on_message(('error', tr('integration.the_process_ended_without_a_report_code', value0=self.process.exitcode)))
                self.process.join(timeout=0)
                self.process.close()
                self.process = None
                self.queue.close()
                self.queue = None
                self.prepared = False
                if self.commands is not None:
                    self.commands.close()
                    self.commands = None
                return
        self.after_id = self.widget.after(125, self.poll)

    def cancel(self):
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.event_bus.publish('worker.cancelled', kind='rms', requested=True)

    def shutdown(self):
        self.cancel()
        if self.after_id:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
        if self.process is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join(timeout=1)
            self.process.close()
            self.process = None
        if self.queue is not None:
            self.queue.close()
            self.queue = None
        if self.commands is not None:
            self.commands.close()
            self.commands = None
        self.prepared = False
