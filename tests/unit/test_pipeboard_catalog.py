"""Full Pipeboard discovery stays concurrent, searchable, and account-scoped."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from paid_media_agent.config import AccountBinding, AccountRegistry, Settings
from paid_media_agent.domain.common import PIPEBOARD_PLATFORMS, Platform
from paid_media_agent.harness.tools import ToolContext
from paid_media_agent.tools.artifacts import ArtifactStore
from paid_media_agent.tools.catalog import DEFAULT_LOCAL_POLICY
from paid_media_agent.tools.discovery import build_discover_tools_tool
from paid_media_agent.tools.pipeboard import (
    McpResult,
    McpTool,
    PipeboardCatalogLoader,
    PipeboardReadProvider,
)
from paid_media_agent.tools.reads import ReadDenied, ReadDispatcher, model_facing_schema


async def test_all_catalogs_load_concurrently_and_new_platforms_are_searchable(
    settings: Settings, tmp_path: Path
) -> None:
    started: set[Platform] = set()
    ready = asyncio.Event()
    calls: list[tuple[str, str, dict[str, Any]]] = []
    configured = settings.model_copy(update={"pipeboard_api_token": SecretStr("test-token")})
    endpoints = configured.pipeboard_endpoints()
    assert set(endpoints) == set(PIPEBOARD_PLATFORMS)

    class Client:
        async def list_tools(self, platform: Platform, url: str) -> list[McpTool]:
            assert endpoints[platform] == url
            started.add(platform)
            if len(started) == len(PIPEBOARD_PLATFORMS):
                ready.set()
            # A sequential loader cannot pass this barrier.
            await asyncio.wait_for(ready.wait(), timeout=1)
            policy = DEFAULT_LOCAL_POLICY.platform_policy(platform)
            assert policy is not None
            account_arg = policy.account_arg_names[0]
            return [
                McpTool(
                    name="get_current_report",
                    description="Campaign and analytics reporting from the live catalog.",
                    input_schema={
                        "type": "object",
                        "properties": {account_arg: {"type": "string"}},
                        "required": [account_arg],
                    },
                    annotations={"readOnlyHint": True},
                ),
                # No readOnlyHint: never admitted as a read, whatever the name says.
                McpTool(
                    name="get_unannotated_report",
                    description="Reporting without annotations.",
                    input_schema={
                        "type": "object",
                        "properties": {account_arg: {"type": "string"}},
                    },
                    annotations=None,
                ),
            ]

        async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> McpResult:
            calls.append((url, name, arguments))
            return McpResult(
                is_error=False,
                structured=None,
                text=json.dumps({"rows": [{"date": "2026-08-28", "sessions": 12}]}),
            )

    loader = PipeboardCatalogLoader(settings=configured, client=Client())
    catalog = await loader.refresh()
    assert len(catalog.read_entries()) == len(PIPEBOARD_PLATFORMS) == 8
    assert not any("unannotated" in e.qualified_name for e in catalog.read_entries())
    assert loader.current() is catalog
    search = build_discover_tools_tool(loader)
    for platform in PIPEBOARD_PLATFORMS:
        activated: list[str] = []
        context = ToolContext("t-1", "local-user", activate=activated.extend)
        result = json.loads(
            str(search.handler({"query": "report", "platform": platform.value}, context))
        )
        assert result["tools"][0]["name"] == f"{platform.value}__get_current_report"
        assert activated == [f"{platform.value}__get_current_report"]
        entry = catalog.get(result["tools"][0]["name"])
        assert entry is not None
        address = loader.address(entry.qualified_name)
        assert address is not None
        assert (address.url, address.name) == (endpoints[platform], "get_current_report")
        schema = model_facing_schema(entry, ["configured-account"])
        assert "account_alias" in schema["properties"]
        assert entry.account_arg not in schema["properties"]

    dispatcher = ReadDispatcher(
        catalog_provider=loader,
        accounts=AccountRegistry(
            bindings=(
                AccountBinding(
                    alias="site",
                    platform=Platform.GOOGLE_ANALYTICS,
                    provider_account_id="123",
                    currency="USD",
                    timezone="UTC",
                ),
            )
        ),
        provider=PipeboardReadProvider(loader),
        artifacts=ArtifactStore(tmp_path / "artifacts"),
    )
    result = await dispatcher.execute(
        "google_analytics__get_current_report", {"account_alias": "site"}
    )
    assert calls == [
        (endpoints[Platform.GOOGLE_ANALYTICS], "get_current_report", {"property_id": "123"})
    ]
    assert result.artifact_kind == "provider_result", "GA4 sessions are not ad-spend rows"
    with pytest.raises(ReadDenied, match="raw_account_id_rejected"):
        await dispatcher.execute(
            "google_analytics__get_current_report",
            {"account_alias": "site", "property_id": "other"},
        )
    assert len(calls) == 1


async def test_unavailable_connector_does_not_block_other_catalogs(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Client:
        async def list_tools(self, platform: Platform, url: str) -> list[McpTool]:
            if platform is Platform.PINTEREST_ADS:
                await asyncio.Event().wait()
            policy = DEFAULT_LOCAL_POLICY.platform_policy(platform)
            assert policy is not None
            return [
                McpTool(
                    name="get_report",
                    description="Report",
                    input_schema={
                        "type": "object",
                        "properties": {policy.account_arg_names[0]: {"type": "string"}},
                    },
                    annotations={"readOnlyHint": True},
                )
            ]

        async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> McpResult:
            raise AssertionError("catalog loading never calls a tool")

    monkeypatch.setattr("paid_media_agent.tools.pipeboard.CATALOG_LOAD_TIMEOUT_SECONDS", 0.02)
    loader = PipeboardCatalogLoader(
        settings=settings.model_copy(update={"pipeboard_api_token": SecretStr("test-token")}),
        client=Client(),
    )
    catalog = await asyncio.wait_for(loader.refresh(), timeout=1)
    assert {e.platform for e in catalog.read_entries()} == set(PIPEBOARD_PLATFORMS) - {
        Platform.PINTEREST_ADS
    }
