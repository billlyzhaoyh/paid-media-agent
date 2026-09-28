"""Money units: what a provider's number means in account currency, and back.

Everything the agent shows, proposes, and stores is in account currency per day. Providers differ:
Meta reports and accepts budgets in the currency's minor unit (cents for USD, yen for JPY), Google
and Reddit in micros. A unit is data on a contract or a write-policy row, never a label only.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

MoneyUnit = Literal["currency", "minor", "micros"]
MONEY_UNITS: tuple[MoneyUnit, ...] = ("currency", "minor", "micros")

_MINOR_EXPONENT: dict[str, int] = {
    # ISO 4217 currencies without two decimal places. Everything else has two.
    **dict.fromkeys(
        (
            "BIF", "CLP", "DJF", "GNF", "ISK", "JPY", "KMF", "KRW", "PYG", "RWF", "UGX", "VND",
            "VUV", "XAF", "XOF", "XPF",
        ),
        0,
    ),
    **dict.fromkeys(("BHD", "IQD", "JOD", "KWD", "LYD", "OMR", "TND"), 3),
}  # fmt: skip
"""ISO 4217 exponents. A provider whose own offsets differ (Meta publishes its own table) shows up
in `doctor --live`'s budget plausibility check before any number is trusted."""


def minor_exponent(currency: str) -> int:
    return _MINOR_EXPONENT.get(currency.upper(), 2)


def scale(unit: MoneyUnit, currency: str) -> Decimal:
    """How many provider units make one unit of account currency."""
    if unit == "currency":
        return Decimal(1)
    if unit == "minor":
        return Decimal(10) ** minor_exponent(currency)
    if unit == "micros":
        return Decimal(1_000_000)
    raise ValueError(f"unknown money unit {unit!r}")


def to_currency(value: Decimal | float | int | str, unit: MoneyUnit, currency: str) -> Decimal:
    """A provider number in `unit` as account currency."""
    return Decimal(str(value)) / scale(unit, currency)


def to_provider(value: Decimal | float | int | str, unit: MoneyUnit, currency: str) -> int | float:
    """Account currency as the provider's number, rounded to the currency's smallest unit first.

    Minor units and micros are whole numbers; currency stays a two-decimal float so fixture and
    currency-unit providers see exactly what they did before.
    """
    step = Decimal(1).scaleb(-minor_exponent(currency))
    rounded = Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP)
    if unit == "currency":
        return float(rounded)
    return int(rounded * scale(unit, currency))
