"""Órdenes reversibles de la aplicación y lectura de corridas guardadas."""
import csv
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from .plots import SignalBuffer


@dataclass
class SnapshotCommand:
    """Orden reversible de un dominio, con snapshots privados."""
    restore: object
    before: object
    after: object

    def undo(self):
        self.restore(deepcopy(self.before))

    def redo(self):
        self.restore(deepcopy(self.after))


class History:
    """Pilas por dominio. La pestaña activa decide qué dominio recorrer."""
    def __init__(self):
        self._undo, self._redo = {}, {}

    def record(self, domain, command):
        self._undo.setdefault(domain, []).append(command)
        self._redo[domain] = []

    def can_step(self, domain, *, redo=False):
        return bool((self._redo if redo else self._undo).get(domain))

    def export_domain(self, domain, encode=deepcopy):
        """Exporta datos de órdenes, nunca funciones ni widgets."""
        return {name: [dict(before=encode(c.before), after=encode(c.after))
                       for c in stacks.get(domain, [])]
                for name, stacks in (('undo', self._undo), ('redo', self._redo))}

    def load_domain(self, domain, data, restore, decode=deepcopy):
        """Reemplaza un dominio tras decodificar ambas pilas completas."""
        stacks = {name: [SnapshotCommand(restore, decode(c['before']), decode(c['after']))
                         for c in data[name]] for name in ('undo', 'redo')}
        self._undo[domain], self._redo[domain] = stacks['undo'], stacks['redo']

    def step(self, domain, *, redo=False):
        source, target = (self._redo, self._undo) if redo else (self._undo, self._redo)
        stack = source.get(domain, [])
        if not stack:
            return False
        command = stack[-1]
        (command.redo if redo else command.undo)()
        stack.pop()
        target.setdefault(domain, []).append(command)
        return True


from .network_session import NetworkSession


def discover_runs(directory):
    return [dict(name=p.parent.name, path=p) for p in sorted(Path(directory).glob('*/signals.csv'), reverse=True)]


def read_signals(path):
    result = SignalBuffer()
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        reader = csv.reader(stream)
        keys, indices = signal_columns(path, next(reader))
        result.reset(keys)
        result.append(([row[i] for i in indices] for row in reader))
    return result


def signal_columns(path, columns):
    """Adapta CSV clásico sin convertir sample_side ni ausentes en números.

    Se conservan las dos muestras de cada evento. La frecuencia clásica
    recibe su propia variable y nunca se identifica como m:fehz de REF.
    """
    if columns[:3] != ['time_s', 'sample_side', 'coi_frequency_hz']:
        return list(columns), list(range(len(columns)))
    report = json.loads((Path(path).parent / 'run_report.json').read_text(encoding='utf-8-sig'))
    keys, indices = ['time|b:tnow|s', 'IEEE39|classical:coi_frequency|Hz'], [0, 2]
    for index, column in enumerate(columns[3:], 3):
        ref = report['columns'][column]
        variable = column.removeprefix(ref['object_id'] + '_')
        keys.append(f"{ref['relative_path']}|classical:{variable}|{ref['unit']}")
        indices.append(index)
    return keys, indices
