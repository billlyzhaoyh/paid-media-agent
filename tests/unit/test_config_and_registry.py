from __future__ import annotations

from pathlib import Path

import pytest

from paid_media_agent.config import AccountRegistry, ModelConfig, Settings


def test_model_config_requires_provider_prefix() -> None:
    with pytest.raises(ValueError):
        ModelConfig.parse("claude-sonnet-4-6")
    config = ModelConfig.parse("Anthropic:claude-sonnet-4-6", base_url="https://proxy.example/v1")
    assert config.provider == "anthropic"
    assert config.spec == "anthropic:claude-sonnet-4-6"
    assert str(config.base_url).startswith("https://proxy.example")
    with pytest.raises(ValueError, match="provider:model"):
        ModelConfig.parse("anthropic/claude-sonnet-4-6")


def test_settings_secret_helpers_never_expose_values(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        paid_media_api_tokens="tok-abc:alice,tok-def:bob,malformed",
        paid_media_approver_ids="alice, bob",
        pipeboard_api_token="",
        paid_media_approval_signing_key="signing-secret-value",
    )
    assert settings.api_token_map() == {"tok-abc": "alice", "tok-def": "bob"}
    assert settings.approver_refs() == frozenset({"alice", "bob"})
    assert settings.pipeboard_api_token is None
    assert "tok-abc" not in repr(settings) and "signing-secret-value" not in repr(settings)


def test_account_registry_from_toml(project_root: Path) -> None:
    registry = AccountRegistry.from_toml(project_root / "config" / "accounts.example.toml")
    assert registry.aliases() == ("demo-google", "demo-meta", "demo-reddit")
    binding = registry.resolve("demo-google")
    assert binding is not None and binding.platform.value == "google_ads"
    assert registry.resolve("unknown") is None
    assert "fixture-google-0001" in registry.provider_ids()
