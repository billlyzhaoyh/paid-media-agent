"""Host actions shared by the CLI and the local console. Every action returns a typed result."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import re
import secrets
import shutil
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from paid_media_agent.admin.accounts_file import (
    WRITABLE_ACCOUNTS_FILE,
    AccountsFileError,
    active_accounts_path,
    add_account,
    read_accounts,
    remove_account,
    writable_accounts_path,
)
from paid_media_agent.admin.envfile import (
    EnvFileError,
    apply_env_file,
    masked_env,
    read_env,
    write_env,
)
from paid_media_agent.admin.model_catalog import ModelCatalogError, list_models
from paid_media_agent.admin.model_presets import (
    _module_available,
    model_key_env,
    model_preset_payloads,
)
from paid_media_agent.config import AccountBinding, ModelConfig, Settings
from paid_media_agent.doctor import Check, run_doctor
from paid_media_agent.domain.common import (
    FIXTURE_PLATFORMS,
    PIPEBOARD_PLATFORMS,
    JsonValue,
    Platform,
)
from paid_media_agent.middleware.redaction import sanitize_exception
from paid_media_agent.middleware.tool_selection import capabilities_for, plan_selection
from paid_media_agent.runtime.profiles import load_write_policy_file
from paid_media_agent.runtime.self_hosted import state_path
from paid_media_agent.store import Store, StoreBusy
from paid_media_agent.surfaces.runner import _content_text
from paid_media_agent.tools.catalog import AuthorizedToolCatalog, CatalogEntry, ToolClass
from paid_media_agent.tools.fixtures import build_fixture_catalog, load_fixture_dataset

Status = Literal["ok", "warn", "fail", "skipped"]


class ActionResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    action: str
    ok: bool
    status: Status
    summary: str
    detail: dict[str, JsonValue] = Field(default_factory=dict)
    command: str = ""
    """Equivalent CLI invocation, so agents and humans share one path."""


def _result(
    action: str,
    status: Status,
    summary: str,
    detail: dict[str, JsonValue] | None = None,
    command: str = "",
) -> ActionResult:
    return ActionResult(
        action=action,
        ok=status in ("ok", "skipped"),
        status=status,
        summary=summary,
        detail=detail or {},
        command=command,
    )


def _checks_json(checks: list[Check]) -> list[dict[str, JsonValue]]:
    return [{"name": c.name, "status": c.status, "detail": c.detail} for c in checks]


def load_settings(root: Path) -> Settings:
    """Settings from the project `.env`, exported into the process so SDKs and children see them."""
    apply_env_file(root)
    return Settings(_env_file=str(root / ".env"))


def model_or_invalid(settings: Settings) -> tuple[ModelConfig, str]:
    """The parsed model, or a placeholder plus the reason, so a typo in `.env` is shown, not a crash."""
    try:
        return settings.model_settings(), ""
    except ValueError as exc:
        return ModelConfig(provider="", model=settings.paid_media_model), str(exc)


# ---------------------------------------------------------------- status and configuration


def catalog_summary(catalog: AuthorizedToolCatalog) -> dict[str, JsonValue]:
    per_platform: dict[str, dict[str, int]] = {}
    for entry in catalog.entries:
        bucket = per_platform.setdefault(
            entry.platform.value, {"read": 0, "mutation": 0, "denied": 0}
        )
        bucket[entry.tool_class.value] += 1
    return {
        "source": catalog.source,
        "revision": catalog.revision,
        "read": len(catalog.read_entries()),
        "mutation": len(catalog.mutation_entries()),
        "denied": len(catalog.denied_entries()),
        "platforms": per_platform,
        "denied_reasons": sorted({e.policy.reason for e in catalog.denied_entries()}),
    }


def status(root: Path) -> ActionResult:
    settings = load_settings(root)
    checks = run_doctor(settings, project_root=root)
    env = {v.name: v.is_set for v in masked_env(root)}
    accounts = read_accounts(active_accounts_path(settings, root))
    policy_file = None
    try:
        policy_file = load_write_policy_file(settings, root)
    except (ValueError, OSError):
        pass
    fixture = build_fixture_catalog()
    validated_ops = 0
    policy_issues: list[str] = []
    if policy_file is not None:
        policy, issues = policy_file.validate_against(fixture)
        validated_ops = len(policy.operations)
        policy_issues = [f"{i.tool_name}: {i.reason}" for i in issues if i.reason != "not_admitted"]
    kill_switch = settings.paid_media_kill_switch_path
    if not kill_switch.is_absolute():
        kill_switch = root / kill_switch
    model, model_error = model_or_invalid(settings)
    caps = capabilities_for(model)
    plan = plan_selection(model, max_tools=settings.paid_media_max_selected_tools)
    key_env = settings.paid_media_model_api_key_env if model_error else model_key_env(settings)
    state = state_path(settings, root)
    failing = [c.name for c in checks if c.status == "fail"]
    detail: dict[str, JsonValue] = {
        "checks": _checks_json(checks),
        "env": env,
        "runtime": settings.paid_media_runtime,
        "data_mode": settings.paid_media_data_mode,
        "model_presets": model_preset_payloads(),
        "model_key_env": key_env,
        "model_key_set": bool(key_env and env.get(key_env, False)),
        "model_base_url": settings.paid_media_model_base_url or "",
        "model": {
            "spec": settings.paid_media_model if model_error else model.spec,
            "error": model_error,
            "provider": model.provider,
            "verified": caps.verified,
            "native_tool_search": caps.native_tool_search,
            "selection": plan.strategy.value,
            "selection_reason": plan.reason,
            "package_installed": bool(model.provider) and _module_available(model.provider),
        },
        "pipeboard": {
            "token_set": settings.pipeboard_api_token is not None,
            "endpoints": {p.value: u for p, u in settings.pipeboard_endpoints().items()},
        },
        "accounts": [
            b.model_dump(mode="json", exclude={"provider_account_id"})
            | {"provider_account_id_masked": _mask_id(b.provider_account_id)}
            for b in accounts.bindings
        ],
        "accounts_path": str(active_accounts_path(settings, root).relative_to(root))
        if active_accounts_path(settings, root).is_relative_to(root)
        else str(active_accounts_path(settings, root)),
        "accounts_writable_path": str(WRITABLE_ACCOUNTS_FILE),
        "catalog": catalog_summary(fixture),
        "writes": {
            "enabled": settings.paid_media_writes_enabled,
            "kill_switch_engaged": kill_switch.exists(),
            "kill_switch_path": str(kill_switch),
            "released_revision": settings.paid_media_live_write_catalog_revision,
            "canary_tools": sorted(settings.live_write_canary_tools()),
            "policy_path": str(settings.paid_media_write_policy_path),
            "policy_operations": validated_ops,
            "policy_issues": policy_issues,
            "approvers": sorted(settings.approver_refs()),
            "allow_self_approval": settings.paid_media_allow_self_approval,
            "signing_key_set": settings.paid_media_approval_signing_key is not None,
        },
        "slack": {
            "transport": settings.slack_transport,
            "bot_token_set": settings.slack_bot_token is not None,
            "app_token_set": settings.slack_app_token is not None,
            "signing_secret_set": settings.slack_signing_secret is not None,
            "package_installed": importlib.util.find_spec("slack_bolt") is not None,
        },
        "self_hosted": {
            "state_path": str(state.relative_to(root))
            if state.is_relative_to(root)
            else str(state),
            "state_exists": state.is_file(),
            "api_tokens_set": settings.paid_media_api_tokens is not None,
            "api_host": settings.paid_media_api_host,
            "api_port": settings.paid_media_api_port,
        },
        "tooling": {"uv": shutil.which("uv") is not None, "python": sys.version.split()[0]},
        "pdf": next((c.status == "ok" for c in checks if c.name == "report_pdf"), False),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    summary = "all checks pass" if not failing else f"failing checks: {', '.join(failing)}"
    return _result(
        "status",
        "fail" if failing else "ok",
        summary,
        detail,
        command="paid-media-agent doctor --json",
    )


def _mask_id(value: str) -> str:
    return value if len(value) <= 4 else f"{value[:2]}…{value[-2:]}"


def config_view(root: Path) -> ActionResult:
    views = [v.model_dump(mode="json") for v in masked_env(root)]
    return _result(
        "config_view",
        "ok",
        f"{sum(1 for v in views if v['is_set'])} key(s) set",
        {"keys": views, "path": str(root / ".env")},
        command="paid-media-agent config show --json",
    )


def config_set(root: Path, updates: Mapping[str, str]) -> ActionResult:
    try:
        written = write_env(root, updates)
    except (EnvFileError, OSError) as exc:
        return _result("config_set", "fail", str(exc))
    return _result(
        "config_set",
        "ok",
        f"updated {', '.join(written)}",
        {"keys": written},
        command="paid-media-agent config set KEY=VALUE",
    )


def generate_secret(root: Path, key: str) -> ActionResult:
    """Create a strong value for a host-owned secret and store it.

    The signing key is never returned. An API token is shown once so the operator can store it in
    their client; `.env` keeps `token:caller` and clients send only the token part as the bearer.
    """
    if key == "PAID_MEDIA_APPROVAL_SIGNING_KEY":
        value = secrets.token_urlsafe(48)
    elif key == "PAID_MEDIA_API_TOKENS":
        value = f"{secrets.token_urlsafe(32)}:operator"
    else:
        return _result("generate_secret", "fail", f"{key} cannot be generated")
    write_env(root, {key: value})
    detail: dict[str, JsonValue] = {"key": key}
    if key == "PAID_MEDIA_API_TOKENS":
        detail["show_once"] = value
        detail["usage"] = f"Authorization: Bearer {value.split(':', 1)[0]}"
    return _result(
        "generate_secret",
        "ok",
        f"{key} generated and stored in .env",
        detail,
        command=f"paid-media-agent config generate {key}",
    )


# ---------------------------------------------------------------- tests


def models_list(root: Path, provider: str, *, api_key: str = "") -> ActionResult:
    """Current provider models, shared by the browser picker and CLI."""
    try:
        detail = list_models(root, provider, api_key=api_key)
    except ModelCatalogError as exc:
        return _result("models_list", "warn", str(exc))
    return _result("models_list", "ok", "Models loaded from the provider.", detail)


def model_test(root: Path, *, invoke: Callable[[str], str] | None = None) -> ActionResult:
    settings = load_settings(root)
    try:
        model = settings.model_settings()
    except ValueError as exc:
        return _result("model_test", "fail", str(exc))
    if model.provider == "scripted":
        return _result(
            "model_test", "skipped", "scripted model needs no provider", {"spec": model.spec}
        )
    if not _module_available(model.provider):
        provider_extra = {"google_genai": "google", "mistralai": "mistral"}.get(
            model.provider, model.provider
        )
        extra = "" if model.provider in {"anthropic", "openai"} else f" --extra {provider_extra}"
        return _result(
            "model_test",
            "fail",
            f"provider package for {model.provider} is not installed; run uv sync{extra}",
        )
    env = read_env(root)
    key_name = model_key_env(settings)
    if key_name and not env.get(key_name) and not _os_env(key_name):
        return _result(
            "model_test",
            "fail",
            f"{key_name} is not set",
            {"spec": model.spec, "missing": key_name},
        )
    started = time.monotonic()
    try:
        if invoke is None:
            from paid_media_agent.assembly import resolve_model

            chat = resolve_model(
                model,
                api_key_env=settings.paid_media_model_api_key_env,
                timeout_seconds=settings.paid_media_model_timeout_seconds,
            )
            reply = _content_text(chat.invoke("Reply with the single word OK.").content)
        else:
            reply = invoke(model.spec)
    except Exception as exc:
        return _result(
            "model_test",
            "fail",
            f"model call failed: {sanitize_exception(exc)}",
            {"spec": model.spec},
        )
    latency_ms = int((time.monotonic() - started) * 1000)
    plan = plan_selection(model, max_tools=settings.paid_media_max_selected_tools)
    return _result(
        "model_test",
        "ok",
        f"{model.spec} answered in {latency_ms} ms",
        {
            "spec": model.spec,
            "latency_ms": latency_ms,
            "selection": plan.strategy.value,
            "reply_preview": reply[:40],
        },
        command="paid-media-agent test model --json",
    )


def _os_env(name: str) -> str | None:
    import os

    return os.environ.get(name)


class LiveCatalog(BaseModel):
    """Result of a host-side live catalog load, kept separate from the model-facing runtime."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    catalog: AuthorizedToolCatalog
    loader: Any


async def _load_live(settings: Settings, root: Path) -> LiveCatalog:
    from paid_media_agent.runtime.catalog import load_catalog

    loaded = await load_catalog(
        settings.model_copy(update={"paid_media_data_mode": "live"}), project_root=root
    )
    return LiveCatalog(catalog=loaded.catalog, loader=loaded.provider)


def pipeboard_test(
    root: Path, *, loader: Callable[[Settings, Path], Any] | None = None
) -> ActionResult:
    settings = load_settings(root)
    if settings.pipeboard_api_token is None:
        return _result(
            "pipeboard_test",
            "warn",
            "PIPEBOARD_API_TOKEN is not set; the fixture catalog is active",
            {"catalog": catalog_summary(build_fixture_catalog())},
            command="paid-media-agent test pipeboard --json",
        )
    try:
        live = asyncio.run(_load_live(settings, root)) if loader is None else loader(settings, root)
    except Exception as exc:
        return _result("pipeboard_test", "fail", f"catalog load failed: {sanitize_exception(exc)}")
    summary = catalog_summary(live.catalog)
    missing = [p.value for p in PIPEBOARD_PLATFORMS if p.value not in (summary["platforms"] or {})]
    connected = any(
        entry.platform in PIPEBOARD_PLATFORMS and entry.tool_class is ToolClass.READ
        for entry in live.catalog.entries
    )
    # A user can connect any subset. An unavailable connector must not block the others.
    status_value: Status = "ok" if connected else "warn"
    note = ""
    if missing:
        note = f"; no tools loaded for {', '.join(missing)} (not connected in Pipeboard, or endpoint unreachable)"
    return _result(
        "pipeboard_test",
        status_value,
        f"live catalog {live.catalog.revision}: {summary['read']} read, {summary['mutation']} admitted mutation, {summary['denied']} denied{note}",
        {"catalog": summary},
        command="paid-media-agent test pipeboard --json",
    )


_LISTING_TOOL_RE = re.compile(
    r"^(list|get)_(?:.*_)?(customers|ad_accounts|accounts|advertisers|properties|account_summaries)$"
)
_ID_KEYS = ("customer_id", "account_id", "ad_account_id", "id")
_NAME_KEYS = (
    "descriptive_name",
    "account_name",
    "advertiser_name",
    "displayName",
    "display_name",
    "name",
    "title",
)
_CURRENCY_KEYS = ("currency_code", "currency", "account_currency")
_TZ_KEYS = ("time_zone", "timezone", "timezone_name")


def _extract_accounts(platform: str, payload: Any) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    id_keys = {
        Platform.TIKTOK_ADS.value: ("advertiser_id", "id"),
        Platform.GOOGLE_ANALYTICS.value: ("property_id", "property", "id"),
    }.get(platform, _ID_KEYS)

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return
        if not isinstance(node, dict):
            return
        # Organization/account wrappers may also have ids and names. Prefer their child accounts.
        children = [
            node[key]
            for key in (
                "accounts",
                "ad_accounts",
                "adaccounts",
                "advertisers",
                "properties",
                "propertySummaries",
                "property_summaries",
            )
            if isinstance(node.get(key), (list, dict))
        ]
        if children:
            for child in children:
                visit(child)
            return
        identifier = next((str(node[k]) for k in id_keys if node.get(k) not in (None, "")), None)
        if platform == Platform.GOOGLE_ANALYTICS.value:
            resource = node.get("name")
            if (
                identifier is None
                and isinstance(resource, str)
                and resource.startswith("properties/")
            ):
                identifier = resource
            if identifier and identifier.startswith("properties/"):
                identifier = identifier.removeprefix("properties/")
        if identifier is not None and any(
            k in node for k in (*_NAME_KEYS, *_CURRENCY_KEYS, *_TZ_KEYS)
        ):
            rows.append(
                {
                    "platform": platform,
                    "provider_account_id": identifier,
                    "name": next((str(node[k]) for k in _NAME_KEYS if node.get(k)), identifier),
                    "currency": next((str(node[k]) for k in _CURRENCY_KEYS if node.get(k)), ""),
                    "timezone": next((str(node[k]) for k in _TZ_KEYS if node.get(k)), ""),
                }
            )
            return
        for value in node.values():
            visit(value)

    visit(payload)
    unique: dict[str, dict[str, str]] = {}
    for row in rows:
        unique.setdefault(row["provider_account_id"], row)
    return list(unique.values())


def _annotate_mapped(
    root: Path, settings: Settings, rows: list[dict[str, str]]
) -> list[dict[str, str]]:
    registry = read_accounts(active_accounts_path(settings, root))
    by_id = {(b.platform.value, b.provider_account_id): b.alias for b in registry.bindings}
    return [
        {**row, "mapped_alias": by_id.get((row["platform"], row["provider_account_id"]), "")}
        for row in rows
    ]


def accounts_discover(
    root: Path, *, loader: Callable[[Settings, Path], Any] | None = None
) -> ActionResult:
    """Host-side discovery of provider accounts. The model never sees these ids."""
    settings = load_settings(root)
    if settings.pipeboard_api_token is None and not settings.direct_platforms():
        fixture_rows: list[dict[str, str]] = []
        for platform in FIXTURE_PLATFORMS:
            data = load_fixture_dataset(platform)
            fixture_rows.append(
                {
                    "platform": platform.value,
                    "provider_account_id": str(data["account_id"]),
                    "name": f"Fixture {platform.value} account",
                    "currency": str(data["currency"]),
                    "timezone": str(data["timezone"]),
                }
            )
        return _result(
            "accounts_discover",
            "warn",
            "no Pipeboard token; showing fixture accounts",
            {"accounts": _annotate_mapped(root, settings, fixture_rows), "source": "fixture"},
            command="paid-media-agent accounts discover --json",
        )
    try:
        live = asyncio.run(_load_live(settings, root)) if loader is None else loader(settings, root)
    except Exception as exc:
        return _result(
            "accounts_discover", "fail", f"catalog load failed: {sanitize_exception(exc)}"
        )
    from paid_media_agent.tools.direct import direct_read_providers
    from paid_media_agent.tools.pipeboard import invoke_mcp_tool

    direct_providers = direct_read_providers(settings)
    rows: list[dict[str, str]] = []
    used: list[str] = []
    errors: list[str] = []

    async def list_accounts(entry: CatalogEntry) -> tuple[str, Any, str | None]:
        try:
            if entry.platform in direct_providers:
                result = await direct_providers[entry.platform].call_read(entry, {})
                payload: Any = result.payload
            else:
                tool = (
                    live.loader.langchain_tool(entry.qualified_name)
                    if hasattr(live.loader, "langchain_tool")
                    else None
                )
                if tool is None:
                    return entry.qualified_name, None, None
                payload = await invoke_mcp_tool(tool, {}, timeout=60)
        except Exception as exc:
            return entry.qualified_name, None, sanitize_exception(exc)
        return entry.qualified_name, payload, None

    entries = [
        entry
        for entry in live.catalog.entries
        if entry.read_only_hint is True
        and _LISTING_TOOL_RE.match(entry.name)
        and entry.account_arg is None
        and entry.policy.reason == "no_account_scope"
    ]

    async def discover() -> list[tuple[str, Any, str | None]]:
        return await asyncio.gather(*(list_accounts(entry) for entry in entries))

    for entry, (name, payload, error) in zip(entries, asyncio.run(discover()), strict=True):
        if error is not None:
            errors.append(f"{name}: {error}")
            continue
        if payload is None:
            continue
        used.append(name)
        rows.extend(_extract_accounts(entry.platform.value, payload))
    status_value: Status = "ok" if rows else "warn"
    summary = (
        f"{len(rows)} account(s) from {len(used)} listing tool(s)"
        if rows
        else "no accounts discovered; connect platforms in Pipeboard first"
    )
    return _result(
        "accounts_discover",
        status_value,
        summary,
        {
            "accounts": _annotate_mapped(root, settings, rows),
            "tools": used,
            "errors": errors,
            "source": "pipeboard",
        },
        command="paid-media-agent accounts discover --json",
    )


def accounts_list(root: Path) -> ActionResult:
    settings = load_settings(root)
    path = active_accounts_path(settings, root)
    registry = read_accounts(path)
    rows = [
        b.model_dump(mode="json", exclude={"provider_account_id"})
        | {"provider_account_id_masked": _mask_id(b.provider_account_id)}
        for b in registry.bindings
    ]
    return _result(
        "accounts_list",
        "ok" if rows else "warn",
        f"{len(rows)} alias(es) in {path.name}",
        {"accounts": rows, "path": str(path)},
        command="paid-media-agent accounts list --json",
    )


def accounts_add(
    root: Path, *, alias: str, platform: str, provider_account_id: str, currency: str, timezone: str
) -> ActionResult:
    settings = load_settings(root)
    try:
        binding = AccountBinding(
            alias=alias,
            platform=Platform(platform),
            provider_account_id=provider_account_id.strip(),
            currency=currency.upper().strip(),
            timezone=timezone.strip(),
        )
        target = writable_accounts_path(root)
        source = active_accounts_path(settings, root)
        # The first real account starts a real file; sample aliases must not join live reads.
        if source.name == "accounts.example.toml" and not binding.provider_account_id.startswith(
            "fixture-"
        ):
            source = target
        registry = add_account(target, binding, seed_from=source)
    except (ValueError, AccountsFileError) as exc:
        return _result("accounts_add", "fail", sanitize_exception(exc))
    if active_accounts_path(settings, root) != target:
        write_env(root, {"PAID_MEDIA_ACCOUNT_CONFIG_PATH": str(WRITABLE_ACCOUNTS_FILE)})
    return _result(
        "accounts_add",
        "ok",
        f"alias {alias} mapped ({len(registry.bindings)} total)",
        {"aliases": list(registry.aliases()), "path": str(WRITABLE_ACCOUNTS_FILE)},
        command=f"paid-media-agent accounts add {alias} --platform {platform} --id <provider-id> --currency {currency} --timezone {timezone}",
    )


def accounts_remove(root: Path, alias: str) -> ActionResult:
    settings = load_settings(root)
    path = active_accounts_path(settings, root)
    if path != writable_accounts_path(root):
        return _result(
            "accounts_remove",
            "fail",
            "the active accounts file is an example; add an alias first to create config/accounts.toml",
        )
    try:
        registry = remove_account(path, alias)
    except AccountsFileError as exc:
        return _result("accounts_remove", "fail", str(exc))
    return _result(
        "accounts_remove",
        "ok",
        f"alias {alias} removed",
        {"aliases": list(registry.aliases())},
        command=f"paid-media-agent accounts remove {alias}",
    )


def catalog_show(
    root: Path, *, live: bool = False, loader: Callable[[Settings, Path], Any] | None = None
) -> ActionResult:
    settings = load_settings(root)
    if live and settings.pipeboard_api_token is not None:
        try:
            loaded = (
                asyncio.run(_load_live(settings, root))
                if loader is None
                else loader(settings, root)
            )
        except Exception as exc:
            return _result(
                "catalog_show", "fail", f"catalog load failed: {sanitize_exception(exc)}"
            )
        catalog = loaded.catalog
    else:
        catalog = build_fixture_catalog()
    entries = [
        {
            "name": e.qualified_name,
            "platform": e.platform.value,
            "class": e.tool_class.value,
            "reason": e.policy.reason,
            "description": e.description[:120],
        }
        for e in catalog.entries
    ]
    return _result(
        "catalog_show",
        "ok",
        f"{catalog.source} catalog {catalog.revision}",
        {"catalog": catalog_summary(catalog), "entries": entries},
        command="paid-media-agent catalog show --json",
    )


def policy_validate(
    root: Path, *, live: bool = False, loader: Callable[[Settings, Path], Any] | None = None
) -> ActionResult:
    settings = load_settings(root)
    try:
        policy_file = load_write_policy_file(settings, root)
    except (ValueError, OSError) as exc:
        return _result(
            "policy_validate", "fail", f"policy file unreadable: {sanitize_exception(exc)}"
        )
    if policy_file is None:
        return _result(
            "policy_validate",
            "warn",
            "no write policy file; the fixture policy applies",
            command="paid-media-agent policy validate --json",
        )
    if live and settings.pipeboard_api_token is not None:
        try:
            loaded = (
                asyncio.run(_load_live(settings, root))
                if loader is None
                else loader(settings, root)
            )
        except Exception as exc:
            return _result(
                "policy_validate", "fail", f"catalog load failed: {sanitize_exception(exc)}"
            )
        catalog = loaded.catalog
    else:
        catalog = build_fixture_catalog()
    policy, issues = policy_file.validate_against(catalog)
    blocking = [i for i in issues if i.reason != "not_admitted"]
    detail: dict[str, JsonValue] = {
        "catalog": catalog.source,
        "admitted": [op.tool_name for op in policy.operations],
        "issues": [{"tool": i.tool_name, "reason": i.reason} for i in issues],
        "denied_mutations": sorted(
            e.qualified_name for e in catalog.denied_entries() if e.read_only_hint is False
        ),
    }
    return _result(
        "policy_validate",
        "warn" if blocking else "ok",
        f"{len(policy.operations)} operation(s) admitted; {len(blocking)} blocking issue(s)",
        detail,
        command="paid-media-agent policy validate --live --json"
        if live
        else "paid-media-agent policy validate --json",
    )


def slack_test(root: Path, *, client_factory: Callable[[str], Any] | None = None) -> ActionResult:
    settings = load_settings(root)
    if settings.slack_bot_token is None:
        return _result("slack_test", "fail", "SLACK_BOT_TOKEN is not set")
    if importlib.util.find_spec("slack_sdk") is None:
        return _result("slack_test", "fail", "slack extra not installed; run uv sync --extra slack")
    try:
        if client_factory is None:
            from slack_sdk import WebClient

            client_factory = lambda token: WebClient(token=token)  # noqa: E731
        bot = client_factory(settings.slack_bot_token.get_secret_value()).auth_test()
        detail: dict[str, JsonValue] = {
            "team": bot.get("team"),
            "bot_user": bot.get("user"),
            "transport": settings.slack_transport,
        }
        if settings.slack_transport == "socket_mode":
            if settings.slack_app_token is None:
                return _result(
                    "slack_test", "fail", "SLACK_APP_TOKEN is required for Socket Mode", detail
                )
            app_token = settings.slack_app_token.get_secret_value()
            client_factory(app_token).apps_connections_open(app_token=app_token)
            detail["socket_mode"] = "connection ticket issued"
        elif settings.slack_signing_secret is None:
            return _result(
                "slack_test",
                "fail",
                "SLACK_SIGNING_SECRET is required for the HTTP transport",
                detail,
            )
    except Exception as exc:
        return _result(
            "slack_test", "fail", f"Slack rejected the credentials: {sanitize_exception(exc)}"
        )
    return _result(
        "slack_test",
        "ok",
        f"connected to {detail.get('team')} as {detail.get('bot_user')}",
        detail,
        command="paid-media-agent test slack --json",
    )


def state_test(root: Path) -> ActionResult:
    """Open the DuckDB state file and apply pending migrations; opening creates a missing file.

    Only one process can hold the file, so a running `serve` makes this report `warn`, not `fail`.
    """
    path = state_path(load_settings(root), root)
    command = "paid-media-agent test state --json"
    detail: dict[str, JsonValue] = {"path": str(path)}
    try:
        store = Store(path)
    except StoreBusy:
        return _result(
            "state_test",
            "warn",
            f"{path} is in use by another process, usually a running paid-media-agent serve",
            detail,
            command=command,
        )
    except Exception as exc:
        return _result(
            "state_test", "fail", f"cannot open state: {sanitize_exception(exc)}", detail, command
        )
    try:
        (applied,) = store.fetch("SELECT count(*) FROM schema_migrations")[0]
    finally:
        store.close()
    detail["migrations"] = int(applied)
    return _result("state_test", "ok", f"state ready at {path}", detail, command=command)


def demo_run(root: Path, *, with_proposal: bool = False) -> ActionResult:
    from paid_media_agent.testing.demo_script import run_demo

    settings = Settings(paid_media_model="scripted:demo", paid_media_allow_self_approval=True)
    try:
        result = asyncio.run(run_demo(settings, with_proposal=with_proposal, root=root))
    except Exception as exc:
        return _result("demo_run", "fail", f"demo failed: {sanitize_exception(exc)}")
    detail: dict[str, JsonValue] = {
        "answer": result["answer"],
        "audit": result["audit"],
        "catalog_revision": result["catalog_revision"],
        "analysis": result["analysis"],
    }
    if with_proposal:
        detail["proposal"] = result.get("proposal")
        detail["receipt"] = result.get("receipt")
        detail["receipt_message"] = result.get("receipt_message")
    return _result(
        "demo_run",
        "ok",
        "fixture demo completed",
        detail,
        command="paid-media-agent demo" + (" --with-proposal" if with_proposal else ""),
    )


MAX_QUESTION_CHARS = 2000


def ask_question(root: Path, question: str, *, model: Any | None = None) -> ActionResult:
    """Run one question locally through the same profile the deployment runs."""
    settings = load_settings(root)
    text = question.strip()
    if not text or len(text) > MAX_QUESTION_CHARS:
        return _result("ask", "fail", "question must be 1 to 2000 characters")
    try:
        from langchain_core.runnables import RunnableConfig

        from paid_media_agent.assembly import resolve_model
        from paid_media_agent.runtime.local import build_configured_runtime

        chat = (
            model
            if model is not None
            else resolve_model(
                settings.model_settings(),
                api_key_env=settings.paid_media_model_api_key_env,
                timeout_seconds=settings.paid_media_model_timeout_seconds,
            )
        )
        runtime = build_configured_runtime(settings, project_root=root, model=chat)
        config = RunnableConfig(
            configurable={
                "thread_id": f"console-{secrets.token_hex(4)}",
                "caller_ref": "local-user",
            }
        )
        state = asyncio.run(
            runtime.graph.ainvoke({"messages": [{"role": "user", "content": text}]}, config=config)
        )
        answer_text = _content_text(state["messages"][-1].content)
    except Exception as exc:
        return _result("ask", "fail", f"run failed: {sanitize_exception(exc)}")
    if answer_text.startswith("Model call failed"):
        # The retry middleware turns provider failures into a message; surface them as a failure.
        return _result("ask", "fail", sanitize_exception(RuntimeError(answer_text)))
    return _result(
        "ask",
        "ok",
        "answered",
        {
            "answer": answer_text[:6000],
            "selection": runtime.components.metadata.selection.strategy.value,
            "catalog_revision": runtime.catalog.revision,
        },
        command='paid-media-agent ask "..."',
    )


def kill_switch_set(root: Path, *, engaged: bool, confirmed: bool = False) -> ActionResult:
    settings = load_settings(root)
    path = settings.paid_media_kill_switch_path
    if not path.is_absolute():
        path = root / path
    if engaged:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"engaged {datetime.now(UTC).isoformat()}\n", encoding="utf-8")
        return _result(
            "kill_switch",
            "ok",
            "kill switch engaged: every execution is refused",
            {"path": str(path), "engaged": True},
            command="paid-media-agent writes kill-switch on",
        )
    if not confirmed:
        return _result(
            "kill_switch",
            "fail",
            "removing the kill switch requires confirmation",
            {"path": str(path), "engaged": path.exists()},
        )
    if path.exists():
        path.unlink()
    return _result(
        "kill_switch",
        "ok",
        "kill switch cleared",
        {"path": str(path), "engaged": False},
        command="paid-media-agent writes kill-switch off --yes",
    )


def as_json(result: ActionResult) -> str:
    return json.dumps(result.model_dump(mode="json"), indent=2, default=str)
