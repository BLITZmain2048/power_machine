"""Curvas DSL sintéticas y sus diagnósticos de identidad."""

import pytest

from power_machine.dynamics.dsl_compile import DSLCompileError, DynamicCatalog
from power_machine.dynamics.dsl_expr import linear_lookup


def _catalog(fields, matrix):
    catalog = object.__new__(DynamicCatalog)
    catalog.attributes = {"dsl": {"matrix": matrix}, "type": {"sParams": fields}}
    return catalog, {"object_id": "dsl", "relative_path": "curva_sintetica.ElmDsl"}, {"object_id": "type"}


def test_curve_headers_are_not_points_and_are_sorted_by_name():
    catalog, dsl, typ = _catalog(["array_z,array_a"], [
        [2, 0, 3, 99], [0, 0, -1, 2], [1, 0, 0, 3], [999, 999, 1, 4],
    ])
    assert catalog.curve_map(dsl, typ) == {
        "array_a": ((0.0, 0.0), (1.0, 0.0)),
        "array_z": ((-1.0, 2.0), (0.0, 3.0), (1.0, 4.0)),
    }
    assert linear_lookup(0.5, ((0.0, 0.0), (1.0, 1.0))) == pytest.approx(0.5)


@pytest.mark.parametrize("matrix", [[[2, 0], [0, 1], [0, 2]], [[float("nan"), 0], [0, 1], [1, 2]]])
def test_invalid_curve_identifies_the_dsl(matrix):
    catalog, dsl, typ = _catalog(["array_a"], matrix)
    with pytest.raises(DSLCompileError, match="curva_sintetica.ElmDsl"):
        catalog.curve_map(dsl, typ)
