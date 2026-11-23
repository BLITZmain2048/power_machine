"""Parser e IR DSL con bloques sintéticos y sin catálogos externos."""

import pytest

from power_machine.dynamics.blocks import FirstOrderLag, Integrator
from power_machine.dynamics.dsl_expr import ParseError, UnsupportedFunction, evaluate, parse_equations, parse_expression
from power_machine.dynamics.dsl_ir import BlockIR, BlockInstance, Connection, MacroIR


def test_ast_evaluation_and_block_initialization_are_safe():
    statements = parse_equations(["limits(T)=[0,)", "inc(x)=yi", "x.=(yi-x)/T", "yo=x"])
    assert len(statements) == 4
    block = BlockIR.from_fields("synthetic", "Synthetic.BlkDef", {
        "sInput": ["yi"], "sOutput": ["yo"], "sStates": ["x"],
        "sParams": ["T"], "sAddEquat": ["x.=(yi-x)/T", "yo=x", "inc(x)=yi"],
    })
    scope, derivative = block.evaluate({"yi": 2.0, "x": 1.0, "T": 0.5})
    assert scope["yo"] == 1.0
    assert derivative["x"] == 2.0
    assert block.initialize({"yi": 2.0})["x"] == 2.0
    with pytest.raises(UnsupportedFunction):
        evaluate(parse_expression("unsupported(yi)"), {"yi": 1.0})
    with pytest.raises(ParseError):
        parse_expression("__import__('os').system('x')")


def test_synthetic_avr_macro_solves_backward_equilibrium():
    macro = MacroIR("synthetic", "Synthetic.AVR", (
        BlockInstance("lag", "Synthetic.AVR.lag", FirstOrderLag(0.1, 1.0)),
        BlockInstance("integrator", "Synthetic.AVR.integrator", Integrator(10.0, 0.0, 5.0)),
    ), (
        Connection("error", "lag"), Connection("lag", "integrator"), Connection("integrator", "field"),
    ), ("error",), ("field",))
    inputs, states = macro.initialize_backward({"field": 2.0})
    assert inputs == {"error": 0.0}
    assert states == {"lag": 0.0, "integrator": 2.0}
