"""Carga de redes para el escritorio durante la integración de la API."""
from pathlib import Path


def load_case(case):
    """Prefiere la fachada pública y conserva carga directa de modelos."""
    try:
        from power_machine import api
    except ImportError:
        api = None
    if hasattr(api, 'load_case'):
        return api.load_case(case)
    from power_machine.resources import ieee39_root
    from power_machine.study import load_model
    path = ieee39_root() / 'model.json' if str(case).lower() == 'ieee39' else Path(case).resolve()
    if path.is_dir() and not (path / 'model.json').exists():
        return import_powerfactory(path)
    return load_model(path.parent, path) if path.is_file() else load_model(path)


def import_powerfactory(folder):
    """Importa CSV mediante la función pública disponible del motor."""
    try:
        from power_machine import api
    except ImportError:
        api = None
    if hasattr(api, 'import_powerfactory'):
        return api.import_powerfactory(folder)
    from power_machine.builder import build_model
    return build_model(folder)


def dynamic_directory(model):
    """Devuelve la entrada dinámica explícita de la red o informa su ausencia."""
    raw = model.get('_dynamic_dir')
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    return path if (path / 'manifest.json').is_file() else None
