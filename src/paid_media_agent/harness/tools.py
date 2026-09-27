"""Tools the model can call, and the single path every call takes.

Dispatch decides, in order: the tool is registered; it is not a provider mutation (those run only
through propose_change and execute_change); its arguments validate against the schema the model
saw. Then it runs, a transient provider timeout on a read is retried once, oversized results are
offloaded to an artifact, and secrets are redacted. A failure becomes an error result for the
model, never an exception out of the loop.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, cast

import jsonschema
from pydantic import BaseModel

from paid_media_agent.harness.messages import ToolCall, ToolMessage
from paid_media_agent.harness.models import ToolSchema
from paid_media_agent.redaction import redact, sanitize_exception
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.catalog import CatalogProvider, ToolClass
from paid_media_agent.tools.providers import ProviderTimeout

logger = logging.getLogger(__name__)

ToolKind = Literal["core", "read", "write", "file"]
OFFLOAD_SCHEMA_VERSION = "tool-result/1"
PREVIEW_CHARS = 400


@dataclass
class ToolContext:
    """What a tool may know about the run that called it."""

    thread_id: str
    caller_ref: str
    activate: Callable[[Sequence[str]], None] = lambda _names: None
    """Bind these read tools to later model calls in this thread (used by discover_tools)."""


SyncHandler = Callable[[dict[str, Any], ToolContext], str]
AsyncHandler = Callable[[dict[str, Any], ToolContext], Awaitable[str]]
Handler = SyncHandler | AsyncHandler


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    kind: ToolKind = "core"
    gated: bool = False
    """Pauses the run for a human decision before the handler runs (execute_change)."""
    offload: bool = True
    """False for file tools, which page with offset and limit instead."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(self.name, self.description, self.parameters)


def parameters_for(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("title", None)
    schema.setdefault("properties", {})
    return schema


def _error(call: ToolCall, body: dict[str, Any]) -> ToolMessage:
    return ToolMessage(call.id, call.name, json.dumps(body), status="error")


@dataclass
class ToolDispatcher:
    tools: dict[str, ToolSpec]
    catalog_provider: CatalogProvider
    artifacts: ArtifactStore
    secrets: tuple[str, ...] = ()
    offload_chars: int = 6000
    denials: list[tuple[str, str]] = field(default_factory=list)

    def _refuse(self, call: ToolCall) -> ToolMessage | None:
        if call.name not in self.tools:
            self.denials.append((call.name, "outside_tool_surface"))
            logger.warning("denied tool call outside surface: %s", call.name)
            return _error(
                call,
                {
                    "denied": True,
                    "reason": "tool is not part of the authorized surface; use discover_tools",
                    "tool": call.name,
                },
            )
        entry = self.catalog_provider.current().get(call.name)
        if entry is not None and entry.tool_class is not ToolClass.READ:
            self.denials.append((call.name, "mutation_or_denied_class"))
            return _error(
                call,
                {
                    "denied": True,
                    "reason": "provider mutations run only through propose_change and execute_change",
                    "tool": call.name,
                },
            )
        if call.invalid_arguments is not None:
            return _error(
                call, {"error": "invalid_arguments", "detail": "arguments must be a JSON object"}
            )
        try:
            jsonschema.validate(call.args, self.tools[call.name].parameters)
        except jsonschema.ValidationError as exc:
            path = "/".join(str(p) for p in exc.absolute_path) or "arguments"
            return _error(
                call, {"error": "invalid_arguments", "detail": f"{path}: {exc.message}"[:300]}
            )
        return None

    async def _run(self, spec: ToolSpec, args: dict[str, Any], context: ToolContext) -> str:
        if inspect.iscoroutinefunction(spec.handler):
            return await cast(AsyncHandler, spec.handler)(args, context)
        return await asyncio.to_thread(cast(SyncHandler, spec.handler), args, context)

    def _offload(self, spec: ToolSpec, content: str) -> str:
        if not spec.offload or len(content) <= self.offload_chars:
            return content
        metadata = self.artifacts.write_json(
            "tool_result",
            {"tool_name": spec.name, "content": content},
            schema_version=OFFLOAD_SCHEMA_VERSION,
            tool_name=spec.name,
        )
        return json.dumps(
            {
                "offloaded": True,
                "artifact_id": metadata.artifact_id,
                "byte_size": metadata.byte_size,
                "sha256": metadata.sha256,
                "preview": content[:PREVIEW_CHARS],
                "note": "Result exceeded the context budget. Use the artifact id with deterministic tools.",
            }
        )

    async def dispatch(self, call: ToolCall, context: ToolContext) -> ToolMessage:
        refusal = self._refuse(call)
        if refusal is not None:
            return refusal
        spec = self.tools[call.name]
        attempts = 2 if spec.kind == "read" else 1
        for attempt in range(attempts):
            try:
                content = await self._run(spec, call.args, context)
                break
            except ProviderTimeout:
                if attempt + 1 < attempts:
                    continue
                return ToolMessage(
                    call.id, call.name, "Tool failed: provider call timed out", status="error"
                )
            except Exception as exc:
                return ToolMessage(
                    call.id,
                    call.name,
                    f"Tool failed: {sanitize_exception(exc, self.secrets)}",
                    status="error",
                )
        content = await asyncio.to_thread(self._offload, spec, content)
        return ToolMessage(call.id, call.name, redact(content, self.secrets))
