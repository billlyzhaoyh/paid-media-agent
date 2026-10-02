"""Opt-in: the real agent loop against live models through OpenRouter, on synthetic data only.

Set PAID_MEDIA_LIVE_TESTS=1 and OPENROUTER_API_KEY. Each model costs a few cents at most. The
fake write provider is used throughout; no live account is read or changed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from paid_media_agent.config import ModelConfig, Settings
from paid_media_agent.harness.messages import AssistantMessage, ToolMessage
from paid_media_agent.harness.models import resolve_model
from paid_media_agent.runtime.local import build_local_runtime
from paid_media_agent.tools.fixtures import FakeWriteProvider, FixtureState

pytestmark = pytest.mark.skipif(
    os.environ.get("PAID_MEDIA_LIVE_TESTS") != "1" or not os.environ.get("OPENROUTER_API_KEY"),
    reason="set PAID_MEDIA_LIVE_TESTS=1 and OPENROUTER_API_KEY to call live models",
)
MODELS = ("anthropic/claude-haiku-4.5", "openai/gpt-5.4-mini", "google/gemini-3.8-flash")


@pytest.mark.parametrize("model_id", MODELS)
async def test_live_model_reads_compares_and_pauses_for_approval(
    settings: Settings, project_root: Path, model_id: str
) -> None:
    configured = settings.model_copy(
        update={
            "paid_media_approver_ids": "reviewer-1",
            "paid_media_model_zero_data_retention": True,
        }
    )
    model = resolve_model(
        ModelConfig.parse(f"openrouter:{model_id}"),
        api_key_env=None,
        timeout_seconds=120,
        zero_data_retention=True,
    )
    runtime = build_local_runtime(
        configured, project_root=project_root, model=model, fixture_state=FixtureState()
    )
    provider = runtime.profile.write_provider
    assert isinstance(provider, FakeWriteProvider)
    thread = f"live-{model_id.replace('/', '-')}"

    answered = await runtime.agent.send(
        thread,
        "local-user",
        "Using the tools, compare demo-google campaign performance for 2026-08-22 to 2026-08-28 "
        "against 2026-08-15 to 2026-08-21, then answer in two sentences.",
    )
    tools_used = [m.name for m in answered.messages if isinstance(m, ToolMessage)]
    assert "discover_tools" in tools_used or any("__" in name for name in tools_used)
    assert "compare_periods" in tools_used, tools_used
    assert isinstance(answered.messages[-1], AssistantMessage) and answered.messages[-1].content

    paused = await runtime.agent.send(
        thread,
        "local-user",
        "Propose lowering the Performance Max (g-103) daily budget to 240 and request execution.",
    )
    assert paused.awaiting_approval, [m for m in paused.messages[-4:]]
    assert [c.name for c in paused.pending] == ["execute_change"]
    assert provider.mutation_calls == [], "nothing runs before a human decides"
    results = [
        json.loads(m.content)
        for m in paused.messages
        if isinstance(m, ToolMessage) and m.name == "propose_change"
    ]
    assert results and "proposal" in results[-1]
