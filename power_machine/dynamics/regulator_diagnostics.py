'Regulator diagnostics support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import csv
import re
from pathlib import Path

import numpy as np
from .state_coordinates import state_transform as dyn_d_transform


def sparse_projection(path, rows, columns=None):
    'Read a selected sparse rectangle with one-based REF indices. Omitted coordinates are zero. Reject empty real or imaginary fields on present coordinates.'
    result = {}
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        for r in csv.DictReader(handle):
            i, j = int(r["row"]), int(r["column"])
            if i not in rows or (columns is not None and j not in columns):
                continue
            if not r["real"].strip() or not r["imag"].strip():
                raise _coded_error(ValueError, 'invalid_regulator_diagnostics_data', f'Empty REF coordinate: {path}, {i}, {j}')
            z = complex(float(r["real"]), float(r["imag"]))
            if not np.isfinite(z):
                raise _coded_error(ValueError, 'invalid_regulator_diagnostics_data', 'REF coordinate must be finite')
            if (i, j) in result:
                raise _coded_error(ValueError, 'invalid_regulator_diagnostics_data', 'Duplicate REF coordinate')
            result[i, j] = z
    return result


def variable_map(path):
    'Map indices to model and state labels without printing the full map.'
    result = {}
    for line in Path(path).read_text(encoding="utf-8-sig").splitlines():
        match = re.match(r'\s*(\d+)\s+(.+?)\s+"([^"]+)"\s*$', line)
        if match:
            i, model, state = match.groups()
            result[int(i)] = (model, state)
    return result


def ref_short(path):
    'Return the shortened REF path while retaining identity in the original manifest.'
    return "Grid\\"+path.split("Grid.ElmNet\\", 1)[1].replace(".ElmComp", "")


def coordinate_transform(rms, ref_states, *, q_permutation=False):
    'Map engine states to REF coordinates: xa=Vr, xe=Efd, xf=Xf, xr=Vm and x0=-K*q. A constant load-state shift does not change A. Optional q-loop permutation is diagnostic only.'
    t, mapping = dyn_d_transform(rms, ref_states)
    for i, r in enumerate(ref_states):
        if "\\AVR " in r["model"] and r["state"] == "xf":
            j = int(np.flatnonzero(t[i])[0])
            t[i, j] = 1.0
            mapping[i]["scale_ref_per_pm"] = 1.0
            mapping[i]["coordinate_status"] = 'Local regulator coordinate, xf=Xf'
        if q_permutation and r["state"] in ("psi1q", "psi2q"):
            counterpart = "psi2q" if r["state"] == "psi1q" else "psi1q"
            partner = next((k for k, s in enumerate(ref_states)
                            if s["model"] == r["model"] and s["state"] == counterpart), None)
            if partner is not None:
                j = mapping[partner]["pm_index"]
                t[i] = 0.0
                t[i, j] = 1.0
    return t, mapping


def mapped_matrix(a, t):
    'Apply a similarity transform with linear solves, without an explicit inverse.'
    return np.linalg.solve(t.T, (t@a).T).T


def participation_shares(values):
    'Normalize participation magnitudes, separately from the complex REF sum.'
    denominator = sum(abs(z) for z in values.values())
    if denominator == 0:
        raise _coded_error(ValueError, 'invalid_regulator_diagnostics_data', 'Mode has no exported participation')
    return {k: abs(z)/denominator for k, z in values.items()}
