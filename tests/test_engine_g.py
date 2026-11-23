"""Regression of bolted faults, explicit frequency and results compatibility."""
from pathlib import Path

import numpy as np
import pytest

from power_machine.dynamics.events import Event, EventTimeline
from power_machine.benchmarks.ieee39_pf import build_rms_case
from power_machine.study import SimEvent, _translate_event


def ieee39_fault_run(impedance):
    rms = build_rms_case(step_s=.01)
    bus = next(b for b in rms.model['buses'] if b['name'] == 'Bus 16')
    event = _translate_event(SimEvent(.02, 'short_circuit', bus['id'],
                            {'r_ohm': impedance.real, 'x_ohm': impedance.imag}),
                            {bus['id']: bus}, 100.)
    run = EventTimeline([event, Event(.06, 'fault_clear', bus['id'])]).run(
        rms.solver, rms.initial, .10)
    return rms, bus, run


def test_ieee39_bolted_fault_converges_without_surrogate_impedance():
    rms, bus, solid = ieee39_fault_run(0j)
    i = rms.network.index_of_bus(bus['id'])
    faulted = [s for s in solid.samples if .02 < s.t < .06]
    assert max(abs(s.v[i]) for s in faulted) < 1e-12
    assert abs(solid.samples[-1].v[i]) > .5
    for jump in solid.discontinuities:
        np.testing.assert_array_equal(solid.samples[jump['before_index']].x,
                                      solid.samples[jump['after_index']].x)
    errors = []
    for z in (complex(1e-3, 1e-3), complex(1e-5, 1e-5)):
        _, _, small = ieee39_fault_run(z)
        assert len(small.samples) == len(solid.samples)
        errors.append(max(float(np.max(abs(a.v-b.v)))
                          for a,b in zip(solid.samples, small.samples)))
        assert np.isfinite(small.samples[-1].x).all()
    assert errors[1] < errors[0] * .02
    assert errors[1] < 1e-5


@pytest.mark.parametrize('z', [complex(-1, 0), complex(float('nan'), 0), complex(0, float('inf'))])
def test_fault_impedance_validation(z):
    rms = build_rms_case()
    bus = rms.model['buses'][0]
    with pytest.raises(ValueError):
        rms.network.set_fault(bus['id'], z)
    with pytest.raises(ValueError):
        _translate_event(SimEvent(0., 'short_circuit', bus['id'],
                         {'r_ohm': z.real, 'x_ohm': z.imag}), {bus['id']: bus}, 100.)


def test_results_directory_legacy_compatibility(tmp_path, monkeypatch):
    from power_machine import resources
    monkeypatch.delenv(resources.RESULTS_ROOT_ENV, raising=False)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    assert resources.output_root() == tmp_path/'power_machine_results'
    legacy = tmp_path/'power_machine_resultados'
    legacy.mkdir()
    assert resources.output_root() == legacy
    current = tmp_path/'power_machine_results'
    current.mkdir()
    assert resources.output_root() == current


def test_import_frequency_warning_and_explicit_value(monkeypatch):
    from power_machine import builder
    from power_machine.io_pf import Package
    package = Package(Path('.'), {}, {}, {}, {}, [])
    monkeypatch.setattr(builder, 'load_package', lambda *args, **kwargs: package)
    with pytest.warns(UserWarning, match='Nominal frequency'):
        model = builder.build_model('.')
    assert model['meta']['f_nom_hz'] == 50.
    assert any('Nominal frequency' in w for w in model['warnings'])
    assert builder.build_model('.', f_nom_hz=60.)['meta']['f_nom_hz'] == 60.
    for frequency in (0, -1, float('nan'), float('inf')):
        with pytest.raises(ValueError, match='positive and finite'):
            builder.build_model('.', f_nom_hz=frequency)


def test_zero_voltage_signals_are_undefined():
    from types import SimpleNamespace
    from power_machine.dynamics.runner import RMSRunner
    runner = object.__new__(RMSRunner)
    runner.network = SimpleNamespace(bus_voltages=lambda v: {'b': v[0]},
                                     topology={'active': {'branches': []}})
    runner.buses = {'b': {'path': 'bus'}}
    runner.monitored = {'bus|m:u|p.u.', 'bus|m:phiu|deg', 'bus|m:fehz|Hz'}
    runner.devices = []
    runner.solver = SimpleNamespace(x_slices=[], z_slices=[])
    runner.model = {'meta': {'f_nom_hz': 60.}, 'branches': []}
    runner.external_signals = False
    runner.state = SimpleNamespace(x=np.zeros(1))
    runner.reference_angle_index = 0
    state = SimpleNamespace(t=.1, v=np.zeros(1, dtype=complex), x=np.zeros(1))
    values = runner._signals(state, None)
    assert values['bus|m:u|p.u.'] == 0.
    assert np.isnan(values['bus|m:phiu|deg'])
    assert np.isnan(values['bus|m:fehz|Hz'])


@pytest.mark.parametrize('frequency', [50., 60., 16.7])
def test_modal_profile_uses_declared_frequency(tmp_path, monkeypatch, frequency):
    from power_machine.modal import results
    called = []
    def prepare(root, model, data):
        called.append(model['meta']['f_nom_hz'])
        raise ValueError('Reached adapter')
    monkeypatch.setattr(results, 'prepare_machine', prepare)
    model = {'meta': {'s_base_mva': 100., 'f_nom_hz': frequency}}
    study = {}
    request = {'schema': 'pm-parity-request-v1', 'module': 'modal', 'root': str(tmp_path),
               'snapshot': {'model': model, 'study': study, 'fingerprints': {
                   'model_sha256': results.fingerprint(model),
                   'study_sha256': results.fingerprint(study)}},
               'input': {'profile': 'machine_admittance'}}
    result = results.execute_request(request)
    assert called == [frequency]
    assert not any(d.get('code') == 'electrical_base' for d in result['diagnostics'])
