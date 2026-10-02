from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from paid_media_agent.domain.common import RiskLevel
from paid_media_agent.tools.catalog import AuthorizedToolCatalog, StaticCatalogProvider
from paid_media_agent.tools.pipeboard import McpResult, ToolAddress, invoke_mcp_tool
from paid_media_agent.tools.providers import ProviderError
from paid_media_agent.tools.write_policy import (
    WriteOperation,
    WritePolicyFile,
    fixture_write_policy,
)
from paid_media_agent.tools.writes import WriteDenied, WriteGate, classify_risk


def test_policy_file_validates_against_catalog(
    project_root: Path, catalog: AuthorizedToolCatalog
) -> None:
    policy_file = WritePolicyFile.from_toml(project_root / "config" / "write-policy.example.toml")
    policy, issues = policy_file.validate_against(catalog)
    assert set(policy.admitted_names()) == set(fixture_write_policy().admitted_names())
    assert {(i.tool_name, i.reason) for i in issues} == {
        ("google_ads__update_campaign_bid", "not_admitted"),
        ("meta_ads__update_campaign", "not_admitted"),
    }
    budget = policy.get("google_ads__update_campaign_budget")
    assert budget is not None and budget.validate_only_arg == "validate_only"


def test_policy_rows_fail_closed_on_schema_and_catalog_mismatch(
    tmp_path: Path, catalog: AuthorizedToolCatalog
) -> None:
    toml = """
[operations."google_ads__update_campaign_budget"]
admitted = true
readback_tool = "google_ads__get_campaign"
target_arg = "campaign_id"
editable_fields = ["daily_budget", "lifetime_budget"]
readback_fields = { daily_budget = "daily_budget", lifetime_budget = "lifetime_budget" }

[operations."google_ads__mutate"]
admitted = true
readback_tool = "google_ads__get_campaign"
target_arg = "campaign_id"
editable_fields = ["operations"]
readback_fields = { operations = "operations" }

[operations."google_ads__list_campaigns"]
admitted = true
readback_tool = "google_ads__get_campaign"
target_arg = "status"
editable_fields = ["status"]
readback_fields = { status = "status" }

[operations."meta_ads__update_campaign_status"]
admitted = true
readback_tool = "meta_ads__update_campaign_budget"
target_arg = "campaign_id"
editable_fields = ["status"]
readback_fields = { status = "status" }
"""
    path = tmp_path / "policy.toml"
    path.write_text(toml)
    policy, issues = WritePolicyFile.from_toml(path).validate_against(catalog)
    assert policy.operations == ()
    reasons = {i.tool_name: i.reason for i in issues}
    assert reasons["google_ads__update_campaign_budget"].startswith(
        "schema_missing_fields:lifetime_budget"
    )
    assert reasons["google_ads__mutate"].startswith("catalog_class_denied")
    assert reasons["google_ads__list_campaigns"].startswith("catalog_class_read")
    assert reasons["meta_ads__update_campaign_status"] == "readback_tool_unavailable"


def test_unknown_policy_keys_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "policy.toml"
    path.write_text(
        '[operations."x__y"]\nadmitted = true\nreadback_tool = "r"\ntarget_arg = "t"\neditable_fields = ["f"]\nreadback_fields = { f = "f" }\nextra = 1\n'
    )
    with pytest.raises(ValueError):
        WritePolicyFile.from_toml(path)


def test_classify_risk_derives_reviewer_facts(catalog: AuthorizedToolCatalog) -> None:
    entry = catalog.get("google_ads__update_campaign_status")
    assert entry is not None
    op = fixture_write_policy().get("google_ads__update_campaign_status")
    assert op is not None
    flags = classify_risk(entry, op, {"status": "ENABLED"}, {"status": "PAUSED"})
    assert {"status_flip", "starts_delivery", "policy_high_risk"} <= set(flags)
    budget_entry = catalog.get("google_ads__update_campaign_budget")
    budget_op = fixture_write_policy().get("google_ads__update_campaign_budget")
    assert budget_entry is not None and budget_op is not None
    flags = classify_risk(budget_entry, budget_op, {"daily_budget": 500}, {"daily_budget": 300})
    assert {"budget_delta", "budget_increase"} <= set(flags) and "status_flip" not in flags
    flags = classify_risk(budget_entry, budget_op, {"daily_budget": 100}, {"daily_budget": 300})
    assert "budget_increase" not in flags and "budget_delta" in flags


def test_write_gate_matrix(tmp_path: Path) -> None:
    switch = tmp_path / "KILL_SWITCH"
    fake = WriteGate(writes_enabled=False, provider_is_fake=True, kill_switch_path=switch)
    fake.check("any")  # fakes pass without flags
    switch.write_text("incident")
    with pytest.raises(WriteDenied, match="kill_switch"):
        fake.check("any")
    switch.unlink()

    def live(**kwargs: object) -> WriteGate:
        base: dict[str, object] = {
            "writes_enabled": True,
            "provider_is_fake": False,
            "kill_switch_path": switch,
            "released_catalog_revision": "rev-1",
            "canary_tools": frozenset({"google_ads__update_campaign_budget"}),
            "current_revision": lambda: "rev-1",
        }
        base.update(kwargs)
        return WriteGate(**base)  # type: ignore[arg-type]

    live().check("google_ads__update_campaign_budget")
    with pytest.raises(WriteDenied, match="writes_disabled"):
        live(writes_enabled=False).check("google_ads__update_campaign_budget")
    with pytest.raises(WriteDenied, match="live_writes_not_released"):
        live(released_catalog_revision=None).check("google_ads__update_campaign_budget")
    with pytest.raises(WriteDenied, match="stale_catalog"):
        live(current_revision=lambda: "rev-2").check("google_ads__update_campaign_budget")
    with pytest.raises(WriteDenied, match="tool_not_released"):
        live().check("google_ads__update_campaign_status")
    assert "live provider" in live().describe() and "fake" in fake.describe()


async def test_invoke_mcp_tool_surfaces_errors_and_structured_content() -> None:
    class Client:
        def __init__(self, result: McpResult | Exception) -> None:
            self.result = result
            self.calls: list[tuple[str, str, dict[str, Any]]] = []

        async def list_tools(self, platform: object, url: str) -> list[object]:
            return []

        async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> McpResult:
            self.calls.append((url, name, arguments))
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    address = ToolAddress("https://mcp.example/google", "get_campaign")
    text = Client(McpResult(False, None, '{"campaign": {"id": "c1", "daily_budget": 5}}'))
    payload = await invoke_mcp_tool(text, address, {"campaign_id": "c1"}, timeout=5)  # type: ignore[arg-type]
    assert payload["campaign"]["daily_budget"] == 5  # type: ignore[index]
    assert text.calls == [(address.url, "get_campaign", {"campaign_id": "c1"})]

    structured = Client(McpResult(False, {"campaign": {"id": "c1"}}, "ignored text"))
    assert await invoke_mcp_tool(structured, address, {}, timeout=5) == {  # type: ignore[arg-type]
        "campaign": {"id": "c1"}
    }

    # Synthetic credential-shaped value built at runtime so the repository never carries one.
    secret = "sk-ant-" + "a" * 30
    failing = Client(RuntimeError("provider exploded with token " + secret))
    with pytest.raises(ProviderError) as info:
        await invoke_mcp_tool(failing, address, {}, timeout=5)  # type: ignore[arg-type]
    assert "sk-ant-" not in str(info.value)

    reported = Client(McpResult(True, None, "isError from provider " + secret))
    with pytest.raises(ProviderError) as info:
        await invoke_mcp_tool(reported, address, {}, timeout=5)  # type: ignore[arg-type]
    assert "sk-ant-" not in str(info.value)


def test_operation_digest_changes_with_policy(catalog: AuthorizedToolCatalog) -> None:
    op = fixture_write_policy().get("google_ads__update_campaign_budget")
    assert op is not None
    changed = WriteOperation(**{**op.model_dump(), "risk": RiskLevel.HIGH})
    assert op.digest() != changed.digest()
    assert StaticCatalogProvider(catalog).current().revision


@pytest.mark.parametrize(
    ("status", "name", "unknown"),
    [(401, "HTTPStatusError", False), (400, "HTTPStatusError", False),
     (502, "HTTPStatusError", True), (None, "ReadError", True), (None, "ConnectError", False)],
)  # fmt: skip
async def test_only_an_answer_that_never_came_leaves_a_mutation_unknown(
    status: int | None, name: str, unknown: bool
) -> None:
    from paid_media_agent.tools.providers import ProviderUnknownOutcome

    class Response:
        status_code = status

    error = type(name, (Exception,), {})("failed")
    if status is not None:
        error.response = Response()  # type: ignore[attr-defined]

    class Client:
        async def list_tools(self, platform: object, url: str) -> list[object]:
            return []

        async def call_tool(self, url: str, name: str, arguments: dict[str, Any]) -> McpResult:
            raise error

    address = ToolAddress("https://mcp.example/google", "update_campaign_budget")
    with pytest.raises(ProviderError) as info:
        await invoke_mcp_tool(Client(), address, {}, timeout=5)  # type: ignore[arg-type]
    assert isinstance(info.value, ProviderUnknownOutcome) is unknown, (status, name)
