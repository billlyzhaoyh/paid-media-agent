"""Performance-row artifacts merged per account, so a read in several pages or calls is one read.

A provider pages a long read, and some contracts read one day per call. The summary tools take
every artifact the model has and merge them per platform and account here: rows are keyed by
entity and day, a later artifact wins where two overlap, and the provider's totals are combined
only when that is still exact.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from paid_media_agent.domain.common import JsonValue
from paid_media_agent.domain.metrics import PerformanceRow
from paid_media_agent.harness.tools import FailedRead
from paid_media_agent.tools.artifacts import ArtifactRecord, ArtifactStore
from paid_media_agent.tools.compute import ComputeError
from paid_media_agent.tools.normalize import rows_from_payload

NOT_ROWS = (
    "{artifact_id} is a {kind} artifact, not performance rows: pass the artifact_id of a platform "
    "performance read (read_result with artifact_kind performance_rows); for stored history use "
    "query_history, explain_change, or check_pacing instead"
)


@dataclass
class AccountRead:
    """Every row read for one platform account, from one artifact or several."""

    platform: str
    account: str
    artifact_ids: list[str] = field(default_factory=list)
    rows: list[PerformanceRow] = field(default_factory=list)
    provider_totals: dict[str, JsonValue] | None = None
    missing_fields: tuple[str, ...] = ()
    overlapping_rows: int = 0
    """Rows that more than one artifact held; the newest artifact's copy is kept."""
    requested_from: date | None = None
    """The earliest day any of the reads asked for: data starting later does not exist."""

    @property
    def first_day(self) -> date:
        return min(r.window.start for r in self.rows)

    @property
    def last_day(self) -> date:
        return max(r.window.start for r in self.rows)


def _combined_totals(records: Sequence[ArtifactRecord]) -> dict[str, JsonValue] | None:
    """The provider's totals for the merged read, or None when they cannot be exact.

    Identical totals on every page describe the whole query (Meta repeats them); different ones
    are per page and add up. A page without totals leaves the merged read without any.
    """
    totals = [r.payload.get("provider_totals") or None for r in records]
    if not totals or any(not isinstance(t, dict) for t in totals):
        return None
    pages: list[dict[str, Any]] = [t for t in totals if isinstance(t, dict)]
    if len(pages) == 1 or all(t == pages[0] for t in pages):
        return dict(pages[0])
    combined: dict[str, JsonValue] = {}
    for key in set.intersection(*(set(t) for t in pages)):
        try:
            total = sum((Decimal(str(t[key])) for t in pages), Decimal(0))
        except (ArithmeticError, ValueError):
            continue
        combined[key] = int(total) if key == "row_count" else str(total)
    return combined or None


def load_reads(artifacts: ArtifactStore, artifact_ids: Sequence[str]) -> list[AccountRead]:
    """One `AccountRead` per platform and account, in the order the artifacts were given."""
    if not artifact_ids:
        raise ComputeError("at least one artifact id is required")
    grouped: dict[tuple[str, str], list[ArtifactRecord]] = {}
    for artifact_id in dict.fromkeys(artifact_ids):
        record = artifacts.read(artifact_id)
        empty = record.payload.get("empty_read")
        if isinstance(empty, str):
            raise ComputeError(f"{artifact_id} is an empty read. {empty}")
        if record.metadata.kind != "performance_rows":
            raise ComputeError(NOT_ROWS.format(artifact_id=artifact_id, kind=record.metadata.kind))
        rows = rows_from_payload(record.payload)
        if not rows:
            raise ComputeError(f"{artifact_id} contains no rows")
        platform = record.metadata.platform or rows[0].platform.value
        account = record.metadata.account_ref or rows[0].account_ref
        grouped.setdefault((platform, account), []).append(record)
    reads = []
    for (platform, account), records in grouped.items():
        records.sort(key=lambda r: r.metadata.created_at)
        keyed: dict[tuple[str, str, Any, Any], PerformanceRow] = {}
        overlaps = 0
        missing: set[str] = set()
        for record in records:
            for row in rows_from_payload(record.payload):
                key = (row.entity_type.value, row.entity_ref, row.window.start, row.window.end)
                overlaps += key in keyed
                keyed[key] = row
            missing.update(str(m) for m in record.payload.get("missing_fields") or [])
        reads.append(
            AccountRead(
                platform=platform,
                account=account,
                artifact_ids=[r.metadata.artifact_id for r in records],
                rows=sorted(keyed.values(), key=lambda r: (r.window.start, r.entity_ref)),
                # Overlapping reads count some rows twice in their totals: no exact check left.
                provider_totals=None if overlaps else _combined_totals(records),
                missing_fields=tuple(sorted(missing)),
                overlapping_rows=overlaps,
                requested_from=min(
                    (
                        date.fromisoformat(r.metadata.requested_window.split("..")[0])
                        for r in records
                        if r.metadata.requested_window
                    ),
                    default=None,
                ),
            )
        )
    return reads


def uncovered(reads: Sequence[AccountRead], failures: Sequence[FailedRead]) -> list[str]:
    """Failed reads for accounts none of `reads` covers, as text: those sources are unavailable.

    A failure for an account whose rows are here (its campaign list, or an earlier attempt) leaves
    that account's numbers whole.
    """
    covered = {r.account for r in reads}
    return [f.text() for f in failures if f.source not in covered]


def before_the_data(read: AccountRead, label: str, start: date, end: date) -> ComputeError:
    """Why a window that starts before the rows cannot be compared, and what would help."""
    span = f"{start.isoformat()}..{end.isoformat()}"
    first = read.first_day.isoformat()
    if read.requested_from is not None and read.requested_from <= start:
        # The read asked for these days and the source had none: re-reading cannot help.
        return ComputeError(
            f"{read.platform}/{read.account}: the source has no data before {first}, so the "
            f"{label} window {span} is not available; say so, and compare only windows from "
            f"{first} on"
        )
    return ComputeError(
        f"{', '.join(read.artifact_ids)}: the {label} window starts {start.isoformat()} but the "
        f"read begins {first}; re-read the union of both windows"
    )


def require_days(read: AccountRead, start: date, end: date) -> None:
    """Merged reads must cover every day in [start, end]: a missing call is not a zero day."""
    if len(read.artifact_ids) < 2:
        return
    have = {r.window.start for r in read.rows}
    missing = [
        start + timedelta(days=i)
        for i in range((end - start).days + 1)
        if start + timedelta(days=i) not in have
    ]
    if missing:
        shown = ", ".join(d.isoformat() for d in missing[:5])
        more = f" and {len(missing) - 5} more" if len(missing) > 5 else ""
        raise ComputeError(
            f"{read.platform}/{read.account}: the reads have no rows for {shown}{more}; "
            "read those days too"
        )
