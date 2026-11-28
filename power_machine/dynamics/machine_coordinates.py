'Machine coordinates support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import csv
import re
from pathlib import Path
import numpy as np


def read_ref_states(root):
    'Match states.csv labels against VariableToIdx_Amat.txt. Amat.mtl contains sparse numeric coordinates, not state labels.'
    root = Path(root)
    with (root/"states.csv").open(encoding="utf-8-sig", newline="") as handle:
        states = list(csv.DictReader(handle))
    pattern = re.compile(r'^\s*(\d+)\s+(.+?)\s+"([^"]+)"\s*$')
    native = []
    with (root/"VariableToIdx_Amat.txt").open(encoding="utf-8-sig") as handle:
        for line in handle:
            match = pattern.match(line)
            if match:
                native.append((int(match[1]), match[2], match[3]))
    exported = [(int(r["index"]), r["model"], r["state"]) for r in states]
    if exported != native or [r[0] for r in native] != list(range(1, len(states)+1)):
        raise _coded_error(ValueError, 'invalid_machine_coordinates_data', 'REF labels are inconsistent or not consecutive')
    return states


def coordinate_projection(path, row_indices, column_indices, *, mtl=False):
    'Read selected matrix triplets with one-based REF indices. Omitted sparse entries are structural zeros. Empty present fields are invalid.'
    rows, cols = tuple(row_indices), tuple(column_indices)
    if (len(set(rows)) != len(rows) or len(set(cols)) != len(cols)
            or any(i < 1 for i in rows+cols)):
        raise _coded_error(ValueError, 'invalid_machine_coordinates_data', 'Require unique positive indices')
    ri, ci = {v:i for i,v in enumerate(rows)}, {v:i for i,v in enumerate(cols)}
    result = np.zeros((len(rows), len(cols)), complex)
    seen = set()
    with Path(path).open(encoding="utf-8-sig", newline="") as handle:
        if mtl:
            entries = ((int(p[0]), int(p[1]), p[2], "0")
                       for line in handle if (p := line.split()))
        else:
            entries = ((int(r["row"]), int(r["column"]), r["real"], r["imag"])
                       for r in csv.DictReader(handle))
        for row, col, real, imag in entries:
            if row not in ri or col not in ci:
                continue
            if (row, col) in seen:
                raise _coded_error(ValueError, 'invalid_machine_coordinates_data', 'Duplicate projection coordinate')
            value = complex(float(real), float(imag))
            if not np.isfinite(value):
                raise _coded_error(ValueError, 'invalid_machine_coordinates_data', 'Coordinate must be finite')
            seen.add((row, col))
            result[ri[row], ci[col]] = value
    return result


def machine_state_map(manifest, ref_states, *, swap_q=False):
    'Map 59 machine states with scale REF/pm=+1. REF indices are one-based and engine indices zero-based. Fluxes use machine-base p.u., speed uses p.u. and angles use radians.'
    index = {}
    for r in manifest:
        if r["kind"] != "machine":
            continue
        short = "Grid\\"+r["relative_path"].split("Grid.ElmNet\\", 1)[1]
        key = short, r["variable"]
        if key in index:
            raise _coded_error(ValueError, 'invalid_machine_coordinates_data', 'Duplicate machine state')
        index[key] = r
    mapped = []
    for p in ref_states:
        if not p["model"].endswith(".ElmSym"):
            continue
        name = p["state"]
        if swap_q and (p["model"], "psi2q") in index:
            name = {"psi1q":"psi2q", "psi2q":"psi1q"}.get(name, name)
        r = index[(p["model"], name)]
        mapped.append({"ref_index":int(p["index"]), "ref_model":p["model"],
                       "ref_state":p["state"], "pm_index":int(r["index"]),
                       "pm_state":name, "scale_ref_per_pm":1.,
                       "object_id":r["object_id"], "relative_path":r["relative_path"],
                       "unit":r["unit"],
                       "designation":'q axes exchanged' if name != p["state"] else "idéntica"})
    if len(mapped) != len(index) or len({r["pm_index"] for r in mapped}) != len(index):
        raise _coded_error(ValueError, 'invalid_machine_coordinates_data', 'Machine map is incomplete or not bijective')
    return mapped


def machine_transform(transform, mapping, manifest, ref_states, *, swap_q=False):
    'Replace only machine rows in T and preserve other coordinates. Use y_REF=T*x and A_REF=T*A*T^-1. The optional q-loop permutation changes designation, not physical parameters.'
    corrected = np.array(transform, copy=True)
    details = [dict(r) for r in mapping]
    for r in machine_state_map(manifest, ref_states, swap_q=swap_q):
        i = r["ref_index"]-1
        corrected[i] = 0.
        corrected[i, r["pm_index"]] = 1.
        details[i].update(r)
        details[i]["coordinate_status"] = (
            'q designation checked in the Amat block without changing the physical model' if swap_q
            else 'Original native RMS designation')
    return corrected, details


def local_candidates(labels):
    'Return bounded coordinate hypotheses and negative controls without fitting parameters.'
    order = np.arange(len(labels))
    yield "original", order.copy(), np.ones(len(labels)), 'Native RMS designation'
    if "psi2q" in labels:
        q1, q2 = labels.index("psi1q"), labels.index("psi2q")
        order[q1], order[q2] = order[q2], order[q1]
        yield "swap_q", order.copy(), np.ones(len(labels)), 'Two q-axis loops, TechRef section 6.1.1'
    sign = np.ones(len(labels))
    for label in ("psi1q", "psi2q"):
        if label in labels:
            sign[labels.index(label)] = -1.
    yield "opposite_q_sign", order.copy(), sign, 'Negative control, opposite generator convention'
    d_order = order.copy()
    fd, d1 = labels.index("psifd"), labels.index("psi1d")
    d_order[fd], d_order[d1] = d_order[d1], d_order[fd]
    yield "swap_d", d_order, np.ones(len(labels)), 'Negative control contradicts field and equation (52)'
