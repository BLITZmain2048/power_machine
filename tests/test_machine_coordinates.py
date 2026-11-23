"""Proyección de estados de máquina con matrices sintéticas."""

import numpy as np
import pytest

from power_machine.dynamics.machine_coordinates import coordinate_projection


def test_coordinate_projection_preserves_structural_zero_and_order(tmp_path):
    path = tmp_path / "matrix.mtl"
    path.write_text("2 1 -4\n1 2 3\n", encoding="utf-8")
    result = coordinate_projection(path, [2, 1], [1, 2], mtl=True)
    np.testing.assert_array_equal(result, [[-4, 0], [0, 3]])


@pytest.mark.parametrize("body", ["1,2,,0\n", "1,2,nan,0\n", "1,2,3,0\n1,2,4,0\n"])
def test_coordinate_projection_rejects_missing_nonfinite_or_duplicate_entries(tmp_path, body):
    path = tmp_path / "matrix.csv"
    path.write_text("row,column,real,imag\n" + body, encoding="utf-8")
    with pytest.raises(ValueError):
        coordinate_projection(path, [1], [2])
