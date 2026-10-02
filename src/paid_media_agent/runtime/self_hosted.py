"""Self-hosted runtime: the configured profile and the agent loop over a DuckDB state file.

Proposals, approval claims, receipts, dedupe keys, thread ownership, conversations, and tool
calls paused for approval all live in the DuckDB file at `PAID_MEDIA_STATE_PATH`. One process
owns that file, so the API and the Slack adapter run in the same `serve` process.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from paid_media_agent.assembly import AgentComponents, build_agent, build_agent_components
from paid_media_agent.config import Settings
from paid_media_agent.harness.loop import Agent
from paid_media_agent.harness.models import ChatModel
from paid_media_agent.persistence.interfaces import DedupeStore, ThreadOwnershipStore
from paid_media_agent.runtime.configured import configured_profile
from paid_media_agent.runtime.profiles import RuntimeProfile
from paid_media_agent.store import Store
from paid_media_agent.tools.catalog import AuthorizedToolCatalog


@dataclass(frozen=True)
class SelfHostedRuntime:
    settings: Settings
    profile: RuntimeProfile
    catalog: AuthorizedToolCatalog
    components: AgentComponents
    agent: Agent
    store: Store

    @property
    def dedupe(self) -> DedupeStore:
        return self.profile.dedupe

    @property
    def threads(self) -> ThreadOwnershipStore:
        return self.profile.threads

    @property
    def persistence(self) -> str:
        return "memory" if self.store.in_memory else "duckdb"


def state_path(settings: Settings, project_root: Path) -> Path:
    path = settings.paid_media_state_path
    return path if path.is_absolute() else project_root / path


def build_self_hosted_runtime(
    settings: Settings,
    *,
    project_root: Path,
    model: ChatModel | None = None,
    store: Store | None = None,
) -> SelfHostedRuntime:
    store = store or Store(state_path(settings, project_root))
    profile, loaded = configured_profile(
        settings, project_root=project_root, name="self_hosted", store=store
    )
    components = build_agent_components(
        settings=settings, runtime=profile, catalog=loaded.catalog, model=model
    )
    return SelfHostedRuntime(
        settings=settings,
        profile=profile,
        catalog=loaded.catalog,
        components=components,
        agent=build_agent(components, store),
        store=store,
    )
