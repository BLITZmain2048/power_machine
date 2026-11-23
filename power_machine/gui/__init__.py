"""Interfaz de escritorio del simulador power_machine."""
import os

# También se ejecuta en el hijo spawn, antes de importar numpy.
for _variable in ('OPENBLAS_NUM_THREADS', 'OMP_NUM_THREADS', 'MKL_NUM_THREADS'):
    os.environ[_variable] = '1'

# También prepara Tk cuando se utiliza el diagrama como componente independiente.
from .paths import configure_tk_libraries
configure_tk_libraries()


def main(case=None):
    """Inicia la interfaz con una red, por defecto IEEE 39."""
    from .__main__ import main as launch
    return launch(case)
