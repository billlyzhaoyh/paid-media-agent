"""`doctor --live`: does each configured account's live read path actually produce history?

For each account it checks, in order, and stops at the first failure with what to fix:
1. the platform's catalog loaded;
2. a read contract matches the catalog, and how far that contract is verified;
3. the contract's tools are authorized reads whose schemas have the arguments it uses;
4. the account id has the shape the platform's tools expect;
5. a read of the account's yesterday normalizes into rows with spend, and conversions are present;
6. campaign budgets land in a plausible range of daily spend (a wrong money unit is 100x off);
7. how many provider calls a daily sync will make.

Reads made here are real reads: they land in history labelled `doctor`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta
from statistics import median
from typing import TYPE_CHECKING

from paid_media_agent.analytics.sync import account_yesterday
from paid_media_agent.config import AccountBinding
from paid_media_agent.doctor import Check
from paid_media_agent.domain.common import Platform
from paid_media_agent.store.db import utc_now
from paid_media_agent.tools.catalog import qualified_name
from paid_media_agent.tools.contracts import candidates, contract_for, schema_gaps
from paid_media_agent.tools.reads import ACCOUNT_ALIAS_ARG, ReadDenied, ReadResult

if TYPE_CHECKING:
    from paid_media_agent.runtime.self_hosted import SelfHostedRuntime

READ_DAYS = 3
"""Days `doctor --live` reads, newest first for per-day contracts, until one has rows."""
BUDGET_TO_SPEND = (0.2, 10.0)
"""Median daily budget / daily spend outside this range suggests a wrong money unit."""
_ID_SHAPES: dict[Platform, tuple[re.Pattern[str], str]] = {
    Platform.META_ADS: (re.compile(r"^act_\d+$"), "Meta ad account ids look like act_1234567890"),
    Platform.GOOGLE_ADS: (
        re.compile(r"^\d{10}$"),
        "Google Ads customer ids are ten digits without dashes",
    ),
}


async def run_live_checks(
    runtime: SelfHostedRuntime,
    *,
    aliases: tuple[str, ...] = (),
    clock: Callable[[], datetime] = utc_now,
) -> list[Check]:
    accounts = runtime.profile.accounts
    selected = aliases or accounts.aliases()
    failures = getattr(runtime.profile.catalog_provider, "failures", {})
    checks: list[Check] = []
    for alias in selected:
        binding = accounts.resolve(alias)
        if binding is None:
            checks.append(Check(f"live:{alias}", "fail", "unknown alias"))
            continue
        checks.extend(await _check_account(runtime, binding, failures, clock()))
    return checks


async def _check_account(
    runtime: SelfHostedRuntime,
    binding: AccountBinding,
    failures: dict[Platform, str],
    now: datetime,
) -> list[Check]:
    name = f"live:{binding.alias}"
    platform = binding.platform
    catalog = runtime.catalog
    if platform in failures:
        return [
            Check(
                name,
                "fail",
                f"{platform.value} catalog did not load ({failures[platform]}); "
                "check PIPEBOARD_API_TOKEN and that the platform is connected in Pipeboard",
            )
        ]
    contract = contract_for(catalog, platform)
    if contract is None:
        tried = "; ".join(
            f"{c.name}: {', '.join(schema_gaps(c, catalog, platform)) or 'no gaps'}"
            for c in candidates(platform)
        )
        return [Check(name, "fail", f"no read contract matches the catalog ({tried})")]
    checks = [
        Check(
            f"{name}:contract",
            "ok",
            f"{contract.name}, {contract.verification.replace('_', ' ')}",
        )
    ]
    gaps = schema_gaps(contract, catalog, platform)
    if gaps:
        checks.append(
            Check(
                f"{name}:schema",
                "fail",
                "; ".join(gaps) + ". The live tools differ from the contract; update "
                "tools/contracts.py from the live schema",
            )
        )
        return checks
    shape = _ID_SHAPES.get(platform) if contract.verification != "host" else None
    if shape and not shape[0].match(binding.provider_account_id):
        checks.append(Check(f"{name}:account_id", "warn", f"{shape[1]} (config/accounts.toml)"))

    end = account_yesterday(binding, now)
    start = end - timedelta(days=READ_DAYS - 1)
    entry = catalog.get(qualified_name(platform, contract.performance_tool))
    schema = entry.input_schema if entry is not None else {}
    label = f"{start}..{end}"
    result: ReadResult | None = None
    for call in reversed(contract.performance_calls(start, end, schema)):
        try:
            result = await runtime.components.read_dispatcher.execute(
                qualified_name(platform, call.tool),
                {ACCOUNT_ALIAS_ARG: binding.alias, **call.arguments},
                source="doctor",
            )
        except ReadDenied as exc:
            detail = f"{label}: {exc.reason} {exc.detail}".strip()
            checks.append(Check(f"{name}:read", "fail", detail))
            return checks
        except Exception as exc:
            detail = f"{label}: {type(exc).__name__}: {exc}"[:300]
            checks.append(Check(f"{name}:read", "fail", detail))
            return checks
        if result.artifact_kind == "performance_rows":
            break
    assert result is not None  # noqa: S101 - a contract always asks for at least one call
    checks.append(_read_check(name, label, result))
    if result.artifact_kind == "performance_rows" and "conversions" in result.missing_fields:
        seen = result.preview.get("action_types")
        hint = (
            f"; set conversion_action in config/accounts.toml, e.g. one of {seen}" if seen else ""
        )
        checks.append(Check(f"{name}:conversions", "warn", "no conversions in the rows" + hint))

    listing = contract.settings_call()
    if listing is not None:
        try:
            await runtime.components.read_dispatcher.execute(
                qualified_name(platform, listing.tool),
                {ACCOUNT_ALIAS_ARG: binding.alias, **listing.arguments},
                source="doctor",
            )
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ReadDenied) else type(exc).__name__
            checks.append(Check(f"{name}:settings", "fail", f"campaign settings: {reason}"))
        else:
            checks.append(_budget_check(name, runtime, binding))

    signal_call = contract.signals_call(start, end)
    if signal_call is not None and catalog.get(qualified_name(platform, signal_call.tool)):
        try:
            found = await runtime.components.read_dispatcher.execute(
                qualified_name(platform, signal_call.tool),
                {ACCOUNT_ALIAS_ARG: binding.alias, **signal_call.arguments},
                source="doctor",
            )
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ReadDenied) else type(exc).__name__
            checks.append(
                Check(
                    f"{name}:signals",
                    "warn",
                    f"what limits spend could not be read ({reason}); budget recommendations "
                    "fall back to spend against budget",
                )
            )
        else:
            recorded = (
                runtime.store.fetch(
                    "SELECT count(*) FROM entity_daily_signals WHERE account_alias = ?",
                    [binding.alias],
                )[0][0]
                + runtime.store.fetch(
                    "SELECT count(*) FROM entity_delivery_status WHERE account_alias = ?",
                    [binding.alias],
                )[0][0]
            )
            checks.append(
                Check(
                    f"{name}:signals",
                    "ok" if recorded else "warn",
                    f"{found.row_count or 0} signal rows read; impression share and status "
                    "reasons recorded"
                    if recorded
                    else "the signals read returned nothing usable; recommendations fall back to "
                    "spend against budget",
                )
            )

    per_day = contract.per_day(schema)
    days = runtime.settings.paid_media_sync_days
    span = min(days, contract.resync_days) if per_day and contract.resync_days else days
    calls = (span if per_day else 1) + (1 if listing is not None else 0) + (1 if signal_call else 0)
    checks.append(
        Check(
            f"{name}:calls",
            "ok",
            f"about {calls} provider calls per daily sync"
            + (f" (one per day for the last {span} days)" if per_day else "")
            + "; pages add more",
        )
    )
    return checks


def _read_check(name: str, day: str, result: ReadResult) -> Check:
    if result.artifact_kind != "performance_rows":
        return Check(
            f"{name}:read",
            "warn",
            f"{day}: the reads returned no daily performance rows (payload keys: "
            f"{', '.join(result.columns[:8]) or 'none'}). A day without spend is normal; "
            "otherwise the contract's row mapping needs the live shape",
        )
    return Check(
        f"{name}:read",
        "ok",
        f"{day}: {result.row_count} campaign-days, currency {result.preview.get('currency')}",
    )


def _budget_check(name: str, runtime: SelfHostedRuntime, binding: AccountBinding) -> Check:
    rows = runtime.store.fetch(
        """
        WITH latest AS (
            SELECT entity_ref, daily_budget FROM entity_settings_snapshots
            WHERE account_alias = ? AND daily_budget IS NOT NULL
            QUALIFY row_number() OVER (PARTITION BY entity_ref ORDER BY observed_at DESC) = 1
        ), spend AS (
            SELECT entity_ref, avg(spend) AS spend FROM entity_daily_latest
            WHERE account_alias = ? AND spend > 0 GROUP BY entity_ref
        )
        SELECT l.daily_budget::DOUBLE, s.spend::DOUBLE FROM latest l JOIN spend s USING (entity_ref)
        """,
        [binding.alias, binding.alias],
    )
    if not rows:
        return Check(
            f"{name}:budgets",
            "warn",
            "no campaign with both a daily budget and spend to compare; "
            "campaigns on shared, lifetime, or ad-set budgets are not allocated",
        )
    ratio = median(budget / spend for budget, spend in rows)
    low, high = BUDGET_TO_SPEND
    if low <= ratio <= high:
        return Check(
            f"{name}:budgets",
            "ok",
            f"median budget is {ratio:.1f}x daily spend across {len(rows)} campaigns",
        )
    return Check(
        f"{name}:budgets",
        "fail",
        f"median budget is {ratio:.1f}x daily spend across {len(rows)} campaigns; the budget "
        "unit is probably wrong (minor units or micros read as currency). Do not allocate or "
        "write budgets until the contract's unit is fixed",
    )
