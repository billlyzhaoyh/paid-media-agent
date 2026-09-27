"""Host-side Pipeboard Streamable HTTP MCP loading, read execution, and the live write adapter."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from pydantic import SecretStr

from paid_media_agent.config import Settings
from paid_media_agent.domain.common import JsonValue, Platform
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.tools.catalog import (
    DEFAULT_LOCAL_POLICY,
    AuthorizedToolCatalog,
    CatalogEntry,
    LocalPolicy,
    RawTool,
    build_authorized_catalog,
    qualified_name,
)
from paid_media_agent.tools.providers import ProviderError, ProviderResult, ProviderTimeout

logger = logging.getLogger(__name__)

PIPEBOARD_SOURCE = "pipeboard"
CATALOG_LOAD_TIMEOUT_SECONDS = 20
_OPERATION_REF_KEYS = ("operation_ref", "operation_id", "resource_name", "id", "campaign_id")
_ANNOTATION_KEYS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint", "title")


@dataclass(frozen=True)
class McpTool:
    name: str
    description: str
    input_schema: dict[str, Any]
    annotations: dict[str, Any] | None


@dataclass(frozen=True)
class McpResult:
    is_error: bool
    structured: dict[str, Any] | None
    text: str


class McpClient(Protocol):
    async def list_tools(self, platform: Platform, url: str) -> list[McpTool]: ...

    async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> McpResult: ...


class StreamableHttpMcpClient:
    """One short MCP session per request. The bearer token lives only in the HTTP headers."""

    def __init__(self, token: SecretStr, *, timeout_seconds: float = 60.0) -> None:
        self._headers = {"Authorization": f"Bearer {token.get_secret_value()}"}
        self._timeout = timeout_seconds

    @asynccontextmanager
    async def _session(self, url: str) -> AsyncIterator[Any]:
        import httpx
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        async with (
            httpx.AsyncClient(headers=self._headers, timeout=self._timeout) as http,
            streamable_http_client(url, http_client=http) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            yield session

    async def list_tools(self, platform: Platform, url: str) -> list[McpTool]:  # noqa: ARG002
        tools: list[McpTool] = []
        async with self._session(url) as session:
            cursor: str | None = None
            while True:
                page = await session.list_tools(cursor=cursor)
                for tool in page.tools:
                    hints = (
                        tool.annotations.model_dump(exclude_none=True) if tool.annotations else {}
                    )
                    tools.append(
                        McpTool(
                            name=tool.name,
                            description=tool.description or "",
                            input_schema=dict(tool.inputSchema),
                            annotations={k: hints[k] for k in _ANNOTATION_KEYS if k in hints}
                            if "readOnlyHint" in hints
                            else None,
                        )
                    )
                cursor = page.nextCursor
                if not cursor:
                    return tools

    async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> McpResult:
        async with self._session(url) as session:
            result = await session.call_tool(name, arguments)
        texts = [getattr(item, "text", "") for item in result.content]
        return McpResult(
            is_error=bool(result.isError),
            structured=result.structuredContent
            if isinstance(result.structuredContent, dict)
            else None,
            text="\n".join(t for t in texts if t),
        )


@dataclass(frozen=True)
class ToolAddress:
    url: str
    name: str


class PipeboardCatalogLoader:
    """Loads every server's tool catalog concurrently; keeps schemas host-side per assembly."""

    def __init__(
        self,
        *,
        settings: Settings,
        policy: LocalPolicy = DEFAULT_LOCAL_POLICY,
        extra_raw_tools: list[RawTool] | None = None,
        client: McpClient | None = None,
    ) -> None:
        if settings.pipeboard_api_token is None:
            raise ValueError("PIPEBOARD_API_TOKEN is not configured")
        self._settings = settings
        self._policy = policy
        self._extra_raw_tools = list(extra_raw_tools or [])
        self.client: McpClient = client or StreamableHttpMcpClient(settings.pipeboard_api_token)
        self._catalog: AuthorizedToolCatalog | None = None
        self._addresses: dict[str, ToolAddress] = {}

    @property
    def policy(self) -> LocalPolicy:
        return self._policy

    def current(self) -> AuthorizedToolCatalog:
        """The catalog from `refresh()`. Loaded once per process; restart to pick up changes."""
        if self._catalog is None:
            raise RuntimeError("catalog not loaded; call refresh() first")
        return self._catalog

    async def refresh(self) -> AuthorizedToolCatalog:
        endpoints = self._settings.pipeboard_endpoints()

        async def load_endpoint(platform: Platform, url: str) -> list[McpTool]:
            try:
                return await asyncio.wait_for(
                    self.client.list_tools(platform, url), timeout=CATALOG_LOAD_TIMEOUT_SECONDS
                )
            except Exception as exc:
                logger.warning(
                    "catalog load failed for %s: %s", platform.value, sanitize_exception(exc)
                )
                return []

        loaded = await asyncio.gather(*(load_endpoint(p, u) for p, u in endpoints.items()))
        raw_tools: list[RawTool] = []
        addresses: dict[str, ToolAddress] = {}
        for (platform, url), tools in zip(endpoints.items(), loaded, strict=True):
            for tool in tools:
                raw_tools.append(
                    RawTool(
                        platform=platform.value,
                        name=tool.name,
                        description=tool.description,
                        input_schema=tool.input_schema,
                        annotations=tool.annotations,
                        source_endpoint=url,
                    )
                )
                # Catalog lookup keeps the first entry and denies duplicate qualified names.
                addresses.setdefault(
                    qualified_name(platform, tool.name), ToolAddress(url, tool.name)
                )
        catalog = build_authorized_catalog(
            [*raw_tools, *self._extra_raw_tools], policy=self._policy, source=PIPEBOARD_SOURCE
        )
        self._catalog = catalog
        self._addresses = addresses
        return catalog

    def address(self, qualified: str) -> ToolAddress | None:
        return self._addresses.get(qualified)


def _payload(result: McpResult) -> dict[str, JsonValue]:
    """Structured content when the server sends it; otherwise text, parsed when it is JSON."""
    if result.structured is not None:
        return result.structured
    try:
        parsed = json.loads(result.text)
    except json.JSONDecodeError:
        return {"text": result.text}
    return parsed if isinstance(parsed, dict) else {"items": parsed}


async def invoke_mcp_tool(
    client: McpClient, address: ToolAddress, arguments: Mapping[str, JsonValue], *, timeout: float
) -> dict[str, JsonValue]:
    """Call one MCP tool with a timeout; server-reported errors become sanitized provider errors."""
    try:
        result = await asyncio.wait_for(
            client.call_tool(address.url, address.name, dict(arguments)), timeout=timeout
        )
    except TimeoutError as exc:
        raise ProviderTimeout("provider call timed out") from exc
    except Exception as exc:
        raise ProviderError(sanitize_exception(exc)) from None
    if result.is_error:
        raise ProviderError(sanitize_exception(RuntimeError(result.text[:300])))
    return _payload(result)


class PipeboardReadProvider:
    """Executes authorized reads through the loaded MCP tools. Never called for mutations."""

    def __init__(self, loader: PipeboardCatalogLoader, *, timeout_seconds: float = 60.0) -> None:
        self._loader = loader
        self._timeout = timeout_seconds

    async def call_read(
        self, entry: CatalogEntry, arguments: dict[str, JsonValue]
    ) -> ProviderResult:
        address = self._loader.address(entry.qualified_name)
        if address is None:
            raise ProviderError("tool is not loaded in the current catalog")
        payload = await invoke_mcp_tool(
            self._loader.client, address, arguments, timeout=self._timeout
        )
        return ProviderResult(payload=payload)


class PipeboardWriteProvider:
    """Exact live mutation adapter. Reachable only through `WriteExecutor` behind `WriteGate`."""

    def __init__(self, loader: PipeboardCatalogLoader, *, timeout_seconds: float = 60.0) -> None:
        self._loader = loader
        self._timeout = timeout_seconds

    async def call_mutation(
        self, entry: CatalogEntry, arguments: dict[str, JsonValue]
    ) -> dict[str, JsonValue]:
        address = self._loader.address(entry.qualified_name)
        if address is None:
            raise ProviderError("mutation tool is not loaded in the current catalog")
        if entry.read_only_hint is not False:
            raise ProviderError("refusing to mutate through a tool without readOnlyHint=false")
        payload = await invoke_mcp_tool(
            self._loader.client, address, arguments, timeout=self._timeout
        )
        for key in _OPERATION_REF_KEYS:
            value = payload.get(key)
            if value is not None and "operation_ref" not in payload:
                payload = {**payload, "operation_ref": str(value)}
                break
        return payload
