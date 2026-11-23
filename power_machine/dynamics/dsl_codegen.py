'Dsl codegen support for power-system RMS dynamics.'

from __future__ import annotations
from power_machine.dynamics.errors import coded_error as _coded_error

import ast
import copy
from typing import Callable, Mapping

from .dsl_expr import (Binary, Call, Expr, Name, Number, String, Unary,
                       UnsupportedFunction, _PURE, linear_lookup)


_MEMORY = frozenset({"picdro", "picdro_const", "flipflop", "aflipflop",
                     "delay", "gradlim_const", "lastvalue", "movingavg"})
_SELECT = frozenset({"select", "select_const", "selfix", "selfix_const"})
_BINARY = {"+": ast.Add, "-": ast.Sub, "*": ast.Mult, "/": ast.Div,
           "%": ast.Mod, "^": ast.Pow}
_COMPARE = {"=": ast.Eq, "==": ast.Eq, "!=": ast.NotEq, "<>": ast.NotEq,
            "<": ast.Lt, ">": ast.Gt, "<=": ast.LtE, ">=": ast.GtE}


def _name(value: str) -> ast.expr:
    return ast.Name(id=value, ctx=ast.Load())


def _call(name: str, *args: ast.expr) -> ast.Call:
    return ast.Call(func=_name(name), args=list(args), keywords=[])


def _generate(expr: Expr) -> ast.expr:
    if isinstance(expr, Number):
        return ast.Constant(expr.value)
    if isinstance(expr, String):
        return ast.Constant(expr.value)
    if isinstance(expr, Name):
        return ast.Subscript(value=_name("values"), slice=ast.Constant(expr.value), ctx=ast.Load())
    if isinstance(expr, Unary):
        value = _generate(expr.operand)
        if expr.op in ("+", "-"):
            return ast.UnaryOp(op=ast.UAdd() if expr.op == "+" else ast.USub(), operand=value)
        if expr.op == ".not.":
            return _call("float", ast.UnaryOp(op=ast.Not(), operand=value))
        raise _coded_error(ValueError, 'invalid_dsl_codegen_data', f'DSL operator is not allowed: {expr.op}')
    if isinstance(expr, Binary):
        left, right = _generate(expr.left), _generate(expr.right)
        if expr.op in _BINARY:
            return ast.BinOp(left=left, op=_BINARY[expr.op](), right=right)
        if expr.op in _COMPARE:
            natural = ast.Compare(left=left, ops=[_COMPARE[expr.op]()], comparators=[right])
            choice = ast.Call(func=ast.Attribute(value=_name('runtime'), attr='compare', ctx=ast.Load()),
                              args=[ast.Constant(id(expr)), ast.Constant(expr.op), left, right], keywords=[])
            return _call('float', ast.IfExp(test=ast.Compare(_name('runtime'), [ast.IsNot()],
                                                           [ast.Constant(None)]),
                                          body=choice, orelse=copy.deepcopy(natural)))
        if expr.op == ".and.":
            return ast.IfExp(test=left, body=_call("float", _call("bool", right)),
                             orelse=ast.Constant(0.0))
        if expr.op == ".or.":
            return ast.IfExp(test=left, body=ast.Constant(1.0),
                             orelse=_call("float", _call("bool", right)))
        raise _coded_error(ValueError, 'invalid_dsl_codegen_data', f'DSL operator is not allowed: {expr.op}')
    if not isinstance(expr, Call):
        raise _coded_error(TypeError, 'invalid_dsl_codegen_data', f'DSL node is not allowed: {type(expr).__name__}')
    name = expr.name.lower()
    if name in _SELECT:
        if len(expr.args) != 3:
            raise _coded_error(ValueError, 'missing_dsl_codegen_data', f'{name} requires three arguments')
        return ast.IfExp(test=_generate(expr.args[0]), body=_generate(expr.args[1]),
                         orelse=_generate(expr.args[2]))
    if name == "time":
        if expr.args:
            raise _coded_error(UnsupportedFunction, 'unsupported_dsl_codegen_data', 'time requires zero arguments')
        return _name("time_s")
    if name == "picontrol_const":
        if len(expr.args) != 4:
            raise _coded_error(ValueError, 'missing_dsl_codegen_data', 'picontrol_const requires four arguments')
        state, low, high, prop = map(_generate, expr.args)
        return _call("min", _call("max", ast.BinOp(left=state, op=ast.Add(), right=prop), low), high)
    if name in ("lapprox", "invlapprox"):
        if len(expr.args) != 2:
            raise _coded_error(ValueError, 'missing_dsl_codegen_data', f'{name} requires two arguments')
        return ast.Call(func=_name("_lookup"),
                        args=[_call("float", _generate(expr.args[0])), _generate(expr.args[1])],
                        keywords=[ast.keyword(arg="inverse", value=ast.Constant(name == "invlapprox"))])
    if name in _MEMORY:
        args = ast.Tuple(elts=[_call("float", _generate(arg)) for arg in expr.args], ctx=ast.Load())
        return ast.Call(func=ast.Attribute(value=_name("runtime"), attr="read", ctx=ast.Load()),
                        args=[ast.Constant(id(expr)), ast.Constant(name),
                              _call("float", _name("time_s")), args], keywords=[])
    if name in ('lim', 'lim_const', 'limstate', 'limstate_const'):
        values = [_generate(arg) for arg in expr.args]
        natural = _call('min', _call('max', values[0], values[1]), values[2])
        frozen = ast.Call(ast.Attribute(_name('runtime'), 'limit', ast.Load()),
                          [ast.Constant(id(expr)), *values], [])
        return ast.IfExp(ast.Compare(_name('runtime'), [ast.IsNot()], [ast.Constant(None)]),
                         frozen, copy.deepcopy(natural))
    if name in _PURE:
        function = ast.Subscript(value=_name("_pure"), slice=ast.Constant(name), ctx=ast.Load())
        return ast.Call(func=function, args=[_generate(arg) for arg in expr.args], keywords=[])
    raise _coded_error(UnsupportedFunction, 'unsupported_dsl_codegen_data', expr.name)


def compile_expression(expr: Expr) -> Callable[[Mapping[str, object], float | None, object | None], object]:
    'Compile only a recognized DSL syntax tree. Preserve AST call identity for memory keys.'
    body = ast.Return(value=_generate(expr))
    function = ast.FunctionDef(name="_dsl_expression",
                               args=ast.arguments(posonlyargs=[],
                                                  args=[ast.arg(arg="values"), ast.arg(arg="time_s"),
                                                        ast.arg(arg="runtime")],
                                                  vararg=None, kwonlyargs=[], kw_defaults=[],
                                                  kwarg=None, defaults=[]),
                               body=[body], decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[function], type_ignores=[]))
    namespace: dict[str, object] = {}
    # Este código proviene únicamente de _generate, nunca de una cadena DSL.
    exec(compile(module, "<dsl-validado>", "exec"),
         {"__builtins__": {}, "_pure": _PURE, "_lookup": linear_lookup,
          "float": float, "bool": bool, "min": min, "max": max}, namespace)
    return namespace["_dsl_expression"]
