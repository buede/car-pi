"""The small expression language used by definition files.

Two things in car-pi accept expressions from data: PID decode formulas (``U / 4``)
and inspection rules (``dtc.permanent_count > 0``). Both are loaded from YAML that
may have arrived via a pull request, so neither may reach :func:`eval`.

This module parses with :mod:`ast`, rejects every node type it does not explicitly
allow, and then walks the tree with its own interpreter. No attacker-influenced text
reaches the Python compiler, and no attribute lookup, subscript, comprehension,
lambda, or arbitrary call is reachable.

Names
-----
Dotted names (``pid.engine_rpm``) are resolved as single flat keys against the fact
mapping -- they are not real attribute access. :attr:`Expression.names` reports every
name an expression references, which callers use to decide *applicability* before
evaluating. That distinction matters: a rule that references a fact the scan never
produced must be skipped, not silently evaluated as ``False``. A car that refuses to
answer a question must never be reported as healthy on the strength of its silence.
"""

from __future__ import annotations

import ast
import operator
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

__all__ = ["Expression", "ExpressionError", "MissingFact", "compile_expression"]


class ExpressionError(Exception):
    """The expression is not valid in car-pi's expression language."""


class MissingFact(LookupError):
    """A name the expression references was absent at evaluation time."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"no such fact: {name!r}")


_BIN_OPS: dict[type[ast.operator], Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_COMPARE_OPS: dict[type[ast.cmpop], Any] = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}

_UNARY_OPS: dict[type[ast.unaryop], Any] = {
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
    ast.Not: operator.not_,
}

# Deliberately tiny. Every addition here widens what a definition file can do.
_FUNCTIONS: dict[str, Any] = {
    "abs": abs,
    "min": min,
    "max": max,
    "round": round,
}

# Exponentiation is allowed because byte-window arithmetic occasionally wants
# 2 ** 24, but an unbounded exponent is a denial-of-service primitive.
_MAX_EXPONENT = 64

# Definition files are YAML, so a contributor writing a rule will reach for YAML's
# spelling of these. Python's parser sees a bare `false` as a *name*, which would
# otherwise be treated as a fact the vehicle failed to report -- silently turning a
# working rule into a skipped one. Both spellings are accepted.
_LITERALS: dict[str, Any] = {
    "true": True,
    "false": False,
    "null": None,
    "True": True,
    "False": False,
    "None": None,
}


def _literal(node: ast.AST) -> tuple[bool, Any]:
    """Return ``(True, value)`` if *node* is a bare literal keyword such as ``false``."""
    if isinstance(node, ast.Name) and node.id in _LITERALS:
        return True, _LITERALS[node.id]
    return False, None


def _dotted_name(node: ast.AST) -> str | None:
    """Return ``"a.b.c"`` if *node* is a plain name or dotted chain, else ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        return None if parent is None else f"{parent}.{node.attr}"
    return None


def _collect(node: ast.AST) -> frozenset[str]:
    """Validate *node* recursively and return the names it references.

    Raises :class:`ExpressionError` for anything not explicitly permitted, so the
    default for an unrecognised construct is rejection rather than acceptance.
    """
    is_literal, _ = _literal(node)
    if is_literal:
        return frozenset()

    dotted = _dotted_name(node)
    if dotted is not None:
        # A bare name that happens to match a builtin function is only legal as the
        # callee of a Call, which is handled below and never reaches here.
        if dotted in _FUNCTIONS:
            raise ExpressionError(f"{dotted!r} is a function and must be called")
        return frozenset({dotted})

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool | int | float | str) or node.value is None:
            return frozenset()
        raise ExpressionError(f"unsupported constant: {node.value!r}")

    if isinstance(node, ast.BoolOp):
        return frozenset().union(*(_collect(v) for v in node.values))

    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise ExpressionError(f"unsupported unary operator: {type(node.op).__name__}")
        return _collect(node.operand)

    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise ExpressionError(f"unsupported operator: {type(node.op).__name__}")
        return _collect(node.left) | _collect(node.right)

    if isinstance(node, ast.Compare):
        for op in node.ops:
            if type(op) not in _COMPARE_OPS:
                raise ExpressionError(f"unsupported comparison: {type(op).__name__}")
        return _collect(node.left).union(*(_collect(c) for c in node.comparators))

    if isinstance(node, ast.IfExp):
        return _collect(node.test) | _collect(node.body) | _collect(node.orelse)

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _FUNCTIONS:
            allowed = ", ".join(sorted(_FUNCTIONS))
            raise ExpressionError(f"only these functions may be called: {allowed}")
        if node.keywords:
            raise ExpressionError("keyword arguments are not supported")
        return frozenset().union(frozenset(), *(_collect(a) for a in node.args))

    raise ExpressionError(f"unsupported syntax: {type(node).__name__}")


def _evaluate(node: ast.AST, facts: Mapping[str, Any]) -> Any:
    is_literal, value = _literal(node)
    if is_literal:
        return value

    dotted = _dotted_name(node)
    if dotted is not None:
        try:
            return facts[dotted]
        except KeyError:
            raise MissingFact(dotted) from None

    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.BoolOp):
        # Short-circuits, which is safe only because callers check applicability via
        # Expression.names before evaluating. See the module docstring.
        if isinstance(node.op, ast.And):
            result: Any = True
            for value in node.values:
                result = _evaluate(value, facts)
                if not result:
                    return result
            return result
        for value in node.values:
            result = _evaluate(value, facts)
            if result:
                return result
        return result

    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_evaluate(node.operand, facts))

    if isinstance(node, ast.BinOp):
        left = _evaluate(node.left, facts)
        right = _evaluate(node.right, facts)
        if isinstance(node.op, ast.Pow) and abs(right) > _MAX_EXPONENT:
            raise ExpressionError(f"exponent {right} exceeds the limit of {_MAX_EXPONENT}")
        try:
            return _BIN_OPS[type(node.op)](left, right)
        except ZeroDivisionError:
            raise ExpressionError("division by zero") from None

    if isinstance(node, ast.Compare):
        left = _evaluate(node.left, facts)
        for op, comparator_node in zip(node.ops, node.comparators, strict=True):
            right = _evaluate(comparator_node, facts)
            if not _COMPARE_OPS[type(op)](left, right):
                return False
            left = right
        return True

    if isinstance(node, ast.IfExp):
        branch = node.body if _evaluate(node.test, facts) else node.orelse
        return _evaluate(branch, facts)

    if isinstance(node, ast.Call):
        assert isinstance(node.func, ast.Name)  # guaranteed by _collect
        args = [_evaluate(a, facts) for a in node.args]
        return _FUNCTIONS[node.func.id](*args)

    raise ExpressionError(f"unsupported syntax: {type(node).__name__}")


@dataclass(frozen=True)
class Expression:
    """A validated expression, safe to evaluate against a fact mapping."""

    source: str
    names: frozenset[str]
    _body: ast.expr

    def evaluate(self, facts: Mapping[str, Any]) -> Any:
        """Evaluate against *facts*.

        Raises :class:`MissingFact` if a referenced name is absent, and
        :class:`ExpressionError` for arithmetic faults such as division by zero.
        """
        return _evaluate(self._body, facts)

    def is_applicable(self, facts: Mapping[str, Any]) -> bool:
        """True if every name this expression references is present in *facts*."""
        return all(name in facts for name in self.names)

    def __str__(self) -> str:
        return self.source


def compile_expression(source: str) -> Expression:
    """Parse and validate *source*, returning an :class:`Expression`.

    Raises :class:`ExpressionError` if the text is not parseable or uses any
    construct outside the permitted subset.
    """
    if not source or not source.strip():
        raise ExpressionError("expression is empty")
    try:
        tree = ast.parse(source, mode="eval")
    except SyntaxError as exc:
        raise ExpressionError(f"could not parse {source!r}: {exc.msg}") from exc
    names = _collect(tree.body)
    return Expression(source=source, names=names, _body=tree.body)
