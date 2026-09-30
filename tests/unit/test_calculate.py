"""The calculate tool: exact arithmetic in code, and nothing but arithmetic."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from paid_media_agent.harness.tools import ToolContext
from paid_media_agent.tools.calculate import CalculationError, build_calculate_tool, evaluate


@pytest.mark.parametrize(
    ("expression", "value"),
    [
        ("(216 - 180) * 30", "1080"),
        ("2 + 3 * 4", "14"),
        ("-3 * -2", "6"),
        ("0.1 + 0.2", "0.3"),
        ("sum(180, 420, 300)", "900"),
        ("sum([180, 420, 300])", "900"),
        ("mean(2, 4)", "3"),
        ("min(4, 2.5)", "2.5"),
        ("max(1, 234)", "234"),
        ("abs(-7.5)", "7.5"),
        ("round(2.345, 2)", "2.35"),
        ("round(2.5)", "3"),
        ("pct_change(26.04, 28.00)", "-0.07"),
        ("share(68, 100)", "0.68"),
        ("5 − 2", "3"),
    ],
)
def test_arithmetic_is_exact_and_follows_precedence(expression: str, value: str) -> None:
    assert evaluate(expression) == Decimal(value)


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ('__import__("os")', "only these functions"),
        ('open("x")', "only these functions"),
        ("x + 1", "only numbers"),
        ("2 ** 10", "only numbers"),
        ("(1).real", "only numbers"),
        ("[i for i in (1, 2)]", "only numbers"),
        ("lambda: 1", "only numbers"),
        ("1 if 1 else 2", "only numbers"),
        ("(1, 2)", "only numbers"),
        ('"1" + "2"', "only numbers are allowed"),
        ("sum(x=1)", "positional"),
        ("sum()", "at least 1"),
        ("pct_change(1)", "exactly 2"),
        ("round(1.5, 0.5)", "whole number"),
        ("1 / 0", "division by zero"),
        ("share(1, 0)", "division by zero"),
        ("1,234.50 + 1", "thousands separators"),
        ("max(1,234)", "thousands separators"),
        ("9" * 20, "out of range"),
        ("1e400", "out of range"),
        ("1" + " + 1" * 150, "longer than"),
        ("", "empty"),
        ("1 +", "not an arithmetic expression"),
    ],
)
def test_anything_but_arithmetic_is_refused_with_a_reason(expression: str, reason: str) -> None:
    with pytest.raises(CalculationError, match=reason):
        evaluate(expression)


def test_the_tool_computes_a_batch_and_fails_items_alone() -> None:
    tool = build_calculate_tool()
    assert tool.offload is False and tool.kind == "core"
    result = json.loads(
        tool.handler(  # type: ignore[arg-type]
            {
                "calculations": [
                    {"label": "monthly", "expression": "(216 - 180) * 30", "format": "money"},
                    {
                        "label": "change",
                        "expression": "pct_change(26.04, 27.97)",
                        "format": "percent",
                    },
                    {
                        "label": "share",
                        "expression": "share(5467.89, 9262.15)",
                        "format": "percent",
                    },
                    {"label": "bad", "expression": "open('x')"},
                    {"label": "plain", "expression": "1234.5 / 2"},
                ]
            },
            ToolContext("t", "local-user"),
        )
    )
    monthly, change, share, bad, plain = result["results"]
    assert (monthly["value"], monthly["display"]) == (1080.0, "1,080.00")
    assert change["display"] == "-6.9%" and change["value"] == pytest.approx(-0.069, abs=1e-4)
    assert share["display"] == "59.0%"
    assert bad["error"] is True and "only these functions" in bad["detail"]
    assert plain["display"] == "617.25" and "earlier tool result" in result["note"]
    empty = json.loads(tool.handler({"calculations": []}, ToolContext("t", "u")))  # type: ignore[arg-type]
    assert empty["error"] is True
