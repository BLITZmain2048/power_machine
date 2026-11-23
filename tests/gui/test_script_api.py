"""Consola de guiones con la API pública y copia de la red abierta."""
from copy import deepcopy
from queue import Queue

from power_machine.gui.scripting import _run_script


def messages(queue):
    result = []
    while not queue.empty():
        result.append(queue.get_nowait())
    return result


def test_console_exposes_api_and_independent_network(red_sintetica):
    before = deepcopy(red_sintetica)
    queue = Queue()
    _run_script(red_sintetica,
        "print(net.summary()['counts']['buses'])\n"
        "net.set('B1', name='Barra editada')\n"
        "print(api.Network.__name__)\n", queue)
    result = messages(queue)
    assert result[-1][0] == 'done'
    assert '3' in ''.join(text for kind, text in result if kind == 'output')
    assert 'Network' in ''.join(text for kind, text in result if kind == 'output')
    assert red_sintetica == before


def test_console_returns_script_error_without_raising(red_sintetica):
    queue = Queue()
    _run_script(red_sintetica, "raise ValueError('Error de ensayo')", queue)
    result = messages(queue)
    assert result[-1][0] == 'error'
    assert 'Error de ensayo' in result[-1][1]
