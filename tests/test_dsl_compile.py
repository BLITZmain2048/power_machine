"""Regresiones de compilación DSL con entradas sintéticas explícitas."""

import pytest

from power_machine.dynamics.dsl_compile import DSLCompileError, DynamicCatalog
from power_machine.dynamics.dsl_expr import evaluate, linear_lookup, parse_expression


def test_linear_lookup_interpolates_and_rejects_non_monotonic_axis():
    curve = ((0.0, -0.71), (0.25, -0.56), (0.5, -0.43))
    assert linear_lookup(0.125, curve) == pytest.approx(-0.635)
    assert linear_lookup(-1.0, curve) == pytest.approx(-0.71)
    assert linear_lookup(2.0, curve) == pytest.approx(-0.43)
    assert linear_lookup(-0.635, curve, inverse=True) == pytest.approx(0.125)
    with pytest.raises(ValueError, match="strictly increasing"):
        linear_lookup(0.1, ((0.0, 1.0), (0.0, 2.0)))


def test_expression_with_curve_and_division_has_explicit_diagnostics():
    expression = parse_expression("lapprox(0.125,array)")
    assert evaluate(expression, {"array": ((0.0, 0.0), (0.25, 1.0))}) == pytest.approx(0.5)
    with pytest.raises(ZeroDivisionError):
        evaluate(parse_expression("a/b"), {"a": 1.0, "b": 0.0})


def test_curve_map_ignores_headers_and_rejects_invalid_rows():
    catalog = object.__new__(DynamicCatalog)
    catalog.attributes = {"dsl": {"matrix": [[2, 0, 3, 99], [0, 0, -1, 2],
                                                  [1, 0, 0, 3], [999, 999, 1, 4]]},
                          "type": {"sParams": ["array_a"]}}
    dsl = {"object_id": "dsl", "relative_path": "synthetic.ElmDsl"}
    typ = {"object_id": "type"}
    assert catalog.curve_map(dsl, typ) == {"array_a": ((0.0, 0.0), (1.0, 0.0))}
    catalog.attributes["dsl"]["matrix"] = [[2, 0], [0, 1], [0, 2]]
    with pytest.raises(DSLCompileError, match="synthetic.ElmDsl"):
        catalog.curve_map(dsl, typ)
