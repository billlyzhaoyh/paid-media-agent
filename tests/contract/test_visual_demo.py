"""The visual demo end to end: a report page for a simulated account, with no token or network."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paid_media_agent.config import Settings
from paid_media_agent.testing import demo_visual
from paid_media_agent.testing.demo_visual import (
    DEMO,
    RecordedTabPFN,
    account_check,
    demo_store,
    load_recorded,
    run_visual_demo,
)

HEADINGS = (
    "Expected range and anomalies",
    "Budget response and recommendation",
    "A change, approved and verified",
)


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    import httpx

    def refuse(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the demo must not use the network")

    monkeypatch.setattr(httpx.AsyncClient, "send", refuse)


async def test_the_demo_shows_tabpfn_from_its_recording_with_no_token_or_network(
    settings: Settings, project_root: Path, tmp_path: Path, no_network: None
) -> None:
    assert settings.tabpfn_token is None
    result = await run_visual_demo(
        settings, root=project_root, state_dir=tmp_path / "state", open_browser=False
    )
    html = Path(result["html"]).read_text("utf-8")
    assert Path(result["html"]).name == "demo_report.html"
    for heading in HEADINGS:
        assert heading in html
    assert result["ranges"] == {"label": "TabPFN, 95% expected range", "source": "recorded"}
    assert result["budgets"]["label"] == "TabPFN as the global model"
    assert result["budgets"]["source"] == "recorded" and result["tabpfn_tokens_billed"] == 0
    assert "recorded answers" in html and "no call was made" in html
    assert "Planted in the simulation" in html and "True curve (simulation)" in html
    assert "status=verified" in html, "the approved change and its readback are on the page"

    # A second run reuses the account and says the same thing.
    again = await run_visual_demo(
        settings, root=project_root, state_dir=tmp_path / "state", open_browser=False
    )
    assert again["ranges"] == result["ranges"]


async def test_without_a_recording_the_local_model_draws_and_says_so(
    settings: Settings,
    project_root: Path,
    tmp_path: Path,
    no_network: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(demo_visual, "recorded_path", lambda: tmp_path / "missing.json")
    result = await run_visual_demo(
        settings, root=project_root, state_dir=tmp_path / "state", open_browser=False
    )
    assert result["ranges"] == {"label": "Local model, 95% expected range", "source": "local"}
    assert result["budgets"]["label"] == "Pooled regression as the global model"
    html = Path(result["html"]).read_text("utf-8")
    assert "computed locally" in html and "TabPFN, 95% expected range" not in html


def test_a_recording_is_used_only_for_the_account_it_was_made_on(tmp_path: Path) -> None:
    import json

    store = demo_store(tmp_path / "state")
    try:
        assert isinstance(load_recorded(store), RecordedTabPFN)
        check = account_check(store)
        assert check["rows"] == DEMO.n_campaigns * DEMO.days
        other = tmp_path / "other.json"
        other.write_text(
            json.dumps({"account_check": {**check, "spend": check["spend"] + 1}, "calls": []})
        )
        assert load_recorded(store, other) is None, "a different account's answers do not apply"
        assert load_recorded(store, tmp_path / "missing.json") is None
    finally:
        store.close()
