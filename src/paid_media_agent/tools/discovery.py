"""Model-facing discovery tools: accounts and authorized catalog search."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field

from paid_media_agent.analytics.goals import GoalStore, account_today
from paid_media_agent.config import AccountRegistry
from paid_media_agent.domain.common import JsonValue, Platform
from paid_media_agent.harness.tools import ToolContext, ToolSpec, parameters_for
from paid_media_agent.tools.catalog import CatalogProvider

LIST_ACCOUNTS_TOOL = "list_accounts"
DISCOVER_TOOLS_TOOL = "discover_tools"


class _DiscoverArgs(BaseModel):
    query: str = Field(description="Keywords describing the data or capability you need.")
    platform: Platform | None = Field(default=None, description="Optional platform filter.")


class _NoArgs(BaseModel):
    """Argument schema for tools that take nothing. Shared with the write tools."""

    pass


def build_list_accounts_tool(accounts: AccountRegistry, goals: GoalStore | None = None) -> ToolSpec:
    def _goals(alias: str) -> dict[str, JsonValue] | None:
        goal = goals.current(alias, account_today(accounts, alias)) if goals else None
        if goal is None:
            return None
        return {**goal.values(), "effective_from": goal.effective_from.isoformat()}

    def _list(_args: dict[str, Any], _context: ToolContext) -> str:
        return json.dumps(
            {
                "accounts": [
                    {
                        "alias": b.alias,
                        "platform": b.platform.value,
                        "currency": b.currency,
                        "timezone": b.timezone,
                        "goals": _goals(b.alias),
                    }
                    for b in accounts.bindings
                ],
                "note": "goals are the configured targets (money in the account's currency); "
                "null means none are set, so label judgements directional.",
            }
        )

    return ToolSpec(
        name=LIST_ACCOUNTS_TOOL,
        description=(
            "List configured account aliases with platform, currency, timezone, and goals "
            "(target CPA, target ROAS, monthly budget). Use aliases in every read."
        ),
        parameters=parameters_for(_NoArgs),
        handler=_list,
    )


def build_discover_tools_tool(catalog_provider: CatalogProvider) -> ToolSpec:
    def _discover(args: dict[str, Any], context: ToolContext) -> str:
        parsed = _DiscoverArgs.model_validate(args)
        catalog = catalog_provider.current()
        entries = catalog.search(parsed.query, platform=parsed.platform)
        # The tools found here are bound to the model's later calls in this thread.
        context.activate([e.qualified_name for e in entries])
        return json.dumps(
            {
                "catalog_revision": catalog.revision,
                "tools": [
                    {
                        "name": e.qualified_name,
                        "platform": e.platform.value,
                        "description": e.description[:240],
                        "arguments": sorted(
                            k for k in e.input_schema.get("properties", {}) if k != e.account_arg
                        ),
                    }
                    for e in entries
                ],
                "note": "These tools are now available to call. Only authorized reads are listed; changes go through propose_change.",
            }
        )

    return ToolSpec(
        name=DISCOVER_TOOLS_TOOL,
        description="Search the authorized read-tool catalog by keywords and make the matching tools available. Call this before using a platform tool.",
        parameters=parameters_for(_DiscoverArgs),
        handler=_discover,
    )
