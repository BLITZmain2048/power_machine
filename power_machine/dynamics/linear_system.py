"""Cached dense linear-system backends for RMS Newton iterations."""
from __future__ import annotations
import importlib
import numpy as np
from .errors import coded_error

BACKENDS = frozenset(('compatibility','numpy','lu','scipy','auto'))

class LinearFactorization:
    """Own reusable numerical factors for one unchanged Jacobian.

    compatibility retains the measured small dense inverse path and bitwise
    trajectories. numpy never builds an inverse but refactors on each solve.
    lu uses optional SciPy LU, falling back to numpy. scipy requires SciPy.
    auto uses SciPy when available and otherwise the compatibility path.
    """
    def __init__(self, matrix, backend='compatibility'):
        if backend not in BACKENDS:
            raise coded_error(ValueError, 'invalid_linear_backend', f'Unknown RMS linear backend: {backend}')
        self.backend=backend
        scipy_linalg=None
        self._lu_solve=None
        if backend in ('lu','scipy','auto'):
            try:scipy_linalg=importlib.import_module('scipy.linalg')
            except ImportError:
                if backend=='scipy':
                    raise coded_error(ImportError, 'optional_backend_unavailable', 'Install power-machine[fast] to use SciPy LU') from None
                self.backend='compatibility' if backend=='auto' else 'numpy'
            else:
                self.backend='scipy'
                self._lu_solve=scipy_linalg.lu_solve
        if self.backend=='compatibility':self.data=np.linalg.inv(matrix)
        elif self.backend=='numpy':self.data=np.array(matrix,dtype=float,copy=True)
        else:
            import warnings
            with warnings.catch_warnings():
                warnings.simplefilter('error',scipy_linalg.LinAlgWarning)
                try:self.data=scipy_linalg.lu_factor(matrix,check_finite=True)
                except scipy_linalg.LinAlgWarning as exc:raise np.linalg.LinAlgError(str(exc)) from exc
    def solve(self, rhs):
        """Solve with the cached factors and preserve the RHS array."""
        if self.backend=='compatibility':return self.data@rhs
        if self.backend=='numpy':return np.linalg.solve(self.data,rhs)
        return self._lu_solve(self.data,rhs,check_finite=False)
