"""Equivalencia de las rutas rápidas de máquina, DSL y Newton."""

from pathlib import Path
import os

import numpy as np
import pytest

from power_machine.dynamics.dsl_codegen import compile_expression
from power_machine.dynamics.dsl_expr import (Call, DSLMemory, Name, UnsupportedFunction,
                                         evaluate, parse_expression)
from power_machine.dynamics.dsl_compile import CompiledDSL
from power_machine.dynamics.runner import RMSRunner
from power_machine.dynamics.solver import DAESolver
@pytest.mark.skip(reason="Requiere un inventario dinámico externo que no forma parte del caso IEEE 39 incluido")
def test_closed_rotor_loops_match_dense_solve_on_twelve_machines():
    from test_dyn_synchronous import _lf_machines
    rng = np.random.default_rng(20260925)
    for gen, un_kv, _, _ in _lf_machines():
        from power_machine.dynamics.synchronous import SynchronousMachine
        machine = SynchronousMachine(gen, bus_un_kv=un_kv)
        c = machine.circuit
        for _ in range(8):
            psi = rng.normal(size=4)
            id_pu, iq_pu = rng.normal(size=2)
            sat_d, sat_q = rng.uniform(0.5, 1.0, size=2)
            result = machine._axis(*psi[:3], psi[3] if c.turbo else None,
                                   id_pu, iq_pu, sat_d, sat_q)
            xad, xaq = c.xadu*sat_d, c.xaqu*sat_q
            ld = np.array([[xad+c.xrl+c.xfd, xad+c.xrl],
                           [xad+c.xrl, xad+c.xrl+c.x1d]])
            expected_d = np.linalg.solve(ld, psi[:2]+xad*id_pu)
            np.testing.assert_allclose([result["ifd"], result["i1d"]], expected_d,
                                       rtol=2e-13, atol=2e-13)
            if c.turbo:
                lq = np.array([[xaq+c.xrlq+c.x1q, xaq+c.xrlq],
                               [xaq+c.xrlq, xaq+c.xrlq+c.x2q]])
                expected_q = np.linalg.solve(lq, psi[2:4]+xaq*iq_pu)
                np.testing.assert_allclose([result["i1q"], result["i2q"]], expected_q,
                                           rtol=2e-13, atol=2e-13)
            else:
                assert result["i1q"] == pytest.approx(
                    (psi[2]+xaq*iq_pu)/(xaq+c.xrlq+c.x1q), abs=2e-13)


def test_generated_ast_matches_interpreter_and_preserves_memory():
    values = {"x": -0.4, "y": 1.2, "curve": ((0.0, 0.0), (2.0, 4.0))}
    expressions = ["x+y*2", "x^2", "lim(x,-1,1)", "select(x>0,1/y,y)",
                   "x<y .and. y>0", "x>y .or. y>0", "lapprox(y,curve)",
                   "picontrol_const(x,-1,1,y)", "time()"]
    for source in expressions:
        expression = parse_expression(source)
        generated = compile_expression(expression)
        assert generated(values, 0.5, None) == pytest.approx(
            evaluate(expression, values, time_s=0.5))
    memory_expression = parse_expression("delay(y,0.1)")
    memory = DSLMemory()
    generated = compile_expression(memory_expression)
    assert generated(values, 0.5, memory) == pytest.approx(
        evaluate(memory_expression, values, time_s=0.5, runtime=memory))
    assert memory.records == {}
    memory.begin_accept()
    assert generated(values, 0.5, memory) == pytest.approx(1.2)
    memory.finish_accept()
    assert len(memory.records) == 1


def test_codegen_rejects_unlisted_calls_and_treats_names_as_keys():
    with pytest.raises(UnsupportedFunction):
        compile_expression(Call("__import__", (Name("os"),)))
    strange = Name("__import__('os').system('invalid')")
    assert compile_expression(strange)({strange.value: 7.0}, 0.0, None) == 7.0


def test_history_queries_match_original_linear_reference():
    from bisect import bisect_right

    def old_sample(history, target):
        if target <= history[0][0]:
            return history[0][1]
        index = bisect_right([row[0] for row in history], target)
        if index == len(history):
            return history[-1][1]
        t0, y0 = history[index-1]
        t1, y1 = history[index]
        return y0+(target-t0)*(y1-y0)/(t1-t0)

    memory = DSLMemory()
    accepted = []
    for index in range(300):
        time_s = index*0.001
        value = float(np.sin(index*0.05))
        trial = accepted+[(time_s, value)] if accepted else [(time_s, value)]
        if index:
            delayed = old_sample(trial, time_s-0.023)
            previous = accepted[-1][1] if index else value
            end, start = time_s-0.011, time_s-0.048
            knots = [start]+[t for t, _ in trial if start<t<end]+[end]
            samples = [old_sample(trial, t) for t in knots]
            moving = sum((b-a)*(u+v)/2 for a,b,u,v in
                         zip(knots,knots[1:],samples,samples[1:]))/0.037
            assert memory.read(1, "delay", time_s, (value, 0.023)) == pytest.approx(delayed, abs=1e-13)
            assert memory.read(2, "lastvalue", time_s, (value,)) == pytest.approx(previous, abs=1e-13)
            assert memory.read(3, "movingavg", time_s, (value, 0.011, 0.037)) == pytest.approx(moving, abs=1e-13)
        memory.begin_accept()
        memory.read(1, "delay", time_s, (value, 0.023))
        memory.read(2, "lastvalue", time_s, (value,))
        memory.read(3, "movingavg", time_s, (value, 0.011, 0.037))
        memory.finish_accept()
        accepted.append((time_s, value))


def test_newton_reuses_accepted_trial_residual():
    class Network:
        node_ids = ("bus",)
        ybus = np.array([[1+0j]])

        @staticmethod
        def index_of_bus(bus_id):
            return 0

    solver = DAESolver(Network(), [], max_iter=5)
    evaluations = []

    def residual(u):
        evaluations.append(float(u[0]))
        return np.array([u[0]-2.0])

    value, iterations, _, _ = solver._newton(
        residual, np.array([0.0]), np.array([1e-8]),
        lambda u: np.array([[1.0]]))
    assert iterations == 1
    assert value[0] == pytest.approx(2.0)
    assert evaluations == [0.0, 2.0]


def set_fast_graph(runner, enabled):
    """Configure compiled DSL graphs, nested templates and cached children."""
    seen = set()
    def visit(value):
        if value is None or id(value) in seen:
            return
        seen.add(id(value))
        if isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)
        else:
            if hasattr(value, "fast_graph"):
                value.fast_graph = bool(enabled)
                value._flat_plan = None
            for name in ("devices", "avr", "governor", "frame_blocks", "blocks", "nodes", "subgraph", "_children"):
                visit(getattr(value, name, None))
    visit(runner)


def test_fast_graph_configuration_visits_nested_and_shared_blocks():
    from types import SimpleNamespace
    child = SimpleNamespace(fast_graph=True, _flat_plan=object())
    parent = SimpleNamespace(fast_graph=True, _flat_plan=object(),
                             nodes=[SimpleNamespace(subgraph=child)], _children={"child": child})
    runner = SimpleNamespace(devices=[SimpleNamespace(avr=parent, blocks={"shared": child})])
    set_fast_graph(runner, False)
    assert not parent.fast_graph and not child.fast_graph
    assert parent._flat_plan is None and child._flat_plan is None
    set_fast_graph(runner, True)
    assert parent.fast_graph and child.fast_graph


@pytest.mark.skip(reason="Requiere un catálogo dinámico externo y una maniobra no incluida")
def test_load_p10_02_s_matches_dsl_interpreter(monkeypatch, tmp_path):
    def trajectory(label):
        runner = RMSRunner(Path(os.environ["POWER_MACHINE_TEST_DYNAMIC_ROOT"]), "LOAD_P10")
        # Deshabilita tambien los grafos de macros anidadas para el interprete.
        if label == "interpreted":
            set_fast_graph(runner, False)
        # Ambos recorridos incluyen los flancos PPC previos a la apertura.
        states = []
        original_step = runner.solver.step

        def record(state, step_s=None):
            result = original_step(state, step_s)
            states.append(result[0].copy())
            return result

        runner.solver.step = record
        report = runner.run(end_s=1.2, output=tmp_path / label, output_every=10)
        assert report["failure"] is None
        # PF-04: h/2 y 2h/3 tras t=1; el aterrizaje final añade un paso.
        assert len(states) == 1301
        after = [s.t for s in states if s.t > 1.+1e-10]
        np.testing.assert_allclose(after[:2], [1.0005, 1.+.001/2+2*.001/3], atol=1e-12)
        return states

    optimized = trajectory("generated")

    def interpreted(self, expr, scope, t_s=None):
        return evaluate(expr, scope, time_s=t_s, runtime=self.memory)

    monkeypatch.setattr(CompiledDSL, "_eval", interpreted)
    reference = trajectory("interpreted")
    max_voltage_pu = max(float(np.max(abs(a.v-b.v))) for a, b in zip(optimized, reference))
    max_state = max(float(np.max(abs(a.x-b.x))) for a, b in zip(optimized, reference))
    print(f"study LOAD_P10 0.2 s: max abs(V) difference={max_voltage_pu:.3e} p.u.; "
          f"max abs(x) difference={max_state:.3e}")
    assert max_voltage_pu <= 1e-9
    assert max_state <= 1e-9

