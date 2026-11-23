'Linearize support for power-system modal analysis.'
from __future__ import annotations

from dataclasses import replace
import numpy as np

from .schema import (Linearization, ModalError, ModalOptions, check_cancel,
                     diagnostic, fail, real_vector, validate_options)


def _evaluate(adapter, x, z, time_s):
    # El clon se descarta después de cada llamada, incluidas las de equilibrio.
    clone = adapter.clone()
    if clone is adapter:
        fail('unsupported', 'unsafe_clone', 'Adapter does not isolate its runtime')
    f, g = clone.evaluate(x.copy(), z.copy(), time_s)
    f, g = real_vector(f, 'f [state unit/s]'), real_vector(g, "g real")
    if f.shape != x.shape or g.shape != z.shape:
        fail('invalid_input', 'residual_dimension', 'f/g dimensions do not match x/z')
    return np.r_[f, g]


def _eliminate(fx, fz, gx, gz, options):
    if not gz.size:
        return fx.copy(), None, None
    try:
        correction = np.linalg.solve(gz, gx)
    except np.linalg.LinAlgError:
        fail('unsupported', 'singular_gz', 'Singular gz. No supported index-1 DAE')
    condition = float(np.linalg.cond(gz))
    if not np.isfinite(condition) or condition > options.condition_limit:
        fail('numerical_failure', 'ill_conditioned_gz', 'gz exceeds the condition limit',
             condition=condition if np.isfinite(condition) else None,
             condition_limit=options.condition_limit)
    a = fx-fz@correction
    if not np.isfinite(a).all():
        fail('numerical_failure', 'nonfinite_a', 'Elimination produced nonfinite values')
    return a, condition, correction


def linearize(adapter, point, options: ModalOptions, *, cancel=None, progress=None,
              column_order=None) -> Linearization:
    'Differentiate f/g with independent x/z at constant time and inputs. Perturbations use declared units and scales. Do not resolve equilibrium separately at each perturbation.'
    validate_options(options)
    check_cancel(cancel)
    x, z = real_vector(point.x, "x"), real_vector(point.z, "z")
    nx, nz = len(x), len(z)
    if nx == 0:
        fail('unsupported', 'no_dynamic_states', 'Model has no dynamic states')
    if len(point.states) != nx or tuple(s.index for s in point.states) != tuple(range(nx)):
        fail('invalid_input', 'state_order', 'StateRef must describe every state in order')
    if not all(s.object_id and s.relative_path and s.variable and s.unit for s in point.states):
        fail('invalid_input', 'state_identity', 'Missing identity, variable or unit of a state')
    sx = real_vector([s.scale for s in point.states], 'State scales')
    sz = real_vector(point.algebraic_scales, 'Algebraic scales')
    if sz.shape != z.shape or np.any(sx <= 0) or np.any(sz <= 0):
        fail('invalid_input', 'invalid_scales', 'Scales must be positive with consistent dimensions')
    if not np.isfinite(point.time_s):
        fail('invalid_input', 'invalid_time', 'Time must be finite')
    blockers = point.metadata.get("unsupported_features", [])
    if blockers:
        raise ModalError("unsupported", [diagnostic(
            row.get("code", "unsupported_continuous_model"), row["message"], level="error",
            object_id=row.get("object_id"), relative_path=row.get("relative_path"))
            for row in blockers])
    y, scales = np.r_[x, z], np.r_[sx, sz]
    steps = np.finfo(np.float64).eps**(1/3)*np.maximum(abs(y), scales)*options.step_factor
    base = _evaluate(adapter, x, z, point.time_s)
    with np.errstate(over="ignore", divide="ignore", invalid="ignore"):
        normalized = abs(base)/scales
    if not np.isfinite(normalized).all():
        fail('numerical_failure', 'nonfinite_normalized_residual',
             'Residual normalization exceeds float64 range')
    if not np.isfinite(steps).all() or np.any(steps <= 0):
        fail('numerical_failure', 'invalid_difference_step',
             'Difference step is not representable in float64')
    diagnostics = [diagnostic('equilibrium_residual', 'Normalized real operating-point residuals',
        max_f_per_s=float(np.max(normalized[:nx], initial=0)),
        max_g=float(np.max(normalized[nx:], initial=0)),
        f=base[:nx].tolist(), g=base[nx:].tolist(),
        state_scales=sx.tolist(), algebraic_scales=sz.tolist())]
    residual_errors = [diagnostic('not_equilibrium', 'Derivative is nonzero at the requested equilibrium',
        level="error", state=point.states[i], variable=point.states[i].variable,
        residual_per_s=float(normalized[i])) for i in range(nx)
        if normalized[i] > options.equilibrium_tol]
    residual_errors += [diagnostic('algebraic_residual', 'Algebraic residual exceeds tolerance',
        level="error", index=int(i), residual=float(normalized[nx+i])) for i in range(nz)
        if normalized[nx+i] > options.equilibrium_tol]
    matrices = []
    boundary_errors = []
    order = list(range(len(y))) if column_order is None else list(column_order)
    if sorted(order) != list(range(len(y))):
        fail('invalid_input', 'column_order', 'Order must be a permutation of columns')
    for factor in (1.0, 0.5, 2.0):
        jac = np.empty((len(y), len(y)))
        for j in order:
            check_cancel(cancel)
            h = steps[j]*factor
            plus, minus = y.copy(), y.copy()
            plus[j] += h
            minus[j] -= h
            fp = _evaluate(adapter, plus[:nx], plus[nx:], point.time_s)
            fm = _evaluate(adapter, minus[:nx], minus[nx:], point.time_s)
            jac[:, j] = (fp-fm)/(2*h)
            if factor in (1.0, 0.5):
                right, left = (fp-base)/h, (base-fm)/h
                jump = np.max(abs(right-left)/np.maximum(1.0, np.maximum(abs(right), abs(left))))
                if factor == 1.0:
                    boundary_errors.append(float(jump))
                elif jump > 1e-4 and jump >= .8*boundary_errors[order.index(j)]:
                    state = point.states[j] if j < nx else None
                    diagnostics.append(diagnostic('unsupported_nonsmooth',
                        'One-sided derivatives differ at the active boundary',
                        level="error", state=state, column=int(j), step=float(h), jump=float(jump)))
            if progress:
                progress({"phase": "jacobian", "step_factor": factor,
                          "column": j+1, "columns": len(y)})
        matrices.append(jac)
    if any(d["code"] == "unsupported_nonsmooth" for d in diagnostics):
        raise ModalError("unsupported", diagnostics+residual_errors)
    if residual_errors:
        raise ModalError("not_converged", diagnostics+residual_errors)
    blocks, variants, conditions = [], [], []
    for jac in matrices:
        fx, fz, gx, gz = jac[:nx, :nx], jac[:nx, nx:], jac[nx:, :nx], jac[nx:, nx:]
        a, condition, _ = _eliminate(fx, fz, gx, gz, options)
        blocks.append((fx, fz, gx, gz))
        variants.append(a)
        conditions.append(condition)
    fx, fz, gx, gz = blocks[0]
    scaled_gz = gz*sz[None, :]/sz[:, None] if nz else gz
    diagnostics += [diagnostic('algebraic_condition', 'Condition of raw gz and of gz in declared scales',
        condition=conditions[0], scaled_condition=float(np.linalg.cond(scaled_gz)) if nz else None),
        diagnostic('finite_difference', 'Central differences at h, h/2 and 2h, float64',
            steps=steps.tolist(), a_half=variants[1].tolist(), a_double=variants[2].tolist()),
        diagnostic('angular_frame', 'Preserve the adapter frame and retain small modes',
            frame=point.metadata.get("angular_frame", 'No angular reduction'))]
    return Linearization(fx, fz, gx, gz, variants[0], tuple(diagnostics))


def reduce_angle_reference(linearization, states, angle_indexes, reference_index,
                           *, symmetry_tol=1e-7):
    'Form delta_i-delta_ref only when A preserves common rotation. Do not remove speed states or equate a bus-angle reference with the rotor reference.'
    a = linearization.a
    indexes = tuple(angle_indexes)
    if (reference_index not in indexes or len(set(indexes)) != len(indexes)
            or any(i < 0 or i >= len(states) or states[i].unit != "rad" for i in indexes)):
        fail('invalid_input', 'angle_reference', 'Indices must identify rotor angles in radians')
    generator = np.zeros(len(states))
    generator[list(indexes)] = 1.
    if np.linalg.norm(a@generator, np.inf) > symmetry_tol*max(1., np.linalg.norm(a, np.inf)):
        fail('unsupported', 'no_rotational_symmetry', 'A does not preserve the declared common rotation')
    keep = [i for i in range(len(states)) if i != reference_index]
    t = np.eye(len(states))[keep]
    for row, i in enumerate(keep):
        if i in indexes:
            t[row, reference_index] = -1.
    r = np.eye(len(states))[:, keep]
    reduced_states = tuple(replace(states[i], index=j,
        variable=(states[i].variable+"_relative" if i in indexes else states[i].variable))
        for j, i in enumerate(keep))
    details = []
    for d in linearization.diagnostics:
        d = dict(d)
        if d["code"] == "finite_difference":
            for key in ("a_half", "a_double"):
                d[key] = (t@np.asarray(d[key])@r).tolist()
        details.append(d)
    details.append(diagnostic('angular_reduction', 'Explicit rotational symmetry reduction',
        removed_state=vars(states[reference_index]), transformation=t.tolist(), reconstruction=r.tolist()))
    return Linearization(t@linearization.fx@r, t@linearization.fz,
        linearization.gx@r, linearization.gz.copy(), t@a@r, tuple(details)), reduced_states
