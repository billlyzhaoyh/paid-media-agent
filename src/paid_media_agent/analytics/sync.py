"""Scheduled history: pull every account's recent days and campaign settings into the store.

`run_sync` re-pulls a trailing window (28 days by default) so late conversions show up as newer
snapshots of the same days. `run_backfill` walks an older range in chunks once. Both go through
the same `ReadDispatcher` as the agent: the same catalog, scoping, artifacts, and history writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from paid_media_agent.analytics.ingest import AnalyticsSource
from paid_media_agent.config import AccountRegistry
from paid_media_agent.tools.catalog import AuthorizedToolCatalog, qualified_name
from paid_media_agent.tools.reads import ACCOUNT_ALIAS_ARG, ReadDenied, ReadDispatcher

PERFORMANCE_TOOL = "get_campaign_performance"
SETTINGS_TOOL = "list_campaigns"
DEFAULT_SYNC_DAYS = 28
BACKFILL_CHUNK_DAYS = 28


@dataclass
class SyncRun:
    source: AnalyticsSource
    start: date
    end: date
    rows: int = 0
    settings: int = 0
    reads: list[str] = field(default_factory=list)
    unavailable: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, object]:
        return {
            "source": self.source,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "rows": self.rows,
            "settings": self.settings,
            "reads": list(self.reads),
            "unavailable": list(self.unavailable),
        }


async def _pull(
    run: SyncRun,
    *,
    accounts: AccountRegistry,
    catalog: AuthorizedToolCatalog,
    dispatcher: ReadDispatcher,
    aliases: tuple[str, ...],
    windows: list[tuple[date, date]],
    settings: bool,
) -> SyncRun:
    for alias in aliases:
        binding = accounts.resolve(alias)
        if binding is None:
            run.unavailable.append(f"{alias}: unknown alias")
            continue
        performance = catalog.get(qualified_name(binding.platform, PERFORMANCE_TOOL))
        if performance is None:
            run.unavailable.append(f"{alias}: no campaign performance read tool")
            continue
        for start, end in windows:
            try:
                result = await dispatcher.execute(
                    performance.qualified_name,
                    {
                        ACCOUNT_ALIAS_ARG: alias,
                        "start_date": start.isoformat(),
                        "end_date": end.isoformat(),
                    },
                    source=run.source,
                )
            except ReadDenied as exc:
                run.unavailable.append(f"{alias} {start}..{end}: {exc.reason}")
                continue
            except Exception as exc:
                run.unavailable.append(f"{alias} {start}..{end}: {type(exc).__name__}")
                continue
            run.reads.append(result.artifact_id)
            run.rows += result.row_count or 0
        listing = catalog.get(qualified_name(binding.platform, SETTINGS_TOOL))
        if not settings or listing is None:
            continue
        try:
            result = await dispatcher.execute(
                listing.qualified_name, {ACCOUNT_ALIAS_ARG: alias}, source=run.source
            )
        except Exception as exc:
            reason = exc.reason if isinstance(exc, ReadDenied) else type(exc).__name__
            run.unavailable.append(f"{alias} settings: {reason}")
            continue
        run.reads.append(result.artifact_id)
        run.settings += result.row_count or 0
    return run


async def run_sync(
    *,
    accounts: AccountRegistry,
    catalog: AuthorizedToolCatalog,
    dispatcher: ReadDispatcher,
    end: date,
    days: int = DEFAULT_SYNC_DAYS,
    aliases: tuple[str, ...] | None = None,
) -> SyncRun:
    """Pull the trailing `days` ending on `end`, plus current campaign settings, per alias."""
    start = end - timedelta(days=days - 1)
    return await _pull(
        SyncRun(source="sync", start=start, end=end),
        accounts=accounts,
        catalog=catalog,
        dispatcher=dispatcher,
        aliases=tuple(aliases) if aliases else accounts.aliases(),
        windows=[(start, end)],
        settings=True,
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
) -> SyncRun:
    """Pull `start..end` in chunks, oldest first. Settings are only observable now, not then."""
    if end < start:
        raise ValueError("backfill end precedes start")
    windows: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        chunk_end = min(end, cursor + timedelta(days=chunk_days - 1))
        windows.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return await _pull(
        SyncRun(source="backfill", start=start, end=end),
        accounts=accounts,
        catalog=catalog,
        dispatcher=dispatcher,
        aliases=tuple(aliases) if aliases else accounts.aliases(),
        windows=windows,
        settings=False,
    )
