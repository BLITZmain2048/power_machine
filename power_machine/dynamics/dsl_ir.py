'Dsl ir support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass, field
from typing import Mapping, Sequence

from .blocks import Primitive
from .dsl_expr import (Assignment, Initializer, LimitDeclaration, VariableDeclaration,
                       Directive, Statement, Expr, Call, Unary, Binary, evaluate,
                       parse_equations, UnsupportedFunction)


def called_functions(expr: Expr) -> set[str]:
    if isinstance(expr, Call):
        return {expr.name.lower()} | set().union(*(called_functions(a) for a in expr.args))
    if isinstance(expr, Unary):
        return called_functions(expr.operand)
    if isinstance(expr, Binary):
        return called_functions(expr.left) | called_functions(expr.right)
    return set()


PURE_FUNCTIONS = frozenset({"abs", "min", "max", "sqrt", "exp", "sin", "cos", "tan",
                            "atan", "acos", "pow", "sqr", "modulo", "lim", "lim_const",
                            "limstate", "limstate_const", "select", "selfix", "selfix_const", "time",
                            "lapprox", "invlapprox", "picontrol_const", "select_const"})
STATEFUL_FUNCTIONS = frozenset({"sapprox", "picdro", "picdro_const",
                                "flipflop", "aflipflop", "delay", "movingavg", "lastvalue",
                                "gradlim_const", "event", "output"})


@dataclass(frozen=True)
class BlockIR:
    object_id: str
    relative_path: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    states: tuple[str, ...]
    parameters: tuple[str, ...]
    algebraic: tuple[Assignment, ...]
    derivatives: tuple[Assignment, ...]
    initializers: tuple[Initializer, ...]
    limits: tuple[LimitDeclaration, ...]
    variables: tuple[VariableDeclaration, ...]
    directives: tuple[Directive, ...]
    parameter_lower_raw: tuple[str, ...] = ()
    parameter_upper_raw: tuple[str, ...] = ()

    @classmethod
    def from_fields(cls, object_id: str, relative_path: str, fields: Mapping[str, Sequence[str]]) -> "BlockIR":
        stmts = parse_equations(fields.get("sAddEquat") or ())
        def names(key: str) -> tuple[str, ...]:
            return tuple(str(s).strip() for s in (fields.get(key) or ()) if str(s).strip())
        return cls(object_id, relative_path, names("sInput"), names("sOutput"),
                   names("sStates"), names("sParams"),
                   tuple(s for s in stmts if isinstance(s, Assignment) and not s.derivative),
                   tuple(s for s in stmts if isinstance(s, Assignment) and s.derivative),
                   tuple(s for s in stmts if isinstance(s, Initializer)),
                   tuple(s for s in stmts if isinstance(s, LimitDeclaration)),
                   tuple(s for s in stmts if isinstance(s, VariableDeclaration)),
                   tuple(s for s in stmts if isinstance(s, Directive)),
                   names("sLowLimPar"), names("sUpLimPar"))

    @property
    def functions(self) -> frozenset[str]:
        exprs = [s.value for s in (*self.algebraic, *self.derivatives, *self.initializers)]
        exprs += [a for s in self.directives for a in s.args]
        return frozenset().union(*(called_functions(e) for e in exprs))

    @property
    def unsupported_functions(self) -> frozenset[str]:
        implemented_memory = {"picdro", "picdro_const", "flipflop", "aflipflop",
                              "delay", "gradlim_const", "lastvalue", "movingavg"}
        return self.functions - PURE_FUNCTIONS - implemented_memory

    def evaluate(self, values: Mapping[str, float], *, time_s: float | None = None,
                 runtime: object | None = None) -> tuple[dict[str, float], dict[str, float]]:
        'Evaluate an explicit block. Report an unresolved algebraic loop.'
        scope = dict(values)
        pending = list(self.algebraic)
        while pending:
            remaining = []
            progress = False
            for stmt in pending:
                try:
                    scope[stmt.target] = float(evaluate(stmt.value, scope, time_s=time_s, runtime=runtime))
                    progress = True
                except KeyError:
                    remaining.append(stmt)
            if not progress:
                raise _coded_error(ValueError, 'invalid_dsl_ir_data', f'Unresolved algebraic variables in {self.object_id}: {[s.target for s in remaining]}')
            pending = remaining
        deriv = {stmt.target: float(evaluate(stmt.value, scope, time_s=time_s, runtime=runtime)) for stmt in self.derivatives}
        return scope, deriv

    def initialize(self, values: Mapping[str, float]) -> dict[str, float]:
        scope = dict(values)
        pending = list(self.initializers)
        while pending:
            remaining = []
            progress = False
            for stmt in pending:
                try:
                    scope[stmt.target] = float(evaluate(stmt.value, scope))
                    progress = True
                except KeyError:
                    remaining.append(stmt)
            if not progress:
                raise _coded_error(ValueError, 'invalid_dsl_ir_data', f'Unresolved initialization in {self.object_id}: {[s.target for s in remaining]}')
            pending = remaining
        return scope


@dataclass(frozen=True)
class BlockInstance:
    object_id: str
    relative_path: str
    primitive: Primitive
    input_name: str = "yi"
    output_name: str = "yo"


@dataclass(frozen=True)
class Connection:
    'Source and destination of a BlkSig connection. Indices represent inodfrom/inodto. Endpoints preserve REF paths or IDs and sign preserves wire orientation.'
    pnodfrom: str
    pnodto: str
    inodfrom: int = 0
    inodto: int = 0
    sign: float = 1.0


@dataclass(frozen=True)
class MacroIR:
    object_id: str
    relative_path: str
    instances: tuple[BlockInstance, ...]
    connections: tuple[Connection, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]

    def __post_init__(self) -> None:
        ids = [item.object_id for item in self.instances]
        if len(ids) != len(set(ids)):
            raise _coded_error(ValueError, 'invalid_dsl_ir_data', 'Duplicate instance IDs')
        known = set(ids) | set(self.inputs) | set(self.outputs)
        for wire in self.connections:
            if wire.pnodfrom not in known or wire.pnodto not in known:
                raise _coded_error(ValueError, 'invalid_dsl_ir_data', f'Unknown connection endpoint: {wire}')

    def initialize_backward(self, desired_outputs: Mapping[str, float]) -> tuple[dict[str, float], dict[str, float]]:
        'Propagate imposed outputs to inputs through acyclic one-to-one macros.'
        desired = dict(desired_outputs)
        states: dict[str, float] = {}
        by_id = {item.object_id: item for item in self.instances}
        pending = list(self.connections)
        for _ in range(len(self.instances) + len(self.connections) + 1):
            progress = False
            for wire in list(pending):
                if wire.pnodto in desired and wire.pnodfrom not in desired:
                    if wire.sign == 0:
                        raise _coded_error(ValueError, 'invalid_dsl_ir_data', 'Wire sign must be nonzero')
                    desired[wire.pnodfrom] = desired[wire.pnodto] / wire.sign
                    pending.remove(wire)
                    progress = True
            for oid, instance in by_id.items():
                if oid in desired and oid not in states:
                    x, u = instance.primitive.backward(desired[oid])
                    states[oid] = x
                    # Un solo puerto de entrada; BlkSum requiere resolución propia.
                    for wire in self.connections:
                        if wire.pnodto == oid and wire.pnodfrom not in desired:
                            desired[wire.pnodfrom] = u / wire.sign
                            progress = True
            if not progress:
                break
        missing = set(self.inputs) - set(desired)
        if missing:
            raise _coded_error(ValueError, 'missing_dsl_ir_data', f'Unresolved initial inputs: {sorted(missing)}')
        return {k: desired[k] for k in self.inputs}, states
