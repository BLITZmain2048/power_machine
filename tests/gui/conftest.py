"""Aislamiento de salidas y marca común de las pruebas de escritorio."""
import os
from pathlib import Path
import sys
import time

import pytest

# La exclusión por marcador ocurre después de importar los módulos.
# Sin Tk, usar un colector que omita antes de esos imports.
try:
    import tkinter
except ImportError:
    TK_AVAILABLE = False
else:
    TK_AVAILABLE = True


class TkUnavailableModule(pytest.Module):
    def collect(self):
        pytest.skip('GUI omitida: tkinter no está instalado en este intérprete')


def pytest_pycollect_makemodule(module_path, parent):
    if not TK_AVAILABLE:
        return TkUnavailableModule.from_parent(parent, path=module_path)


sys.path.insert(0, str(Path(__file__).parent))


def pytest_collection_modifyitems(items):
    for item in items:
        if Path(__file__).parent in Path(item.path).parents:
            item.add_marker(pytest.mark.gui)


@pytest.fixture(autouse=True)
def isolated_outputs(tmp_path, monkeypatch):
    from power_machine.gui import workspace_links
    from tkinter import messagebox
    monkeypatch.setenv('POWER_MACHINE_OUTPUT_DIR', str(tmp_path))
    monkeypatch.setattr(workspace_links, 'ROOT', tmp_path)
    monkeypatch.setattr(messagebox, 'showinfo', lambda *args, **kwargs: None)
    monkeypatch.setattr(messagebox, 'showerror', lambda *args, **kwargs: None)
    monkeypatch.setattr(messagebox, 'askyesnocancel', lambda *args, **kwargs: False)
    yield
    import gc
    gc.collect()


@pytest.fixture
def tk_root():
    import tkinter as tk
    from power_machine.gui.paths import configure_tk_libraries
    configure_tk_libraries()
    for attempt in range(3):
        root = None
        try:
            root = tk.Tk()
            # This Windows Tk 8.6 runtime can return a partially initialized
            # interpreter. Resolve this lazily indexed command before export tests.
            if (sys.platform == 'win32' and str(root.tk.call('info','patchlevel')).startswith('8.6.')
                    and not root.tk.call('info','commands','::tk::ensure_psenc_is_loaded')
                    and not root.tk.call('auto_load','::tk::ensure_psenc_is_loaded')):
                raise tk.TclError('Incomplete Tk initialization: PostScript loader missing')
            break
        except tk.TclError as exc:
            if root is not None:
                try: root.destroy()
                except tk.TclError: pass
            if attempt == 2:
                pytest.skip(f'Entorno Tk no disponible tras tres intentos: {exc}')
            time.sleep(.2)
    root.geometry('1100x700')
    yield root
    root.destroy()


@pytest.fixture(scope='session', autouse=True)
def isolated_gui_settings(tmp_path_factory):
    previous = os.environ.get('POWER_MACHINE_CONFIG_DIR')
    os.environ['POWER_MACHINE_CONFIG_DIR'] = str(tmp_path_factory.mktemp('gui_settings'))
    from power_machine.gui.settings import settings
    old_path = settings.path
    settings.path = Path(os.environ['POWER_MACHINE_CONFIG_DIR'])/'settings.json'
    settings.load()
    yield
    settings.path = old_path
    settings.load()
    if previous is None: os.environ.pop('POWER_MACHINE_CONFIG_DIR', None)
    else: os.environ['POWER_MACHINE_CONFIG_DIR'] = previous
