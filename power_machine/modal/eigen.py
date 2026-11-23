'Eigen support for power-system modal analysis.'
from __future__ import annotations

import math
import numpy as np
from .schema import ModalResult, diagnostic, fail, validate_options


def complex_value(value, unit):
    return {"real": float(np.real(value)), "imag": float(np.imag(value)), "unit": unit}


def match_eigenvalues(values, candidates):
    'Minimum-total-distance global eigenvalue matching, independent of eig ordering. Use the square Hungarian assignment algorithm.'
    values, candidates = np.asarray(values), np.asarray(candidates)
    if len(values) != len(candidates):
        fail('invalid_input', 'mode_count', 'Eigenvalue counts differ')
    n = len(values)
    cost = abs(values[:, None]-candidates[None, :])
    u, v = np.zeros(n+1), np.zeros(n+1)
    p, way = np.zeros(n+1, dtype=int), np.zeros(n+1, dtype=int)
    for i in range(1, n+1):
        p[0], j0 = i, 0
        minimum, used = np.full(n+1, np.inf), np.zeros(n+1, dtype=bool)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], np.inf, 0
            for j in range(1, n+1):
                if not used[j]:
                    cur = cost[i0-1, j-1]-u[i0]-v[j]
                    if cur < minimum[j]:
                        minimum[j], way[j] = cur, j0
                    if minimum[j] < delta:
                        delta, j1 = minimum[j], j
            for j in range(n+1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minimum[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0], j0 = p[j1], j1
            if j0 == 0:
                break
    result = np.empty(n, dtype=int)
    for j in range(1, n+1):
        result[p[j]-1] = j-1
    return result


def solve_modes(linearization, states, options) -> ModalResult:
    validate_options(options)
    raw = np.asarray(linearization.a)
    n = len(states)
    if n == 0:
        fail('unsupported', 'no_dynamic_states', 'Model has no dynamic states')
    if np.iscomplexobj(raw) or raw.shape != (n, n) or not np.isfinite(raw).all():
        fail('invalid_input', 'invalid_a', 'A must be real, finite and square in state order')
    if tuple(s.index for s in states) != tuple(range(n)):
        fail('invalid_input', 'state_order', 'State order is inconsistent with A')
    a = raw.astype(float)
    diagnostics = list(linearization.diagnostics)
    try:
        values, vectors = np.linalg.eig(a)
    except np.linalg.LinAlgError:
        fail('numerical_failure', 'eigen_failure', 'Could not solve the eigenvalue problem')
    order = sorted(range(n), key=lambda i: (float(values[i].real), -float(values[i].imag)))
    values, vectors = values[order], vectors[:, order]
    norm_a = np.linalg.norm(a)
    residuals = np.array([np.linalg.norm(a@vectors[:, i]-values[i]*vectors[:, i])/
        max(norm_a*np.linalg.norm(vectors[:, i]), np.finfo(float).tiny) for i in range(n)])
    if np.max(residuals) > 1e-7:
        fail('numerical_failure', 'eigen_residual', 'Eigenvector residual exceeds 1e-7')
    sensitivity = np.zeros(n)
    for d in diagnostics:
        if d["code"] == "finite_difference":
            for key in ("a_half", "a_double"):
                perturbed = np.linalg.eigvals(np.asarray(d[key]))
                mapped = perturbed[match_eigenvalues(values, perturbed)]
                sensitivity = np.maximum(sensitivity, abs(values-mapped))
    inaccurate = sensitivity > np.maximum(1e-7, 1e-5*abs(values))
    uncertain = inaccurate | (sensitivity > options.eigenvalue_tol_per_s)
    if uncertain.any():
        diagnostics.append(diagnostic('step_sensitivity', 'Step sensitivity prevents a conclusive classification',
            level="warning", mode_indexes=np.flatnonzero(uncertain).tolist(),
            eigenvalue_tol_per_s=options.eigenvalue_tol_per_s,
            acceptance_exceeded_indexes=np.flatnonzero(inaccurate).tolist()))
    condition = float(np.linalg.cond(vectors))
    distances = abs(values[:, None]-values[None, :])
    np.fill_diagonal(distances, np.inf)
    repeated = np.any(distances <= options.eigenvalue_tol_per_s*np.maximum(1., abs(values[:, None])))
    unresolved_by_step = np.any(distances <= sensitivity[:, None]+sensitivity[None, :])
    participation = None
    if (repeated or unresolved_by_step or not np.isfinite(condition)
            or condition > options.participation_condition_limit):
        diagnostics.append(diagnostic('participation_unidentifiable',
            'Repeated eigenvalues, inseparable under step variation or ill-conditioned eigenvectors',
            level="warning", eigenvector_condition=condition if np.isfinite(condition) else None,
            repeated=bool(repeated), unresolved_by_step=bool(unresolved_by_step)))
    else:
        w = np.linalg.solve(vectors, np.eye(n))
        participation = []
        for i in range(n):
            p = vectors[:, i]*w[i, :]
            magnitudes = abs(p)
            total = float(np.sum(magnitudes))
            for k, state in enumerate(states):
                participation.append({"mode_index": i, "state_index": k,
                    "object_id": state.object_id, "relative_path": state.relative_path,
                    "variable": state.variable, "state_unit": state.unit,
                    "p": complex_value(p[k], "1"), "abs_p": float(magnitudes[k]),
                    "normalized_abs_p": float(magnitudes[k]/total)})
        diagnostics.append(diagnostic('bilinear_normalization', 'Rows W=solve(V,I), W V=I, without conjugation',
            eigenvector_condition=condition, max_identity_error=float(np.max(abs(w@vectors-np.eye(n))))))
    modes = []
    paired = {}
    for i, value in enumerate(values):
        if i not in paired and abs(value.imag) > options.eigenvalue_tol_per_s:
            candidates = [j for j in range(n) if j != i and j not in paired]
            if candidates:
                j = min(candidates, key=lambda j: abs(values[j]-value.conjugate()))
                if abs(values[j]-value.conjugate()) <= 1e-7*max(1., abs(value)):
                    paired[i], paired[j] = j, i
    for i, value in enumerate(values):
        sigma, omega = float(value.real), float(value.imag)
        modulus = abs(value)
        near = abs(sigma) <= options.eigenvalue_tol_per_s
        classification = "almost_neutral" if near else ("stable" if sigma < 0 else "unstable")
        if uncertain[i]:
            classification = "uncertain"
        if near:
            diagnostics.append(diagnostic('near_neutral_mode', 'Sign near zero does not imply operational safety',
                level="warning", mode_index=i))
        modes.append({"index": i, "lambda": complex_value(value, "1/s"),
            "frequency_hz": abs(omega)/(2*math.pi),
            "damping_ratio": None if modulus == 0 else -sigma/modulus,
            "decay_rate_per_s": sigma, "classification": classification,
            "kind": "aperiodic" if abs(omega) <= options.eigenvalue_tol_per_s else "oscillatory",
            "conjugate_index": paired.get(i), "table_visible": i not in paired or omega >= 0,
            "eigenvector_residual": float(residuals[i]),
            "step_sensitivity_per_s": float(sensitivity[i]),
            "right_eigenvector": [complex_value(vectors[k, i], states[k].unit) for k in range(n)]})
    return ModalResult("partial" if uncertain.any() or participation is None else "completed",
                       tuple(modes), None if participation is None else tuple(participation), tuple(diagnostics))
