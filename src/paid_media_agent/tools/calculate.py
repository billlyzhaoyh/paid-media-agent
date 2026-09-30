"""Model-facing `calculate` tool: arithmetic done in code, never in prose.

Tools return the figures an analysis rests on; a figure none of them returned (a total, a
difference, a percentage change, a share, a monthly amount) is computed here from figures the
tools did return. Expressions are parsed, never executed: only number literals, the four
operators, parentheses, and a few named functions are admitted, in exact decimal arithmetic.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Context, Decimal, DivisionByZero, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import sanitize_exception

CALCULATE_TOOL = "calculate"
MAX_EXPRESSION_CHARS = 300
MAX_CALCULATIONS = 20
MAX_MAGNITUDE = Decimal("1e15")
_CONTEXT = Context(prec=28, traps=[DivisionByZero, InvalidOperation])
_THOUSANDS = re.compile(r"\d,\d{3}(?!\d)")

Format = Literal["number", "percent", "money"]


class CalculationError(ValueError):
    """An expression that is refused or cannot be computed; the message is safe to show."""


class Calculation(BaseModel):
    label: str = Field(max_length=80, description="What the figure is, e.g. 'monthly impact'.")
    expression: str = Field(
        max_length=MAX_EXPRESSION_CHARS,
        description=(
            "Arithmetic on figures from earlier tool results, e.g. '(216 - 180) * 30' or "
            "'pct_change(26.04, 27.97)'. Numbers without thousands separators, + - * /, "
            "parentheses, and sum, mean, min, max, abs, round, pct_change(new, old), "
            "share(part, whole)."
        ),
    )
    format: Format = Field(
        default="number",
        description="number (2 dp), percent (a fraction shown x100 with %), or money (2 dp).",
    )


class CalculateArgs(BaseModel):
    calculations: list[Calculation] = Field(min_length=1, max_length=MAX_CALCULATIONS)


def _ratio(a: Decimal, b: Decimal) -> Decimal:
    if b == 0:
        raise CalculationError("division by zero")
    return _CONTEXT.divide(a, b)


def _round(value: Decimal, places: Decimal = Decimal(0)) -> Decimal:
    if places != places.to_integral_value() or not 0 <= places <= 10:
        raise CalculationError("round takes a whole number of places from 0 to 10")
    return value.quantize(Decimal(1).scaleb(-int(places)), rounding=ROUND_HALF_UP)


def _need(name: str, count: int, args: list[Decimal], *, exact: bool = True) -> None:
    if (exact and len(args) != count) or (not exact and len(args) < count):
        raise CalculationError(f"{name} takes {'exactly' if exact else 'at least'} {count}")


def _call(name: str, args: list[Decimal]) -> Decimal:
    if name in ("sum", "mean", "min", "max"):
        _need(name, 1, args, exact=False)
    if name == "sum":
        return sum(args, Decimal(0))
    if name == "mean":
        return _ratio(sum(args, Decimal(0)), Decimal(len(args)))
    if name == "min":
        return min(args)
    if name == "max":
        return max(args)
    if name == "abs":
        _need(name, 1, args)
        return abs(args[0])
    if name == "round":
        if len(args) not in (1, 2):
            raise CalculationError("round takes a value and optional places")
        return _round(*args)
    if name == "pct_change":
        _need(name, 2, args)
        return _ratio(args[0], args[1]) - 1
    if name == "share":
        _need(name, 2, args)
        return _ratio(args[0], args[1])
    raise CalculationError(f"unknown function {name}")


FUNCTIONS = ("sum", "mean", "min", "max", "abs", "round", "pct_change", "share")
_OPERATORS: dict[type[ast.operator], Callable[[Decimal, Decimal], Decimal]] = {
    ast.Add: lambda a, b: _CONTEXT.add(a, b),
    ast.Sub: lambda a, b: _CONTEXT.subtract(a, b),
    ast.Mult: lambda a, b: _CONTEXT.multiply(a, b),
    ast.Div: _ratio,
}


def _evaluate(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Expression):
        return _evaluate(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, int | float):
            raise CalculationError("only numbers are allowed")
        value = Decimal(str(node.value))
        if not value.is_finite() or abs(value) > MAX_MAGNITUDE:
            raise CalculationError("a number is out of range")
        return value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub | ast.UAdd):
        value = _evaluate(node.operand)
        return -value if isinstance(node.op, ast.USub) else value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        result = _OPERATORS[type(node.op)](_evaluate(node.left), _evaluate(node.right))
        if abs(result) > MAX_MAGNITUDE:
            raise CalculationError("result is out of range")
        return result
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS:
            raise CalculationError(f"only these functions are allowed: {', '.join(FUNCTIONS)}")
        if node.keywords:
            raise CalculationError("functions take positional arguments only")
        args: list[Decimal] = []
        for arg in node.args:
            if isinstance(arg, ast.List | ast.Tuple):  # sum([a, b]) reads naturally too
                args += [_evaluate(item) for item in arg.elts]
            else:
                args.append(_evaluate(arg))
        return _call(node.func.id, args)
    raise CalculationError(
        "only numbers, + - * /, parentheses, and the listed functions are allowed"
    )


def evaluate(expression: str) -> Decimal:
    """The value of `expression`, or `CalculationError` saying why it is refused."""
    text = expression.strip().replace("−", "-")
    if _THOUSANDS.search(text):
        # 1,234 is ambiguous inside a call (max(1,234)), so separators are refused, not guessed.
        raise CalculationError(
            "write numbers without thousands separators (1234.50), and put a space after each "
            "comma between arguments (max(1, 234))"
        )
    if not text:
        raise CalculationError("empty expression")
    if len(text) > MAX_EXPRESSION_CHARS:
        raise CalculationError(f"longer than {MAX_EXPRESSION_CHARS} characters")
    try:
        tree = ast.parse(text, mode="eval")
    except (SyntaxError, ValueError) as exc:
        raise CalculationError("not an arithmetic expression") from exc
    try:
        return _evaluate(tree)
    except (DivisionByZero, InvalidOperation) as exc:
        raise CalculationError("division by zero or an undefined result") from exc
    except RecursionError as exc:
        raise CalculationError("too deeply nested") from exc


def display(value: Decimal, fmt: Format) -> str:
    if fmt == "percent":
        return f"{_round(value * 100, Decimal(1))}%"
    rounded = _round(value, Decimal(2))
    return f"{rounded:,.2f}" if fmt == "money" else f"{rounded:.2f}"


def calculate(calculations: list[Calculation]) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for item in calculations:
        row: dict[str, Any] = {"label": item.label, "expression": item.expression}
        try:
            value = evaluate(item.expression)
        except CalculationError as exc:
            row.update(error=True, detail=str(exc))
        else:
            row.update(value=float(value), display=display(value, item.format))
        results.append(row)
    return results


def build_calculate_tool() -> ToolSpec:
    def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = CalculateArgs.model_validate(kwargs)
        except ValidationError as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc)})
        return json.dumps(
            {
                "results": calculate(args.calculations),
                "note": (
                    "Quote each display as it is. Every number in an expression must come from an "
                    "earlier tool result; a calculation cannot make an unsourced figure true."
                ),
            }
        )

    return ToolSpec(
        name=CALCULATE_TOOL,
        description=(
            "Compute figures no tool returned (totals, differences, percentage changes, shares, "
            "per-day or per-month amounts) from figures earlier tools did return. Never do "
            "arithmetic in prose; several calculations can go in one call. Read-only."
        ),
        parameters=parameters_for(CalculateArgs),
        handler=_run,
        offload=False,
    )
