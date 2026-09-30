"""Model-facing `read_artifact` tool: page through a result that was too large to return whole.

An offloaded tool result, a platform read's rows, or an analysis artifact can be read in pages of
a few thousand characters. It reads only this workspace's artifacts by id, never files by path,
and its own result is never offloaded again.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.redaction import redact, sanitize_exception
from paid_media_agent.tools.artifacts import ArtifactError, ArtifactStore

READ_ARTIFACT_TOOL = "read_artifact"
PAGE_CHARS = 5000


class ReadArtifactArgs(BaseModel):
    artifact_id: str = Field(description="An artifact id from an earlier result (art_...).")
    offset: int = Field(default=0, ge=0, description="Character to start from; see next_offset.")


def build_read_artifact_tool(artifacts: ArtifactStore, secrets: tuple[str, ...] = ()) -> ToolSpec:
    """`secrets` are redacted from the whole text before it is paged, so no page boundary can
    split a secret into two halves that each escape redaction."""

    def _run(kwargs: dict[str, Any], _context: ToolContext) -> str:
        try:
            args = ReadArtifactArgs.model_validate(kwargs)
            record = artifacts.read(args.artifact_id)
        except (ValidationError, ArtifactError, OSError, ValueError) as exc:
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
            "quoting anything an offloaded preview does not show. Read-only."
        ),
        parameters=parameters_for(ReadArtifactArgs),
        handler=_run,
        offload=False,
    )
