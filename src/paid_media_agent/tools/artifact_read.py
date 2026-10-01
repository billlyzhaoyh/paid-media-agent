"""Model-facing `read_artifact` tool: page through a result that was too large to return whole.

An offloaded tool result, a platform read's rows, or an analysis artifact can be read in pages of
a few thousand characters. A platform read's rows can instead be filtered and totalled here, with
the same arithmetic as `summarize_window`, so the model never adds rows up itself. It reads only
this workspace's artifacts by id, never files by path, and its own result is never offloaded
again.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.domain.metrics import PerformanceRow
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import redact, sanitize_exception
from paid_media_agent.tools.artifacts import ArtifactError, ArtifactRecord, ArtifactStore
from paid_media_agent.tools.compute import aggregate
from paid_media_agent.tools.normalize import NormalizationError, rows_from_payload

READ_ARTIFACT_TOOL = "read_artifact"
PAGE_CHARS = 5000


MAX_ROWS = 200
CENT = Decimal("0.01")
ROW_FIELDS = (
    "day", "entity_ref", "entity_name", "currency", "spend", "impressions", "clicks",
    "conversions", "conversion_value",
)  # fmt: skip
RowGroup = Literal["entity", "day", "total"]


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(description="An artifact id from an earlier result (art_...).")
    offset: int = Field(default=0, ge=0, description="Character to start from; see next_offset.")
    entity_ref: str | None = Field(
        default=None, description="Rows of a platform read only: this campaign or entity."
    )
    start_date: date | None = Field(default=None, description="Rows only: from this day.")
    end_date: date | None = Field(default=None, description="Rows only: through this day.")
    group_by: RowGroup | None = Field(
        default=None,
        description=(
            "Rows only: totals per entity, per day, or one total, with CPA, ROAS, CTR, and CVR "
            "from the sums. Use it instead of adding up rows."
        ),
    )
    fields: list[str] | None = Field(
        default=None, description="Rows only: keep these columns, e.g. ['day', 'spend']."
    )

    @property
    def wants_rows(self) -> bool:
        return any(
            v is not None
            for v in (self.entity_ref, self.start_date, self.end_date, self.group_by, self.fields)
        )


def _row(row: PerformanceRow) -> dict[str, Any]:
    return {
        "day": row.window.start.isoformat(),
        "entity_ref": row.entity_ref,
        "entity_name": row.entity_name,
        "currency": row.currency,
        "spend": str(row.spend),
        "impressions": row.impressions,
        "clicks": row.clicks,
        "conversions": None if row.conversions is None else str(row.conversions),
        "conversion_value": None if row.conversion_value is None else str(row.conversion_value),
    }


def _totals(key: dict[str, Any], rows: list[PerformanceRow]) -> dict[str, Any]:
    """`compute.aggregate`, as `summarize_window` states it: money to the cent."""
    metrics = aggregate(rows)
    body = metrics.model_dump(mode="json", exclude={"cpc", "cpm"})
    for name in ("spend", "conversion_value"):
        value = getattr(metrics, name)
        body[name] = None if value is None else str(value.quantize(CENT, rounding=ROUND_HALF_UP))
    return {**key, "currency": rows[0].currency, **body}


def read_rows(record: ArtifactRecord, args: ReadArtifactArgs) -> dict[str, Any]:
    """A platform read's rows, filtered and optionally totalled."""
    if record.metadata.kind != "performance_rows":
        raise ValueError(
            f"filters and group_by work on performance_rows artifacts, not {record.metadata.kind}"
        )
    rows = [
        r
        for r in rows_from_payload(record.payload)
        if (args.entity_ref is None or r.entity_ref == args.entity_ref)
        and (args.start_date is None or r.window.start >= args.start_date)
        and (args.end_date is None or r.window.start <= args.end_date)
    ]
    out: list[dict[str, Any]]
    if args.group_by is None:
        out = [_row(r) for r in sorted(rows, key=lambda r: (r.window.start, r.entity_ref))]
    else:
        groups: dict[tuple[str, ...], list[PerformanceRow]] = {}
        for r in rows:
            if args.group_by == "entity":
                key: tuple[str, ...] = (r.entity_ref, r.entity_name)
            elif args.group_by == "day":
                key = (r.window.start.isoformat(),)
            else:
                key = ()
            groups.setdefault(key, []).append(r)
        names = {"entity": ("entity_ref", "entity_name"), "day": ("day",), "total": ()}[
            args.group_by
        ]
        out = [
            _totals(dict(zip(names, key, strict=True)), members)
            for key, members in sorted(groups.items())
        ]
    if args.fields and out:
        unknown = [f for f in args.fields if f not in out[0]]
        if unknown:
            raise ValueError(f"unknown fields {', '.join(unknown)}; rows have {', '.join(out[0])}")
        out = [{f: row[f] for f in args.fields} for row in out]
    result: dict[str, Any] = {
        "artifact_id": record.metadata.artifact_id,
        "kind": record.metadata.kind,
        "row_count": len(out),
        "truncated": len(out) > MAX_ROWS,
    }
    if args.group_by:
        result["grouped_by"] = args.group_by
    result["rows"] = out[:MAX_ROWS]
    return result


def build_read_artifact_tool(artifacts: ArtifactStore, secrets: tuple[str, ...] = ()) -> ToolSpec:
    """`secrets` are redacted from the whole text before it is paged, so no page boundary can
    split a secret into two halves that each escape redaction."""

    def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = ReadArtifactArgs.model_validate(kwargs)
            record = artifacts.read(args.artifact_id)
            if args.wants_rows:
                return redact(json.dumps(read_rows(record, args), default=str), secrets)
        except (ValidationError, ArtifactError, NormalizationError, OSError, ValueError) as exc:
            return json.dumps({"error": True, "detail": sanitize_exception(exc, secrets)})
        payload = record.payload
        # An offloaded tool result stores the original text; other artifacts are JSON payloads.
        content = payload.get("content")
        raw = content if isinstance(content, str) else json.dumps(payload, default=str)
        text = redact(raw, secrets)
        end = min(len(text), args.offset + PAGE_CHARS)
        return json.dumps(
            {
                "artifact_id": args.artifact_id,
                "kind": record.metadata.kind,
                "total_chars": len(text),
                "offset": args.offset,
                "next_offset": end if end < len(text) else None,
                "text": text[args.offset : end],
            }
        )

    return ToolSpec(
        name=READ_ARTIFACT_TOOL,
        description=(
            "Read a stored result by its artifact id, a page at a time: an offloaded tool result "
            "(its preview is partial), a platform read's rows, or an analysis. Use it before "
            "quoting anything an offloaded preview does not show. For a platform read's rows, "
            "filter by entity_ref and dates and set group_by for totals instead of adding rows "
            "up. Read-only."
        ),
        parameters=parameters_for(ReadArtifactArgs),
        handler=_run,
        offload=False,
    )
