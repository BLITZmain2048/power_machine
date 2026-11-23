"""Entrada de escritorio portable."""
import multiprocessing
import time
from .i18n import tr


def create_application(root=None, selftest=False, *, attempts=3, pause_s=.5, factory=None, case=None):
    """Reintenta hasta tres veces ante el fallo intermitente de Tcl/Tk."""
    from .identity import initialize_windows_identity
    initialize_windows_identity()
    import tkinter as tk
    if factory is None:
        from .app import Application
        factory = Application
    last_error = None
    for number in range(attempts):
        try:
            options = {'selftest': selftest}
            if case is not None:
                options['case'] = case
            return factory(root, **options)
        except tk.TclError as exc:
            last_error = exc
            if number + 1 < attempts:
                time.sleep(pause_s)
    raise RuntimeError(tr('shell.tk_start_failed', attempts=attempts, error=str(last_error)))


def main(case=None):
    """Abre la red solicitada o IEEE 39."""
    multiprocessing.freeze_support()
    app = create_application(case=case or 'ieee39')
    app.mainloop()
    return app.exit_code


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=tr('shell.desktop_simulator'))
    parser.add_argument('case', nargs='?', default='ieee39')
    raise SystemExit(main(parser.parse_args().case))
