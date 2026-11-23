"""Rutas de datos en desarrollo y distribución congelada."""
import re
import os
import sys
from datetime import datetime
from pathlib import Path

from power_machine.resources import resource_root, writable_root


def configure_tk_libraries():
    """Localiza Tcl del mismo intérprete en desarrollo, sin afectar PyInstaller.

    Las rutas relativas evitan el fallo de resolución de Tcl en el entorno
    Windows restringido. No se copian bibliotecas ni se cambia la instalación.
    """
    if sys.platform != 'win32' or getattr(sys, 'frozen', False):
        return
    for key, folder in (('TCL_LIBRARY', 'tcl8.6'), ('TK_LIBRARY', 'tk8.6')):
        path = Path(sys.base_prefix) / 'tcl' / folder
        if path.is_dir() and key not in os.environ:
            try:
                os.environ[key] = Path(os.path.relpath(path, Path.cwd())).as_posix()
            except ValueError:
                os.environ[key] = str(path)


def data_root(root=None):
    return Path(root).resolve() if root is not None else resource_root().parent


def results_root(root=None):
    return writable_root()


def run_directory(root, name):
    safe = re.sub(r'[^\w.-]+', '_', name).strip(' ._')[:80] or 'caso'
    path = results_root(root) / (safe + datetime.now().strftime('_%Y%m%d_%H%M%S_%f'))
    path.mkdir()
    return path
