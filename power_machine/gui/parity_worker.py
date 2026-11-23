"""Trabajos de paridad en hilo, mensajes entregados por sondeo en el hilo de Tk."""
from power_machine.gui.i18n import tr
from copy import deepcopy
import json
from queue import Empty, Queue
import threading
import time
import traceback

from .panels.registry import job_entrypoint
from .bus import bus


class JobHandle:
    def __init__(self, callback):
        self.callback = callback
        self.cancel_event = threading.Event()
        self.finished = threading.Event()
        self.thread = None

    def cancel(self):
        self.cancel_event.set()


class ParityWorker:
    """El hilo no conoce widgets. shutdown cancela y desconecta sin bloquear Tk.

    La cancelación es cooperativa con execute_request. Una operación numpy ya
    iniciada puede terminar después del cierre, sin entregar callbacks a Tk.
    """
    def __init__(self, widget, *, resolver=job_entrypoint, on_error=None, event_bus=bus):
        self.widget = widget
        self.resolver = resolver
        self.on_error = on_error or (lambda message: None)
        self.event_bus = event_bus
        self.queue = Queue()
        self.jobs = set()
        self.after_id = None
        self.closed = False

    @property
    def running(self):
        return any(not job.finished.is_set() for job in self.jobs)

    def submit(self, module_id, request, callback):
        if self.closed:
            raise RuntimeError(tr('integration.parity_worker_closed'))
        # La frontera excluye objetos vivos y números no finitos antes del hilo.
        json.dumps(request, allow_nan=False)
        request = deepcopy(request)
        job = JobHandle(callback)
        self.jobs.add(job)

        def run():
            try:
                execute = self.resolver(module_id)
                result = execute(request, cancel=job.cancel_event,
                                 progress=lambda value: self.queue.put((job, ("progress", deepcopy(value)))))
                json.dumps(result, allow_nan=False)
                self.queue.put((job, ("result", deepcopy(result))))
            except Exception:
                self.queue.put((job, ("error", traceback.format_exc())))
            finally:
                job.finished.set()
                self.queue.put((job, ("finished",)))

        job.thread = threading.Thread(target=run, daemon=True, name="power_machine-paridad-" + module_id)
        job.thread.start()
        self.event_bus.publish('worker.progress', kind=module_id, completed=0, total=None, cancelling=False)
        if self.after_id is None:
            self.after_id = self.widget.after(75, self.poll)
        return job

    def poll(self):
        self.after_id = None
        if self.closed:
            return
        deadline = time.monotonic() + .04
        while time.monotonic() < deadline:
            try:
                job, message = self.queue.get_nowait()
            except Empty:
                break
            if message[0] == "finished":
                self.jobs.discard(job)
                job.callback = None
                self.event_bus.publish('worker.progress', kind='parity', completed=1, total=1, cancelling=job.cancel_event.is_set(), terminal='finished')
            elif message[0] == 'progress':
                value = message[1] if len(message) > 1 and isinstance(message[1], dict) else {}
                self.event_bus.publish('worker.progress', kind='parity', completed=value.get('completed'), total=value.get('total'), cancelling=job.cancel_event.is_set())
                if job.callback is not None:
                    try:
                        job.callback(message)
                    except Exception:
                        self.on_error(traceback.format_exc())
            elif job.callback is not None:
                try:
                    job.callback(message)
                except Exception:
                    self.on_error(traceback.format_exc())
        if self.jobs or not self.queue.empty():
            self.after_id = self.widget.after(75, self.poll)

    def cancel(self):
        for job in self.jobs:
            job.cancel()
        if self.jobs:
            self.event_bus.publish('worker.cancelled', kind='parity', requested=True)

    def shutdown(self):
        self.closed = True
        self.cancel()
        for job in self.jobs:
            job.callback = None
        self.jobs.clear()
        if self.after_id is not None:
            self.widget.after_cancel(self.after_id)
            self.after_id = None
