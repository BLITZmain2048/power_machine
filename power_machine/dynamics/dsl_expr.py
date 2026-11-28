'Dsl expr support for power-system RMS dynamics.'
from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

from dataclasses import dataclass, field
import math
import re
from typing import Mapping, Sequence
from bisect import bisect_left, bisect_right


class ParseError(ValueError):
    'DSL syntax outside the recognized subset.'


class UnsupportedFunction(ValueError):
    'The function requires unsupported semantics or missing implementation data.'


@dataclass(frozen=True)
class Number:
    value: float


@dataclass(frozen=True)
class String:
    value: str


@dataclass(frozen=True)
class Name:
    value: str


@dataclass(frozen=True)
class Unary:
    op: str
    operand: Expr


@dataclass(frozen=True)
class Binary:
    op: str
    left: Expr
    right: Expr


@dataclass(frozen=True)
class Call:
    name: str
    args: tuple[Expr, ...]


Expr = Number | String | Name | Unary | Binary | Call


@dataclass
class DSLMemory:
    'Memory indexed by AST call identity. Reads do not commit state during Newton. begin_accept and finish_accept delimit an accepted update. Timer arguments use seconds and signal limits use the signal units.'

    records: dict[int, dict[str, object]] = field(default_factory=dict)
    observations: dict[int, tuple[str, float, tuple[float, ...], float]] | None = None
    trace: dict[int, tuple[str, float, tuple[float, ...], float]] | None = None
    version: int = 0
    switch_modes: dict[int, bool] = field(default_factory=dict)
    switch_watch: set[int] = field(default_factory=set)
    switch_values: dict[int, tuple[float, bool]] = field(default_factory=dict)

    def compare(self, key, op, a, b):
        natural = _COMPARATORS[op](a, b)
        if key in self.switch_watch:
            self.switch_values[key] = (float(a-b), natural)
        return float(self.switch_modes.get(key, natural))

    def limit(self, key, x, low, high):
        'Hold the selected limit branch during Newton and observe its switching margins.'
        lower = self.compare(-2*key, '<=', x, low)
        upper = self.compare(-2*key-1, '>=', x, high)
        return low if lower else high if upper else x

    def begin_accept(self) -> None:
        self.observations = {}

    @staticmethod
    def _history(history: list[tuple[float, float]], target: float) -> float:
        if target <= history[0][0]:
            return history[0][1]
        i = bisect_right(history, (target, float("inf")))
        if i == len(history):
            return history[-1][1]
        t0, y0 = history[i-1]
        t1, y1 = history[i]
        return y0 + (target-t0)*(y1-y0)/(t1-t0)

    @staticmethod
    def _trial_history(history: list[tuple[float, float]], target: float,
                       time_s: float, value: float) -> float:
        """Consulta una muestra tentativa sin copiar ni confirmar el historial."""
        last_t, last_y = history[-1]
        if time_s > last_t and target > last_t:
            if target >= time_s:
                return value
            return last_y + (target-last_t)*(value-last_y)/(time_s-last_t)
        return DSLMemory._history(history, target)

    def read(self, key: int, name: str, t_s: float, args: tuple[float, ...]) -> float:
        count = {"picdro": 3, "picdro_const": 3, "flipflop": 2,
                 "aflipflop": 3, "delay": 2, "gradlim_const": 3,
                 "lastvalue": 1, "movingavg": 3}[name]
        if len(args) != count or not math.isfinite(t_s) or not all(map(math.isfinite, args)):
            raise _coded_error(ValueError, 'invalid_dsl_expr_data', f'Invalid arguments for {name}')
        old = self.records.get(key)
        if name in ("picdro", "picdro_const"):
            if args[1] < 0 or args[2] < 0:
                raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'Tpick/Tdrop must be nonnegative')
            condition = args[0] >= 0.5
            if old is None:
                value = float(condition)
            else:
                value = float(old["value"])
                if condition == old["condition"] and condition != bool(value):
                    delay_s = args[1] if condition else args[2]
                    if t_s >= float(old["since"]) + delay_s - 1e-12:
                        value = float(condition)
                elif condition != old["condition"] and (args[1] if condition else args[2]) == 0:
                    value = float(condition)
        elif name == "flipflop":
            set_on, reset_on = args[0] >= 0.5, args[1] >= 0.5
            if old is None and set_on and reset_on:
                raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'flipflop: simultaneous SET and RESET during initialization')
            value = float(old["value"]) if old is not None else float(set_on)
        elif name == "aflipflop":
            value = (float(old["value"]) if old is not None and old["latched"]
                     else args[0])
        elif name == "gradlim_const":
            if args[1] > 0 or args[2] < 0:
                raise _coded_error(ValueError, 'missing_dsl_expr_data', 'gradlim_const requires min<=0<=max')
            value = args[0] if old is None else min(max(args[0], float(old["value"]) +
                        args[1]*max(0.0, t_s-float(old["time"]))),
                        float(old["value"]) + args[2]*max(0.0, t_s-float(old["time"])))
        elif name in ("delay", "lastvalue", "movingavg"):
            if name == "delay" and args[1] < 0 or name == "movingavg" and (args[1] < 0 or args[2] <= 0):
                raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'Invalid time window')
            history = old["history"] if old is not None else [(t_s, args[0])]
            if name == "lastvalue":
                index = bisect_left(history, (t_s-1e-12, float("-inf"))) - 1
                value = history[index][1] if index >= 0 else history[0][1]
            elif name == "delay":
                value = self._trial_history(history, t_s-args[1], t_s, args[0]) if args[1] else args[0]
            else:
                end = t_s-args[1]
                start = end-args[2]
                first = bisect_right(history, (start, float("inf")))
                last = bisect_left(history, (end, float("-inf")))
                knots = [start] + [t for t, _ in history[first:last]]
                if history[-1][0] < t_s and start < t_s < end:
                    knots.append(t_s)
                knots.append(end)
                values = [self._trial_history(history, t, t_s, args[0]) for t in knots]
                value = sum((b-a)*(v+w)/2 for a,b,v,w in
                            zip(knots, knots[1:], values, values[1:])) / args[2]
        else:
            raise _coded_error(UnsupportedFunction, 'unsupported_dsl_expr_data', name)
        if self.observations is not None:
            self.observations[key] = (name, t_s, args, float(value))
        elif self.trace is not None:
            self.trace[key] = (name, t_s, args, float(value))
        return float(value)

    def finish_accept(self) -> None:
        if self.observations is None:
            raise _coded_error(RuntimeError, 'invalid_dsl_expr_data', 'begin_accept is required')
        for key, (name, t_s, args, value) in self.observations.items():
            old = self.records.get(key)
            if old is not None and t_s < float(old["time"])-1e-12:
                raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'DSL time moved backwards')
            record: dict[str, object] = {"time": t_s, "value": value}
            if name in ("picdro", "picdro_const"):
                condition = args[0] >= 0.5
                record["condition"] = condition
                record["since"] = (t_s if old is None or condition != old["condition"]
                                   else old["since"])
                if condition != bool(record["value"]):
                    record["deadline"] = float(record["since"]) + (args[1] if condition else args[2])
            elif name == "flipflop":
                set_on, reset_on = args[0] >= 0.5, args[1] >= 0.5
                record["value"] = float(True if set_on and not reset_on else
                                        False if reset_on and not set_on else value)
            elif name == "aflipflop":
                set_on, reset_on = args[1] >= 0.5, args[2] >= 0.5
                latched = bool(old["latched"]) if old is not None else False
                if set_on and not reset_on and not latched:
                    record["value"] = args[0]
                    latched = True
                elif reset_on and not set_on:
                    latched = False
                    record["value"] = args[0]
                record["latched"] = latched
            elif name in ("delay", "lastvalue", "movingavg"):
                history = old["history"] if old is not None else []
                prior_value = float(history[-1][1]) if history else None
                if history and abs(history[-1][0]-t_s) <= 1e-12:
                    history[-1] = (t_s, args[0])
                else:
                    history.append((t_s, args[0]))
                record["history"] = history
                deadlines = [float(deadline) for deadline in
                             (old.get("deadlines", ()) if old is not None else ())
                             if float(deadline) > t_s + 1e-12]
                if prior_value is not None and abs(args[0]-prior_value) > 1e-12:
                    if name == "delay" and args[1] > 0:
                        deadlines.append(t_s + args[1])
                    elif name == "movingavg":
                        deadlines.extend((t_s + args[1], t_s + args[1] + args[2]))
                record["deadlines"] = sorted(set(deadlines))
            elif name == "gradlim_const":
                delta = args[0] - value
                rate = args[2] if delta > 0 else args[1]
                if delta and rate:
                    record["deadline"] = t_s + delta / rate
            self.records[key] = record
        self.observations = None
        self.version += 1

    def next_event_time(self, after_s: float) -> float | None:
        'Return the next known timer deadline in seconds.'
        candidates = []
        for record in self.records.values():
            deadline = record.get("deadline")
            if deadline is not None and float(deadline) > after_s + 1e-12:
                candidates.append(float(deadline))
            candidates.extend(float(t) for t in record.get("deadlines", ())
                              if float(t) > after_s + 1e-12)
        return min(candidates) if candidates else None


@dataclass(frozen=True)
class Assignment:
    target: str
    value: Expr
    derivative: bool = False


@dataclass(frozen=True)
class Initializer:
    target: str
    value: Expr
    initial_only: bool = False


@dataclass(frozen=True)
class LimitDeclaration:
    kind: str
    target: str
    lower: Expr | None
    upper: Expr | None
    lower_closed: bool
    upper_closed: bool


@dataclass(frozen=True)
class VariableDeclaration:
    target: str
    unit: str | None
    description: str | None


@dataclass(frozen=True)
class Directive:
    name: str
    args: tuple[Expr, ...]


Statement = Assignment | Initializer | LimitDeclaration | VariableDeclaration | Directive


_TOKEN = re.compile(
    r"\s+|(?:\d+\.\d+|\d+\.(?![A-Za-z])|\d+|\.\d+)(?:[Ee][+-]?\d+)?|"
    r"'(?:[^']|'')*'|\"(?:[^\"]|\"\")*\"|"
    r"\.and\.|\.or\.|\.not\.|<=|>=|<>|==|!=|"
    r"[A-Za-z_][A-Za-z_0-9]*|[+*/^%<>=(),{};\[\]-]",
    re.IGNORECASE,
)
_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*\Z")
_NUMBER = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][+-]?\d+)?\Z", re.I)
_PRECEDENCE = {".or.": 1, ".and.": 2, "=": 3, "==": 3, "!=": 3,
               "<>": 3, "<": 3, ">": 3, "<=": 3, ">=": 3,
               "+": 4, "-": 4, "*": 5, "/": 5, "%": 5, "^": 6}


def _strip_comment(line: str) -> str:
    quote = ""
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                if i + 1 < len(line) and line[i + 1] == quote:
                    i += 2
                    continue
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch == "!":
            return line[:i]
        i += 1
    return line


def logical_lines(lines: Sequence[str]) -> list[str]:
    'Join & continuations and strip DSL comments.'
    result: list[str] = []
    for line in lines:
        clean = _strip_comment(line).strip()
        if not clean:
            continue
        if clean.startswith("&"):
            if not result:
                raise _coded_error(ParseError, 'invalid_dsl_expr_data', 'Continuation & has no previous line')
            result[-1] += " " + clean[1:].lstrip()
        else:
            result.append(clean)
    return result


def _tokens(source: str) -> list[str]:
    tokens = []
    pos = 0
    while pos < len(source):
        match = _TOKEN.match(source, pos)
        if match is None:
            raise _coded_error(ParseError, 'invalid_dsl_expr_data', f'Unrecognized symbol near {source[pos:pos + 24]!r}')
        token = match.group()
        if not token.isspace():
            tokens.append(token)
        pos = match.end()
    return tokens


class _Parser:
    def __init__(self, tokens: list[str]):
        self.tokens = tokens
        self.pos = 0

    def peek(self) -> str:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ""

    def take(self, expected: str | None = None) -> str:
        token = self.peek()
        if not token or expected is not None and token != expected:
            raise _coded_error(ParseError, 'invalid_dsl_expr_data', f"Expected {expected or 'expression'}, found {token!r}")
        self.pos += 1
        return token

    def expr(self, min_bp: int = 0) -> Expr:
        token = self.take()
        if token in ("+", "-", ".not."):
            left: Expr = Unary(token.lower(), self.expr(7))
        elif token in ("(", "{"):
            left = self.expr()
            self.take(")" if token == "(" else "}")
        elif _NUMBER.fullmatch(token):
            left = Number(float(token))
        elif token[0] in "'\"":
            left = String(token[1:-1].replace(token[0] * 2, token[0]))
        elif _IDENT.fullmatch(token):
            if self.peek() == "(":
                self.take("(")
                args = []
                if self.peek() != ")":
                    while True:
                        args.append(self.expr())
                        if self.peek() != ",":
                            break
                        self.take(",")
                self.take(")")
                left = Call(token, tuple(args))
            else:
                left = Name(token)
        else:
            raise _coded_error(ParseError, 'invalid_dsl_expr_data', f'Invalid operand: {token!r}')
        while True:
            op = self.peek().lower()
            bp = _PRECEDENCE.get(op, -1)
            if bp < min_bp:
                break
            self.take()
            right = self.expr(bp if op == "^" else bp + 1)
            left = Binary(op, left, right)
        return left

    def finish(self) -> None:
        if self.peek():
            raise _coded_error(ParseError, 'invalid_dsl_expr_data', f'Remaining text: {self.tokens[self.pos:]!r}')


def parse_expression(source: str) -> Expr:
    parser = _Parser(_tokens(source))
    expr = parser.expr()
    parser.finish()
    return expr


def _split_top(source: str, delimiter: str) -> list[str]:
    parts = []
    quote = ""
    depth = 0
    start = 0
    for i, ch in enumerate(source):
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "({[":
            depth += 1
        elif ch in ")}]":
            depth -= 1
        elif ch == delimiter and depth == 0:
            parts.append(source[start:i].strip())
            start = i + 1
    parts.append(source[start:].strip())
    return parts


def parse_statement(source: str) -> Statement | None:
    source = _strip_comment(source).strip()
    if not source:
        return None
    m = re.fullmatch(r"\[([A-Za-z_]\w*)\]\s*=\s*['\"](.*?)['\"]", source)
    if m:
        return VariableDeclaration(m[1], m[2], None)
    m = re.fullmatch(r"(limits|limfix)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*=\s*([\[(])\s*(.*?)\s*,\s*(.*?)\s*([\])])", source, re.I)
    if m:
        low = parse_expression(m[4]) if m[4] else None
        high = parse_expression(m[5]) if m[5] else None
        return LimitDeclaration(m[1].lower(), m[2], low, high, m[3] == "[", m[6] == "]")
    m = re.match(r"vardef\s*\(\s*([A-Za-z_]\w*)\s*\)\s*=\s*(.*)\Z", source, re.I)
    if m:
        parts = _split_top(m[2], ";")
        def field(value: str) -> str | None:
            value = value.strip()
            return value[1:-1] if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0] else value or None
        return VariableDeclaration(m[1], field(parts[0]), field(parts[1]) if len(parts) > 1 else None)
    m = re.match(r"(inc0?|output|event|constant)\s*\(", source, re.I)
    if m:
        if m[1].lower() in ("inc", "inc0"):
            init = re.fullmatch(r"(inc0?)\s*\(\s*([A-Za-z_]\w*)\s*\)\s*=\s*(.*)", source, re.I | re.S)
            if init:
                return Initializer(init[2], parse_expression(init[3]), init[1].lower() == "inc0")
        call = parse_expression(source)
        if not isinstance(call, Call):
            raise _coded_error(ParseError, 'invalid_dsl_expr_data', source)
        return Directive(call.name.lower(), call.args)
    m = re.match(r"([A-Za-z_]\w*)(\.)?\s*=\s*(.*)\Z", source, re.S)
    if m:
        return Assignment(m[1], parse_expression(m[3]), bool(m[2]))
    raise _coded_error(ParseError, 'invalid_dsl_expr_data', f'Unknown statement: {source}')


def parse_equations(lines: Sequence[str]) -> tuple[Statement, ...]:
    return tuple(stmt for line in logical_lines(lines) if (stmt := parse_statement(line)) is not None)


_COMPARATORS = {'<': lambda a, b: a < b, '<=': lambda a, b: a <= b,
                '>': lambda a, b: a > b, '>=': lambda a, b: a >= b,
                '=': lambda a, b: a == b, '==': lambda a, b: a == b,
                '!=': lambda a, b: a != b, '<>': lambda a, b: a != b}


_PURE = {"abs": abs, "min": min, "max": max, "sqrt": math.sqrt, "exp": math.exp,
         "sin": math.sin, "cos": math.cos, "tan": math.tan, "atan": math.atan,
         "acos": math.acos, "pow": pow, "sqr": lambda x: x * x,
         "modulo": lambda x, y: x % y,
         "lim": lambda x, low, high: min(max(x, low), high),
         "lim_const": lambda x, low, high: min(max(x, low), high),
         "limstate": lambda x, low, high: min(max(x, low), high),
         "limstate_const": lambda x, low, high: min(max(x, low), high)}


_CURVE_CACHE: dict[int, tuple] = {}


def _prepare_curve(points: Sequence[Sequence[float]]) -> tuple:
    """Conserva unidades y orden, memoriza solo tuplas de float inmutables."""
    if len(points) < 2 or any(len(pair) != 2 for pair in points):
        raise _coded_error(ValueError, 'missing_dsl_expr_data', 'DSL curve requires at least two x,y pairs')
    values = [(float(row[0]), float(row[1])) for row in points]
    # Un punto repetido idéntico no añade segmento ni ambigüedad (array_K
    # del PPC la planta multipuerto). Una abscisa repetida con distinta y sigue fallando.
    values = [pair for i, pair in enumerate(values) if i == 0 or pair != values[i-1]]
    if any(not (math.isfinite(a) and math.isfinite(b)) for a, b in values):
        raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'DSL curve values must be finite')
    if any(b[0] <= a[0] for a, b in zip(values, values[1:])):
        raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'DSL curve abscissae must be strictly increasing')
    values = tuple(values)
    if type(points) is tuple and all(type(row) is tuple and
            all(type(value) is float for value in row) for row in points):
        if len(_CURVE_CACHE) >= 128:
            _CURVE_CACHE.clear()
        # La identidad distingue curvas iguales que contienen +0.0 y -0.0.
        # La referencia retenida impide reutilizar su id mientras esté en caché.
        _CURVE_CACHE[id(points)] = points, values
    return values


def linear_lookup(x: float, points: Sequence[Sequence[float]], *,
                  inverse: bool = False) -> float:
    'Interpolate REF piecewise-linear curves. Inverse lookup may skip a plateau outside the requested value. Reject multivalued inverse results, invalid ordering and nonfinite points. Preserve signed zero.'
    cached = _CURVE_CACHE.get(id(points))
    values = (cached[1] if cached is not None and cached[0] is points
              else _prepare_curve(points))
    if inverse:
        if len({y for _, y in values}) == 1:
            raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'Inverse DSL curve ordinates must be strictly increasing')
        roots = []
        for (x0, y0), (x1, y1) in zip(values, values[1:]):
            if y0 == y1:
                if x == y0:
                    roots.extend((x0, x1))
                continue
            if min(y0, y1) <= x <= max(y0, y1):
                roots.append(x0 + (x-y0)*(x1-x0)/(y1-y0))
        if roots:
            if max(roots)-min(roots) > 1e-10:
                raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'Inverse DSL curve is multivalued')
            return roots[0]
        first, last = values[0][1], values[-1][1]
        if min(y for _, y in values) <= x <= max(y for _, y in values):
            raise _coded_error(ValueError, 'invalid_dsl_expr_data', 'Inverse DSL curve has no interpolatable segment')
        return values[0][0] if abs(x-first) <= abs(x-last) else values[-1][0]
    if x <= values[0][0]:
        return values[0][1]
    for (x0, y0), (x1, y1) in zip(values, values[1:]):
        if x <= x1:
            return y0 + (x-x0)*(y1-y0)/(x1-x0)
    return values[-1][1]


def evaluate(expr: Expr, values: Mapping[str, object], *, time_s: float | None = None,
             runtime: object | None = None) -> float | str:
    'Evaluate the DSL syntax tree without committing optional memory during Newton.'
    if isinstance(expr, Number):
        return expr.value
    if isinstance(expr, String):
        return expr.value
    if isinstance(expr, Name):
        if expr.value not in values:
            raise _coded_error(KeyError, 'missing_dsl_signal', expr.value)
        return values[expr.value]
    if isinstance(expr, Unary):
        v = evaluate(expr.operand, values, time_s=time_s, runtime=runtime)
        return {"+": lambda: +v, "-": lambda: -v, ".not.": lambda: float(not v)}[expr.op]()
    if isinstance(expr, Binary):
        a = evaluate(expr.left, values, time_s=time_s, runtime=runtime)
        if expr.op == ".and." and not a:
            return 0.0
        if expr.op == ".or." and a:
            return 1.0
        if expr.op in ('<', '<=', '>', '>=', '=', '==', '!=', '<>') and runtime is not None:
            b = evaluate(expr.right, values, time_s=time_s, runtime=runtime)
            return runtime.compare(id(expr), expr.op, a, b)
        b = evaluate(expr.right, values, time_s=time_s, runtime=runtime)
        ops = {"+": lambda: a+b, "-": lambda: a-b, "*": lambda: a*b,
               "/": lambda: a/b, "%": lambda: a%b, "^": lambda: a**b,
               "=": lambda: float(a == b), "==": lambda: float(a == b),
               "!=": lambda: float(a != b), "<>": lambda: float(a != b),
               "<": lambda: float(a < b), ">": lambda: float(a > b),
               "<=": lambda: float(a <= b), ">=": lambda: float(a >= b),
               ".and.": lambda: float(bool(a) and bool(b)),
               ".or.": lambda: float(bool(a) or bool(b))}
        return ops[expr.op]()
    name = expr.name.lower()
    if name in ("select", "select_const", "selfix", "selfix_const"):
        if len(expr.args) != 3:
            raise _coded_error(ValueError, 'missing_dsl_expr_data', f'{name} requires three arguments')
        return evaluate(expr.args[1 if evaluate(expr.args[0], values, time_s=time_s,
                                                runtime=runtime) else 2], values,
                        time_s=time_s, runtime=runtime)
    if name == "time" and not expr.args and time_s is not None:
        return time_s
    if name == "picontrol_const":
        if len(expr.args) != 4:
            raise _coded_error(ValueError, 'missing_dsl_expr_data', 'picontrol_const requires a state, limits and a proportional term')
        state, low, high, prop = (float(evaluate(arg, values, time_s=time_s, runtime=runtime))
                                  for arg in expr.args)
        return min(max(state + prop, low), high)
    if name in ("lapprox", "invlapprox"):
        if len(expr.args) != 2:
            raise _coded_error(ValueError, 'missing_dsl_expr_data', f'{name} requires an input and a curve')
        x = float(evaluate(expr.args[0], values, time_s=time_s, runtime=runtime))
        curve = evaluate(expr.args[1], values, time_s=time_s, runtime=runtime)
        return linear_lookup(x, curve, inverse=name == "invlapprox")
    if name in ("picdro", "picdro_const", "flipflop", "aflipflop", "delay",
                "gradlim_const", "lastvalue", "movingavg"):
        if runtime is None or time_s is None:
            raise _coded_error(UnsupportedFunction, 'unsupported_dsl_expr_data', f'{name} requires temporal memory')
        args = tuple(float(evaluate(arg, values, time_s=time_s, runtime=runtime))
                     for arg in expr.args)
        return runtime.read(id(expr), name, float(time_s), args)
    if name in ('lim', 'lim_const', 'limstate', 'limstate_const') and runtime is not None:
        return runtime.limit(id(expr), *(evaluate(arg, values, time_s=time_s, runtime=runtime)
                                         for arg in expr.args))
    if name in _PURE:
        return _PURE[name](*(evaluate(arg, values, time_s=time_s, runtime=runtime)
                             for arg in expr.args))
    raise _coded_error(UnsupportedFunction, 'unsupported_dsl_expr_data', expr.name)
