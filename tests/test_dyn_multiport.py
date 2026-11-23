"""Jacobiana multipuerto frente a diferencias del residuo DAE completo."""
import numpy as np
import pytest
from pathlib import Path

from power_machine.dynamics.interfaces import finite_difference_jacobian, rect_to_complex
from power_machine.dynamics.network import DynamicNetwork
from power_machine.dynamics.solver import DAESolver, DAEState
from test_dyn_solver import model_for


def test_remote_measurements_and_two_injections_enter_full_jacobian():
    class Plant:
        object_id = 'plant'
        n_states, n_alg = 1, 1
        read_bus_ids = ('a', 'c')
        read_bus_current_ids = ('c',)
        read_branch_ids = ('ab',)
        injection_bus_ids = ('a', 'c')

        def f_multi(self, t, x, z, p):
            return np.array([-x[0]+z[0]+p.branch_power_pu['ab', 1].real
                             +p.bus_current_pu['c'].imag])

        def g_multi(self, t, x, z, p):
            return np.array([z[0]-abs(p.bus_voltage_pu['c'])**2-x[0]])

        def current_injections_multi(self, x, z, p):
            return {'a': complex(x[0], z[0])*p.bus_voltage_pu['c'],
                    'c': complex(z[0], -x[0])*p.bus_voltage_pu['a']}

    network = DynamicNetwork(model_for('a', 'b', 'c',
        lines=(('ab', 'a', 'b', .2), ('bc', 'b', 'c', .3))))
    solver = DAESolver(network, [Plant()])
    state = DAEState(0., np.array([.12]), np.array([.4]),
                     np.array([1+.03j, .98-.02j, 1.02+.01j]))
    for dynamic in (False, True):
        point = np.r_[state.x if dynamic else np.empty(0), state.z,
                      state.v.real, state.v.imag]
        def residual(values):
            offset = int(dynamic)
            x = values[:1] if dynamic else state.x
            f, g, net = solver._evaluate(.001, x, values[offset:offset+1],
                                        rect_to_complex(values[offset+1:]))
            return np.r_[x-state.x-.001*f if dynamic else np.empty(0), g, net]
        reference = finite_difference_jacobian(residual, point)
        local = solver._local_jacobian(.001, state.x, state.z, state.v,
            old=state if dynamic else None, h=.001, f_old=np.zeros(1),
            method='euler' if dynamic else 'algebraic')
        np.testing.assert_allclose(local, reference, rtol=2e-7, atol=2e-8)
        # La medida de corriente de c añade dependencia del nodo remoto b.
        assert np.count_nonzero(abs(local[0]) > 1e-8) >= 3


@pytest.mark.skip(reason="Requiere un catálogo dinámico externo no incluido en IEEE 39")
def test_grouped_plant_jacobian_matches_individual_columns():
    from power_machine.dynamics.runner import RMSRunner
    runner = RMSRunner(Path(__file__).resolve().parents[1], 'BASE_15s')
    state = runner.state.copy()
    rng = np.random.default_rng(20260926)
    for i in range(3):
        if i:
            state.x += rng.normal(0., 1e-4, len(state.x))
            state.z += rng.normal(0., 1e-4, len(state.z))
            state.v += rng.normal(0., 1e-4, len(state.v))
        results = []
        for grouped in (False, True):
            runner.solver.grouped_multiport_differences = grouped
            results.append(runner.solver._local_jacobian(.001,
                state.x, state.z, state.v, old=state, h=.001,
                f_old=np.zeros(len(state.x)), method='trapezoidal'))
        np.testing.assert_allclose(*results, rtol=2e-7, atol=2e-8)
    assert any(len(groups) < len(supports)
               for groups, supports in runner.solver._difference_groups.values())

