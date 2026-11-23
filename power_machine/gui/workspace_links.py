"""Rutas confinadas, contenido y vigencia de enlaces de estudio."""
from power_machine.gui.i18n import tr
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from power_machine.netedit.schema import value_sha256

from power_machine.resources import output_root

ROOT = output_root()
FINGERPRINT_SCHEMA = 'power_machine-calculation-fingerprint-v1'


def confined_path(path):
    path = Path(path)
    path = (path if path.is_absolute() else ROOT / path).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError(tr('integration.path_must_remain_within_the_project'))
    return path


def relative_path(path):
    return confined_path(path).relative_to(ROOT).as_posix()


def file_sha256(path):
    return hashlib.sha256(confined_path(path).read_bytes()).hexdigest()


def read_json(path):
    def reject(value):
        raise ValueError(tr('integration.nonfinite_json_number') + value)
    return json.loads(confined_path(path).read_text(encoding='utf-8-sig'), parse_constant=reject)


def calculation_sha256(dependencies):
    return value_sha256(dependencies)


def engine_sha256():
    """Identifica el código del paquete sin confundirlo con la salida del usuario."""
    from power_machine.resources import resource_root
    base = resource_root()
    return calculation_sha256({p.relative_to(base).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in sorted(base.rglob('*.py'))})


def check_link(link, dependencies=None, active_case_id=None):
    """No convierte un archivo íntegro en resultado vigente sin sus dependencias."""
    try:
        if file_sha256(link['result_path']) != link['result_file_sha256']:
            return 'stale'
        deps = link['dependencies']
        if deps.get('engine_source') == 'power_machine' and deps.get('engine_version_sha256') != engine_sha256():
            return 'stale'
        if calculation_sha256(deps) != link['calculation_sha256']:
            return 'stale'
        required = ('base_electrical_sha256', 'materialized_electrical_sha256', 'scenario_sha256',
                    'options_sha256', 'engine_id', 'engine_version_sha256', 'input_files', 'workspace_hashes')
        if any(k not in deps or deps[k] is None for k in required):
            return 'unknown'
        for item in deps['input_files']:
            if file_sha256(item['path']) != item['sha256']:
                return 'stale'
        if not deps['engine_id'] or not deps['engine_version_sha256']:
            return 'unknown'
        if active_case_id is not None and active_case_id != link['case_id']:
            return 'stale'
        if dependencies is None:
            return 'unknown'
        if any(deps.get(k) != v for k, v in dependencies.items()):
            return 'stale'
        return 'current'
    except FileNotFoundError:
        return 'missing'
    except (KeyError, ValueError, TypeError):
        return 'unknown'


def workspace(kind, content, bindings=None, workspace_id=None, revision=0):
    from uuid import uuid4
    if kind not in ('protection', 'dsl', 'types'):
        raise ValueError(tr('integration.unknown_workspace_class'))
    return dict(workspace_id=workspace_id or str(uuid4()), kind=kind,
                schema='power_machine-workspace-v1', revision=revision, content=deepcopy(content),
                content_sha256=value_sha256(content), bindings=deepcopy(bindings or {}))
