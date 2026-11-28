from .i18n import tr
"""Adaptador estructural de documento/modelo activos para los paneles de cálculo."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from uuid import uuid4
from .app_jobs import document_gate

from .panels.registry import JOB_MODULES
from .parity_worker import ParityWorker


def fingerprint(value):
    text = json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PanelContext:
    """Consulta proveedores vivos, pero entrega solo copias independientes.

    El proveedor entrega el modelo activo materializado y el estudio actual.
    """
    def __init__(self, widget, root, get_document, get_model, *, log,
                 select_element, on_result=None, resolver=None):
        self.root = Path(root).resolve()
        from power_machine.resources import resource_root
        if self.root != resource_root(self.root):
            raise ValueError(tr('shell.incompatible_resource_root'))
        self._get_document, self._get_model = get_document, get_model
        self._logger = log
        self._error_messages = set()
        self.select_element = select_element
        self.on_result = on_result or (lambda result: None)
        self.closed = False
        self.results = {}
        self.generation = 0
        self.external_busy = lambda: False
        self.on_activity = lambda: None
        self._tickets = {}
        self._delivery_ticket = None
        kwargs = {"resolver": resolver} if resolver is not None else {}
        self.worker = ParityWorker(widget, on_error=lambda message: self.log("ERROR", message), **kwargs)

    def log(self, level, message):
        """Un error entregado también al panel se registra una sola vez."""
        if level.upper() == 'ERROR':
            identity = str(message)
            if identity in self._error_messages:
                return
            self._error_messages.add(identity)
        self._logger(level, message)

    def get_snapshot(self):
        if self.closed:
            raise RuntimeError(tr('shell.panel_closed'))
        document = self._get_document()
        if document is None:
            raise ValueError(tr('shell.no_active_document'))
        model, study = deepcopy(self._get_model()), document.snapshot()
        hashes = {"model_sha256": fingerprint(model), "study_sha256": fingerprint(study),
                  "variant_sha256": None}
        dependencies = dict(model_sha256=hashes['model_sha256'],
                            scenario_sha256=fingerprint(study.get('scenario', {})),
                            options_sha256=fingerprint({'options': study.get('options', {}),
                                                        'ldf_options': study.get('ldf_options', {})}),
                            events_sha256=fingerprint(study.get('events', [])))
        return {'model': model, 'study': study, 'fingerprints': hashes,
                'dependencies': dependencies}


    def submit_job(self, module_id, request, on_message):
        if module_id not in JOB_MODULES or request.get("module") != module_id:
            raise ValueError(tr('shell.work_module_unregistered_or_incompatible'))
        if Path(request["root"]).resolve() != self.root:
            raise ValueError(tr('shell.work_root_differs_from_the_panel_root'))
        document = self._get_document()
        original = self.get_snapshot()
        stale = False
        generation = self.generation
        job_id = str(uuid4())
        captured = original['fingerprints']
        incoming = request.get('snapshot')
        expected_response = dict(captured)
        if incoming is not None:
            if any(incoming.get(key) != original[key] for key in ('model', 'study')):
                raise ValueError(tr('shell.work_snapshot_does_not_match_the_active'))
            if module_id == 'netedit':
                # NET usa su huella eléctrica y agrega la variante propuesta.
                from power_machine.netedit.schema import model_sha256
                expected_response['model_sha256'] = model_sha256(original['model'])
            for key in ('model_sha256', 'study_sha256'):
                if incoming.get('fingerprints', {}).get(key) != expected_response[key]:
                    raise ValueError(tr('shell.incompatible_input_fingerprint') + key)
        if self.external_busy():
            raise ValueError(tr('shell.wait_for_lf_or_rms_to_finish'))
        gate = document_gate(document)
        gate.reserve()
        request = deepcopy(request)
        from .workspace_links import relative_path,file_sha256
        inputs=[]
        def capture_files(value):
            if isinstance(value,dict):
                for key,item in value.items():
                    if key not in ('snapshot','model','study'):
                        capture_files(item)
            elif isinstance(value,(list,tuple)):
                for item in value:capture_files(item)
            elif isinstance(value,str) and len(value)<512 and Path(value).suffix.lower() in ('.json','.csv','.power_machine','.tsv'):
                path=Path(value)
                path=(path if path.is_absolute() else self.root/path)
                try:
                    if path.is_file():inputs.append(dict(path=relative_path(path),sha256=file_sha256(path),role='request'))
                except (OSError,ValueError):pass
        capture_files(request)
        request.update(job_id=job_id, generation=generation)

        def deliver(message):
            nonlocal stale
            if self.closed or stale or generation != self.generation:
                return
            if (self._get_document() is not document or self._get_model() != original["model"]
                    or document.snapshot() != original["study"]):
                stale = True
                self.log('AVISO', tr('shell.document_or_model_changed_comparison_message_discarded'))
                # NET necesita liberar la edición pendiente al recibir un error.
                on_message(("error", tr('shell.document_or_model_changed_during_calculation')))
                return
            if message[0] == 'error':
                self.log('ERROR', str(message[1]))
            elif message[0] == 'result' and message[1].get('status') == 'error':
                self.log('ERROR', str(message[1].get('diagnostics') or tr('shell.the_engine_finished_with_an_error')))
            if message[0] == 'result':
                result = deepcopy(message[1])
                if result.get('job_id', job_id) != job_id or result.get('generation', generation) != generation:
                    self.log('ERROR', tr('shell.result_discarded_due_to_an_incompatible_work'))
                    return
                if (module_id == 'sensitivities' and result.get('study_sha256') ==
                        fingerprint({'scenario': original['study'].get('scenario', {})})):
                    result.setdefault('provenance', {})['engine_study_sha256'] = result['study_sha256']
                    result['study_sha256'] = captured['study_sha256']
                for key in ('model_sha256', 'study_sha256'):
                    if key in result and result[key] != expected_response[key]:
                        self.log('ERROR', tr('shell.result_discarded_due_to_an_incompatible_fingerprint') + key)
                        return
                if result.get('module', module_id) != module_id:
                    self.log('ERROR', tr('shell.result_discarded_due_to_an_incompatible_module'))
                    return
                for key in ('model_sha256','study_sha256'):
                    result.setdefault(key,expected_response[key])
                result.setdefault('provenance',{})['input_files']=inputs
                result.update(job_id=job_id, generation=generation)
                self._tickets[job_id] = (generation, captured)
                self._delivery_ticket = job_id
                message = ('result', result)
            try:
                on_message(message)
            finally:
                self._delivery_ticket = None
                self.on_activity()

        try:
            handle = self.worker.submit(module_id, request, deliver)
            handle.job_id, handle.generation = job_id, generation
            gate.attach(handle)
            self.on_activity()
            return handle
        except Exception:
            gate.reserved = False
            raise

    def publish_result(self, result):
        if self.closed:
            return
        result = deepcopy(result)
        job_id = result.get('job_id') or self._delivery_ticket
        ticket = self._tickets.get(job_id)
        if ticket is None or ticket[0] != self.generation:
            self.log('AVISO', tr('shell.late_publication_discarded_due_to_an_incompatible'))
            return
        current = self.get_snapshot()['fingerprints']
        if current != ticket[1]:
            self.log('AVISO', tr('shell.publication_discarded_due_to_an_incompatible_fingerprint'))
            return
        result.update(job_id=job_id, generation=self.generation)
        result['freshness'] = 'current'
        result['stale'] = False
        result.setdefault("provenance", {})["ref_comparison"] = result.get("gui_validation_label", tr('shell.no_ref_validation'))
        self.results[result["module"]] = result
        self.on_result(deepcopy(result))

    def invalidate(self, reason):
        """Invalida resultados y desconecta mensajes de la generación anterior."""
        self.generation += 1
        self.worker.cancel()
        for result in self.results.values():
            result.update(stale=True, stale_reason=reason, freshness='stale')

    def close(self):
        self.closed = True
        self.worker.shutdown()
        self.results.clear()


def cancel_panel_timers(widget):
    """Cancela after de este subárbol antes de destruir widgets de un panel.

    Incluye los polls propios de ingestión protection. Los timers del resto de
    la aplicación se conservan mediante los comandos Tcl de cada widget.
    """
    commands = {}
    def collect(node):
        commands.update({command:node for command in (getattr(node, "_tclCommands", None) or ())})
        for child in node.winfo_children():
            collect(child)
    collect(widget)
    for ident in widget.tk.call("after", "info"):
        script, _ = widget.tk.call("after", "info", ident)
        words = widget.tk.splitlist(script)
        if words and words[0] in commands:
            commands[words[0]].after_cancel(ident)
