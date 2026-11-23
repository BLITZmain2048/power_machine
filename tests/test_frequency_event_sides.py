"""Derivación causal de fase a ambos lados de eventos, sin ajustar filtros."""
import cmath
import math

import numpy as np
import pytest

from power_machine.dynamics.measurements import (
    PhasorFrequencyHistory, PhaseLockedLoop, VoltageMeasurement)


@pytest.mark.parametrize('jump', [-.8, .25, 2.9])
def test_postevent_origin_and_held_frequency(jump):
    history = PhasorFrequencyHistory()
    omega = 2*math.pi*.013
    history.accept(0., {'b': 1+0j})
    assert history.accept(.01, {'b': cmath.exp(1j*omega*.01)})['b'] == pytest.approx(50.013)
    post = .9*cmath.exp(1j*(omega*.01+jump))
    assert history.accept(.01, {'b': post}, restart=True)['b'] == pytest.approx(50.013)
    assert history.accept(.011, {'b': post*cmath.exp(1j*omega*.001)})['b'] == pytest.approx(50.013)


def test_control_jump_at_end_of_accepted_step_never_enters_next_derivative():
    history = PhasorFrequencyHistory()
    history.accept(0., {'b': 1+0j})
    history.accept(.001, {'b': cmath.exp(-.00002j)})
    before = dict(history.frequency)
    history.accept(.001, {'b': cmath.exp(.4j)}, restart=True)
    assert history.frequency == before
    assert history.accept(.002, {'b': cmath.exp(.39998j)}) == pytest.approx(before)


def test_frequency_history_is_independent_of_output_decimation_and_node_indexes():
    history = PhasorFrequencyHistory()
    for i in range(10):
        result = history.accept(i*.001, {'a': cmath.exp(i*.00001j), 'b': 1+0j})
    assert result['a'] == pytest.approx(50+.01/(2*math.pi))
    history.accept(.009, {'b': 1+0j, 'a': 0j}, restart=True)
    assert history.frequency == result
    with pytest.raises(ValueError, match='time order'):
        history.accept(0., {'a': 1j})


def test_native_measurements_use_postevent_phasor_and_physical_states():
    # Ninguna depende de h ni del cociente V+/V-. El salto excita el PI/filtro
    # del TechRef; reiniciar esos estados para ocultarlo cambiaría el modelo.
    pll = PhaseLockedLoop('pll', 'pll.ElmPhi', 1, 100., 10., .9, 1.1)
    vm = VoltageMeasurement('vm', 'vm.StaVmea')
    xp, xv = pll.init(1+0j), vm.init(1+0j)
    post = .9*cmath.exp(.02j)
    assert pll.outputs(xp, post)['f_meas_hz'] == pytest.approx(50+100*.9*math.sin(.02)/(2*math.pi))
    assert vm.outputs(xv, post)['fe_hz'] == pytest.approx(50+math.sin(.02)/(3/50)/(2*math.pi))
    np.testing.assert_array_equal(xp, pll.init(1+0j))
    np.testing.assert_array_equal(xv, vm.init(1+0j))


@pytest.mark.skip(reason="Requiere un catálogo dinámico externo no incluido en IEEE 39")
def test_wecc_protection_receives_exported_voltage_measurement():
    from pathlib import Path
    from power_machine.dynamics.runner import RMSRunner
    from power_machine.dynamics.converters import WECCPVAdapter
    runner = RMSRunner(Path(__file__).resolve().parents[1], 'BASE_15s')
    pv = next(d for d in runner.devices if isinstance(d, WECCPVAdapter))
    index = runner.devices.index(pv)
    x = runner.state.x[runner.solver.x_slices[index]]
    v = runner.state.v[runner.network.index_of_bus(pv.bus_id)]
    before = pv.controller._signals(0., x, v)[2][2]['f']
    post = pv.controller._signals(0., x, v*cmath.exp(.01j))[2][2]['f']
    assert before == pytest.approx(1.)
    assert abs(post-before) > 1e-5
    from power_machine.dynamics.plant import MultiportPlant
    plant = next(d for d in runner.devices if isinstance(d, MultiportPlant))
    i = runner.devices.index(plant)
    px, pz = runner.state.x[runner.solver.x_slices[i]], runner.state.z[runner.solver.z_slices[i]]
    ports = runner.solver._ports(plant, runner.state.v)
    expected = plant._native(px, pz, ports)[0]
    plant.vmeas = {p: (*value[:3], 1) for p,value in plant.vmeas.items()}
    actual = plant._native(px, pz, ports)[0]
    assert actual == expected  # freqOutput=1 no multiplica fe por 50.

