"""Scheduled history: pull every account's recent days and campaign settings into the store.

`run_sync` re-pulls a trailing window (28 days by default) so late conversions show up as newer
snapshots of the same days. `run_backfill` walks an older range in chunks once. Both go through
the same `ReadDispatcher` as the agent: the same catalog, scoping, artifacts, and history writes.

Which tools to call, with which arguments, is the platform's read contract (`tools/contracts.py`).
A contract that needs one call per day re-pulls only its maturity window, pages are followed, and
every provider call counts against `max_calls`, because hosted MCP plans meter calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Literal

from paid_media_agent.analytics.ingest import AnalyticsSource, local_date
from paid_media_agent.config import AccountBinding, AccountRegistry
from paid_media_agent.domain.common import JsonValue
from paid_media_agent.store.db import utc_now
from paid_media_agent.tools.catalog import AuthorizedToolCatalog, qualified_name
from paid_media_agent.tools.contracts import ReadCall, ReadContract, contract_for
from paid_media_agent.tools.reads import ACCOUNT_ALIAS_ARG, ReadDenied, ReadDispatcher

DEFAULT_SYNC_DAYS = 28
BACKFILL_CHUNK_DAYS = 28
DEFAULT_MAX_CALLS = 200
MAX_PAGES = 20
"""Pages followed per call; a longer listing is reported rather than read without end."""


@dataclass
class SyncRun:
    source: AnalyticsSource
    start: date
    end: date
    rows: int = 0
    settings: int = 0
    signals: int = 0
    calls: int = 0
    reads: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)
    contracts: dict[str, str] = field(default_factory=dict)

    def summary(self) -> dict[str, object]:
        return {
            "source": self.source,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "rows": self.rows,
            "settings": self.settings,
            "signals": self.signals,
            "calls": self.calls,
            "reads": list(self.reads),
            "unavailable": list(self.unavailable),
            "contracts": dict(self.contracts),
        }


class _CallCapReached(Exception):
    pass


def account_yesterday(binding: AccountBinding, now: datetime) -> date:
    """The last complete day in the account's own timezone."""
    return local_date(now, binding.timezone) - timedelta(days=1)


def _chunks(start: date, end: date, chunk_days: int) -> list[tuple[date, date]]:
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
        windows.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return windows


async def _call(
    run: SyncRun,
    dispatcher: ReadDispatcher,
    binding: AccountBinding,
    call: ReadCall,
    *,
    max_calls: int,
    kind: Literal["rows", "settings", "signals"] = "rows",
) -> None:
    """One contract call, following its pages. Failures are recorded, not raised."""
    label = (
        kind
        if kind != "rows"
        else f"{call.window[0]}..{call.window[1]}"
        if call.window and call.window[0] != call.window[1]
        else str(call.window[0])
        if call.window
        else call.tool
    )
    arguments: dict[str, JsonValue] | None = dict(call.arguments)
    pages = 0
    while arguments is not None:
        if run.calls >= max_calls:
            run.unavailable.append(
                f"{binding.alias} {label}: stopped at this account's share of the call limit "
                "(PAID_MEDIA_SYNC_MAX_CALLS); older windows were skipped"
            )
            raise _CallCapReached
        run.calls += 1
        try:
            result = await dispatcher.execute(
                qualified_name(binding.platform, call.tool),
                {ACCOUNT_ALIAS_ARG: binding.alias, **arguments},
                source=run.source,
            )
        except ReadDenied as exc:
            run.unavailable.append(f"{binding.alias} {label}: {exc.reason}")
            return
        except Exception as exc:
            run.unavailable.append(f"{binding.alias} {label}: {type(exc).__name__}")
            return
        run.reads.append(result.artifact_id)
        if kind == "settings":
            run.settings += result.row_count or 0
        elif kind == "signals":
            run.signals += result.row_count or 0
        elif result.artifact_kind == "performance_rows":
            run.rows += result.row_count or 0
        pages += 1
        arguments = result.next_page
        if arguments is not None and pages >= MAX_PAGES:
            run.unavailable.append(f"{binding.alias} {label}: more than {MAX_PAGES} pages")
            return


async def _pull(
    run: SyncRun,
    *,
    accounts: AccountRegistry,
    catalog: AuthorizedToolCatalog,
    dispatcher: ReadDispatcher,
    aliases: tuple[str, ...],
    windows: Callable[[AccountBinding, ReadContract, bool], list[tuple[date, date]]],
    settings: bool,
    max_calls: int,
) -> SyncRun:
    for position, alias in enumerate(aliases):
        binding = accounts.resolve(alias)
        if binding is None:
            run.unavailable.append(f"{alias}: unknown alias")
            continue
        contract = contract_for(catalog, binding.platform)
        if contract is None:
            run.unavailable.append(
                f"{alias}: no read contract matches the {binding.platform.value} catalog; "
                "run `paid-media-agent doctor --live`"
            )
            continue
        run.contracts[alias] = f"{contract.name} ({contract.verification})"
        entry = catalog.get(qualified_name(binding.platform, contract.performance_tool))
        schema = entry.input_schema if entry is not None else {}
        spans = windows(binding, contract, contract.per_day(schema))
        # Each account gets its share of the calls left, so one account that costs a call per
        # day never starves the ones after it; within it, the newest days come first.
        share = max(1, (max_calls - run.calls) // (len(aliases) - position))
        limit = min(max_calls, run.calls + share)
        try:
            calls = [
                c for start, end in spans for c in contract.performance_calls(start, end, schema)
            ]
            for call in sorted(
                calls, key=lambda c: c.window[1] if c.window else date.min, reverse=True
            ):
                await _call(run, dispatcher, binding, call, max_calls=limit)
            listing = contract.settings_call() if settings else None
            if listing is not None and catalog.get(qualified_name(binding.platform, listing.tool)):
                await _call(run, dispatcher, binding, listing, max_calls=limit, kind="settings")
            # What limits spend (impression share, status reasons), where the platform says.
            # Its own call, so a field an account cannot report never costs the performance read.
            for start, end in reversed(spans):
                signal_call = contract.signals_call(start, end)
                if signal_call is not None and catalog.get(
                    qualified_name(binding.platform, signal_call.tool)
                ):
                    await _call(
                        run, dispatcher, binding, signal_call, max_calls=limit, kind="signals"
                    )
        except _CallCapReached:
            continue
    return run


async def run_sync(
    *,
    accounts: AccountRegistry,
    catalog: AuthorizedToolCatalog,
    dispatcher: ReadDispatcher,
    end: date | None = None,
    days: int = DEFAULT_SYNC_DAYS,
    aliases: tuple[str, ...] | None = None,
    max_calls: int = DEFAULT_MAX_CALLS,
    clock: Callable[[], datetime] = utc_now,
) -> SyncRun:
    """Pull the trailing `days` ending on `end`, plus current campaign settings, per alias.

    Without `end`, each account ends on its own yesterday. A contract that costs one call per day
    re-pulls only its `resync_days`; older days come from `run_backfill`.
    """
    selected = tuple(aliases) if aliases else accounts.aliases()
    now = clock()

    def windows(
        binding: AccountBinding, contract: ReadContract, per_day: bool
    ) -> list[tuple[date, date]]:
        last = end or account_yesterday(binding, now)
        span = min(days, contract.resync_days) if per_day and contract.resync_days else days
        return [(last - timedelta(days=span - 1), last)]

    ends = [
        end or account_yesterday(b, now)
        for b in (accounts.resolve(a) for a in selected)
        if b is not None
    ] or [end or now.date() - timedelta(days=1)]
    run = SyncRun(source="sync", start=min(ends) - timedelta(days=days - 1), end=max(ends))
    return await _pull(
        run,
        accounts=accounts,
        catalog=catalog,
        dispatcher=dispatcher,
        aliases=selected,
        windows=windows,
        settings=True,
        max_calls=max_calls,
    )


async def run_backfill(
    *,
    accounts: AccountRegistry,
    catalog: AuthorizedToolCatalog,
    dispatcher: ReadDispatcher,
    start: date,
    end: date,
    aliases: tuple[str, ...] | None = None,
    chunk_days: int = BACKFILL_CHUNK_DAYS,
    max_calls: int = DEFAULT_MAX_CALLS,
) -> SyncRun:
    """Pull `start..end` in chunks, oldest first. Settings are only observable now, not then."""
    if end < start:
        raise ValueError("backfill end precedes start")
    chunks = _chunks(start, end, chunk_days)
    return await _pull(
        SyncRun(source="backfill", start=start, end=end),
        accounts=accounts,
        catalog=catalog,
        dispatcher=dispatcher,
        aliases=tuple(aliases) if aliases else accounts.aliases(),
        windows=lambda _binding, _contract, _per_day: chunks,
        settings=False,
        max_calls=max_calls,
    )
