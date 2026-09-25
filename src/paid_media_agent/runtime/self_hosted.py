"""Self-hosted runtime: the configured profile over a DuckDB state file and a compiled graph.

Proposals, approval claims, receipts, dedupe keys, and thread ownership live in the DuckDB file
at `PAID_MEDIA_STATE_PATH`. One process owns that file, so the API and the Slack adapter run in
the same `serve` process. Conversation checkpoints are still process memory; a pending approval
does not survive a restart and the runner reports it as expired.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.language_models import BaseChatModel
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph.state import CompiledStateGraph

from paid_media_agent.assembly import AgentComponents, build_agent_components
from paid_media_agent.config import Settings
from paid_media_agent.persistence.interfaces import DedupeStore, ThreadOwnershipStore
from paid_media_agent.runtime.configured import configured_profile
from paid_media_agent.runtime.local import compile_graph
from paid_media_agent.runtime.profiles import RuntimeProfile
from paid_media_agent.store import Store
from paid_media_agent.tools.catalog import AuthorizedToolCatalog


@dataclass(frozen=True)
class SelfHostedRuntime:
    settings: Settings
    profile: RuntimeProfile
    catalog: AuthorizedToolCatalog
    components: AgentComponents
    graph: CompiledStateGraph[Any, Any, Any, Any]
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
    model: BaseChatModel | None = None,
    checkpointer: BaseCheckpointSaver[Any] | None = None,
    store: Store | None = None,
) -> SelfHostedRuntime:
    store = store or Store(state_path(settings, project_root))
    profile, loaded = configured_profile(
        settings, project_root=project_root, name="self_hosted", store=store
    )
    components = build_agent_components(
        settings=settings, runtime=profile, catalog=loaded.catalog, model=model
    )
    graph = compile_graph(
        components, project_root=project_root, checkpointer=checkpointer or InMemorySaver()
    )
    return SelfHostedRuntime(
        settings=settings,
        profile=profile,
        catalog=loaded.catalog,
        components=components,
        graph=graph,
        store=store,
    )
