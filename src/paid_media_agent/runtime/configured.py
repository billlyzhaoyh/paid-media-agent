"""The configured profile: live catalog when credentials exist, fixtures otherwise.

The CLI runs this profile for `ask` and `report`, and the self-hosted runtime runs it behind its
API, so what you try on your machine is what a deployment runs.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any

from paid_media_agent.config import Settings
from paid_media_agent.runtime.catalog import LoadedCatalog, load_catalog
from paid_media_agent.runtime.profiles import (
    ProfileName,
    RuntimeProfile,
    approval_policy_from_settings,
    fixture_profile,
    resolve_write_policy,
)
from paid_media_agent.store import Store
from paid_media_agent.tools.direct import CompositeReadProvider


def run_coroutine(coro: Coroutine[Any, Any, Any]) -> Any:
    """Run a coroutine to completion from sync code, inside or outside an event loop.

    The CLI has no loop, so `asyncio.run` is right. The self-hosted runtime builds its profile
    from inside the serving loop, where `asyncio.run` raises; a short-lived worker thread with
    its own loop keeps the catalog load blocking and identical on both paths.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(asyncio.run, coro).result()


def configured_profile(
    settings: Settings,
    *,
    project_root: Path,
    name: ProfileName = "local",
    store: Store | None = None,
) -> tuple[RuntimeProfile, LoadedCatalog]:
    """The profile every entry point runs: live providers only when their credentials exist."""
    loaded: LoadedCatalog = run_coroutine(load_catalog(settings, project_root=project_root))
    write_policy, issues = resolve_write_policy(settings, project_root, loaded.provider)
    profile = fixture_profile(
        settings,
        project_root=project_root,
        catalog_provider=loaded.provider,
        name=name,
        approval_policy=approval_policy_from_settings(settings),
        store=store,
    )
    overrides: dict[str, Any] = {"write_policy": write_policy, "write_policy_issues": issues}
    if loaded.read_provider is not None:
        read_provider = loaded.read_provider
        if not loaded.live and isinstance(read_provider, CompositeReadProvider):
            # Sample reads must see the profile's fixture state, which the fake writes change;
            # otherwise every fake write reads back the old value and is reported failed.
            read_provider = read_provider.with_default(profile.read_provider)
        overrides["read_provider"] = read_provider
    if loaded.live:
        # Live catalog: live reads and the gated live write adapter. The fake is never used here.
        overrides.update(
            read_provider=loaded.read_provider,
            write_provider=loaded.write_provider,
            write_provider_is_fake=False,
        )
    return replace(profile, **overrides), loaded
