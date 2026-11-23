"""Identidad de proceso y recursos de marca opcionales para todas las ventanas."""
import sys
from pathlib import Path


def initialize_windows_identity():
    if sys.platform == 'win32':
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID('PowerMachine.Desktop.1')
        except Exception:
            pass


def apply_window_icon(window, assets=None):
    import tkinter as tk
    assets = Path(assets) if assets is not None else Path(__file__).parent / 'assets'
    try:
        if (assets / 'logo_256.png').is_file():
            window._power_machine_icon = tk.PhotoImage(master=window, file=str(assets / 'logo_256.png'))
            window.iconphoto(True, window._power_machine_icon)
        if sys.platform == 'win32' and (assets / 'logo.ico').is_file():
            window.iconbitmap(str(assets / 'logo.ico'))
    except (tk.TclError, OSError):
        pass


def register_window_icons(root):
    apply_window_icon(root)
    def mapped(event):
        import tkinter as tk
        if isinstance(event.widget, tk.Toplevel) and not getattr(event.widget, '_pm_icon_applied', False):
            event.widget._pm_icon_applied = True
            apply_window_icon(event.widget)
    root.bind_all('<Map>', mapped, add='+')
