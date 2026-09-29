"""Local runtimes: the shared components and the agent loop over a profile's state store.

The demo and the test suite use the fixture profile with an injected model. `ask` and `report` use
the configured profile, the same accounts and policy a server runs, with in-memory state.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from paid_media_agent.assembly import AgentComponents, build_agent, build_agent_components
from paid_media_agent.config import Settings
from paid_media_agent.harness.loop import Agent
from paid_media_agent.harness.models import ChatModel
from paid_media_agent.runtime.configured import configured_profile
from paid_media_agent.runtime.profiles import RuntimeProfile, fixture_profile
from paid_media_agent.tools.catalog import AuthorizedToolCatalog, StaticCatalogProvider
from paid_media_agent.tools.fixtures import FixtureState, build_fixture_catalog


@dataclass(frozen=True)
class LocalRuntime:
    settings: Settings
    profile: RuntimeProfile
    catalog: AuthorizedToolCatalog
    components: AgentComponents
    agent: Agent


def build_local_runtime(
    settings: Settings,
    *,
    project_root: Path,
    model: ChatModel,
    catalog: AuthorizedToolCatalog | None = None,
    catalog_provider: StaticCatalogProvider | None = None,
    profile: RuntimeProfile | None = None,
    fixture_state: FixtureState | None = None,
    workspace_root: Path | None = None,
    clock: Callable[[], datetime] | None = None,
) -> LocalRuntime:
    """Fixture catalog and fake writes with the model you pass: the demo and the test suite."""
    resolved_catalog = catalog or build_fixture_catalog()
    provider = catalog_provider or StaticCatalogProvider(resolved_catalog)
    resolved_profile = profile or fixture_profile(
        settings,
        project_root=project_root,
        catalog_provider=provider,
        fixture_state=fixture_state,
        workspace_root=workspace_root,
    )
    components = build_agent_components(
        settings=settings, runtime=resolved_profile, catalog=resolved_catalog, model=model
    )
    return LocalRuntime(
        settings=settings,
        profile=resolved_profile,
        catalog=resolved_catalog,
        components=components,
        agent=build_agent(components, resolved_profile.store, clock=clock),
    )


def build_configured_runtime(
    settings: Settings, *, project_root: Path, model: ChatModel | None = None
) -> LocalRuntime:
    """The server's profile run locally: live catalog when credentials exist, in-memory state.

    `paid-media-agent ask` and `report` use this, so a local answer comes from the same accounts,
    policy, and tools a server has. The write gate still decides whether a live mutation can run.
    """
    profile, loaded = configured_profile(settings, project_root=project_root)
    components = build_agent_components(
        settings=settings, runtime=profile, catalog=loaded.catalog, model=model
    )
    return LocalRuntime(
        settings=settings,
        profile=profile,
        catalog=loaded.catalog,
        components=components,
        agent=build_agent(components, profile.store),
    )
