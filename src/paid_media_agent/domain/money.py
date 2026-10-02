"""Money units: what a provider's number means in account currency, and back.

Everything the agent shows, proposes, and stores is in account currency per day. Providers differ:
Meta reports and accepts budgets in the currency's minor unit (cents for USD, yen for JPY), Google
and Reddit in micros. A unit is data on a contract or a write-policy row, never a label only.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

MoneyUnit = Literal["currency", "minor", "meta_minor", "micros"]
MONEY_UNITS: tuple[MoneyUnit, ...] = ("currency", "minor", "meta_minor", "micros")

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


_META_WHOLE_UNITS = frozenset(
    ("CLP", "COP", "CRC", "HUF", "ISK", "IDR", "JPY", "KRW", "PYG", "TWD", "VND")
)
"""Meta's own currency offsets (Marketing API "Currencies": offset 1 for these, 100 for every
other currency, including ISO's three-decimal ones). They differ from ISO for COP, CRC, HUF, IDR,
and TWD (whole units at Meta) and for BHD, JOD, KWD, OMR, and TND (hundredths at Meta), which is
why Meta budgets use `meta_minor`, not `minor`. Confirm on a live account (`doctor --live`)."""


def minor_exponent(currency: str) -> int:
    return _MINOR_EXPONENT.get(currency.upper(), 2)


def meta_exponent(currency: str) -> int:
    return 0 if currency.upper() in _META_WHOLE_UNITS else 2


def scale(unit: MoneyUnit, currency: str) -> Decimal:
    """How many provider units make one unit of account currency."""
    if unit == "currency":
        return Decimal(1)
    if unit == "minor":
        return Decimal(10) ** minor_exponent(currency)
    if unit == "meta_minor":
        return Decimal(10) ** meta_exponent(currency)
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
    places = meta_exponent(currency) if unit == "meta_minor" else minor_exponent(currency)
    step = Decimal(1).scaleb(-places)
    rounded = Decimal(str(value)).quantize(step, rounding=ROUND_HALF_UP)
    if unit == "currency":
        return float(rounded)
    return int(rounded * scale(unit, currency))
