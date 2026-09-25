"""Configuration and environment diagnostics that never print secret values."""

from __future__ import annotations

import importlib
import importlib.util
import os
from dataclasses import dataclass
from pathlib import Path

from paid_media_agent.config import Settings
from paid_media_agent.middleware.tool_selection import (
    PROVIDER_DISTRIBUTIONS,
    capabilities_for,
    plan_selection,
)
from paid_media_agent.runtime.profiles import load_accounts, load_write_policy_file
from paid_media_agent.tools.fixtures import build_fixture_catalog

SECRET_ENV_NAMES = (
    "PIPEBOARD_API_TOKEN",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GOOGLE_API_KEY",
    "SLACK_BOT_TOKEN",
    "SLACK_APP_TOKEN",
    "SLACK_SIGNING_SECRET",
    "PAID_MEDIA_APPROVAL_SIGNING_KEY",
    "TABPFN_TOKEN",
)


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str

    @property
    def ok(self) -> bool:
        return self.status != "fail"


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def run_doctor(settings: Settings, *, project_root: Path) -> list[Check]:
    checks: list[Check] = []
    try:
        model = settings.model_settings()
        caps = capabilities_for(model)
        plan = plan_selection(model, max_tools=settings.paid_media_max_selected_tools)
        checks.append(
            Check("model", "ok", f"{model.spec}; selection={plan.strategy.value} ({plan.reason})")
        )
        package = PROVIDER_DISTRIBUTIONS.get(model.provider, caps.integration_package)
        if package and package != "paid-media-agent":
            module = package.replace("-", "_")
            status = "ok" if _module_available(module) else "fail"
            checks.append(
                Check(
                    "model_package",
                    status,
                    f"{package} {'installed' if status == 'ok' else 'missing; install the extra'}",
                )
            )
        if not caps.verified:
            checks.append(
                Check(
                    "model_registry",
                    "warn",
                    "model is not in the tested capability registry; portable selection applies",
                )
            )
        if model.base_url is not None:
            checks.append(
                Check(
                    "model_base_url",
                    "ok",
                    "custom base URL configured; provider-native tool search disabled",
                )
            )
    except ValueError as exc:
        checks.append(Check("model", "fail", str(exc)))

    for name in SECRET_ENV_NAMES:
        present = bool(os.environ.get(name))
        checks.append(
            Check(f"env:{name}", "ok" if present else "warn", "set" if present else "not set")
        )

    direct = settings.direct_platforms()
    checks.append(
        Check(
            "direct_platforms",
            "ok" if direct else "warn",
            ", ".join(p.value for p in direct)
            if direct
            else "none configured (LinkedIn, X, OpenAI Ads are direct adapters)",
        )
    )
    if settings.pipeboard_api_token is None:
        checks.append(Check("pipeboard", "warn", "no token: fixture catalog only"))
    else:
        checks.append(
            Check("pipeboard", "ok", "token configured; live catalog will be loaded host-side")
        )

    accounts = load_accounts(settings, project_root)
    if accounts.bindings:
        checks.append(
            Check(
                "accounts",
                "ok",
                f"{len(accounts.bindings)} alias(es): {', '.join(accounts.aliases())}",
            )
        )
    else:
        checks.append(
            Check(
                "accounts",
                "warn",
                f"no aliases loaded from {settings.paid_media_account_config_path}",
            )
        )

    checks.append(
        Check(
            "writes",
            "ok" if not settings.paid_media_writes_enabled else "warn",
            "disabled (default)"
            if not settings.paid_media_writes_enabled
            else "flag is true; live writes still need the release gates in docs/operations/live-write-runbook.md",
        )
    )
    try:
        policy_file = load_write_policy_file(settings, project_root)
    except (ValueError, OSError) as exc:
        policy_file = None
        checks.append(Check("write_policy", "fail", f"unreadable: {type(exc).__name__}"))
    if policy_file is not None:
        # Offline validation uses the fixture catalog; a live runtime re-validates on startup.
        validated, issues = policy_file.validate_against(build_fixture_catalog())
        blocking = [i for i in issues if i.reason != "not_admitted"]
        detail = f"{len(validated.operations)} operation(s) admitted against the fixture catalog"
        if blocking:
            detail += "; issues: " + ", ".join(f"{i.tool_name} ({i.reason})" for i in blocking)
        checks.append(Check("write_policy", "warn" if blocking else "ok", detail))
    elif settings.pipeboard_api_token is not None:
        checks.append(
            Check(
                "write_policy",
                "warn",
                "no policy file; fixture policy applies, no live mutation is admitted",
            )
        )
    kill_switch = settings.paid_media_kill_switch_path
    if not kill_switch.is_absolute():
        kill_switch = project_root / kill_switch
    checks.append(
        Check(
            "kill_switch",
            "warn" if kill_switch.exists() else "ok",
            "engaged: every execution is refused"
            if kill_switch.exists()
            else f"clear ({kill_switch})",
        )
    )
    pinned = settings.paid_media_live_write_catalog_revision
    canary = settings.live_write_canary_tools()
    if pinned or canary:
        checks.append(
            Check(
                "live_write_gate",
                "warn",
                f"reviewed revision {pinned or 'unpinned'}; canary tools: {', '.join(sorted(canary)) or 'none'}",
            )
        )
    else:
        checks.append(Check("live_write_gate", "ok", "no live canary released"))
    if settings.paid_media_approval_signing_key is None:
        checks.append(
            Check(
                "approval_signing_key",
                "warn",
                "not set; the process uses an ephemeral key, so approvals do not survive a restart",
            )
        )
    else:
        checks.append(Check("approval_signing_key", "ok", "set"))
    approvers = settings.approver_refs()
    checks.append(
        Check(
            "approvers",
            "ok" if approvers else "warn",
            f"{len(approvers)} configured"
            if approvers
            else "none configured; only the local demo user may approve",
        )
    )

    if settings.paid_media_runtime == "self_hosted":
        slack_ready = settings.slack_bot_token is not None and (
            settings.slack_app_token is not None
            if settings.slack_transport == "socket_mode"
            else settings.slack_signing_secret is not None
        )
        checks.append(
            Check(
                "slack",
                "ok" if slack_ready else "warn",
                f"transport={settings.slack_transport}; {'configured' if slack_ready else 'not configured'}",
            )
        )
        checks.append(
            Check(
                "slack_package",
                "ok" if _module_available("slack_bolt") else "warn",
                "slack-bolt installed"
                if _module_available("slack_bolt")
                else "slack extra not installed",
            )
        )
        checks.append(
            Check("persistence", "ok", f"DuckDB state file {settings.paid_media_state_path}")
        )

    from paid_media_agent.reports.render import pdf_renderer_available

    pdf_ok, pdf_detail = pdf_renderer_available()
    checks.append(
        Check(
            "report_pdf",
            "ok" if pdf_ok else "warn",
            "WeasyPrint ready" if pdf_ok else f"HTML only; {pdf_detail}",
        )
    )
    checks.append(
        Check(
            "report_html",
            "ok" if _module_available("jinja2") else "fail",
            "jinja2 installed" if _module_available("jinja2") else "jinja2 missing",
        )
    )

    workspace = project_root / settings.paid_media_workspace_root
    writable = (
        os.access(workspace, os.W_OK) if workspace.exists() else os.access(project_root, os.W_OK)
    )
    checks.append(Check("workspace", "ok" if writable else "fail", str(workspace)))
    return checks


def format_checks(checks: list[Check]) -> str:
    width = max(len(c.name) for c in checks)
    lines = [f"{c.status.upper():<5} {c.name:<{width}}  {c.detail}" for c in checks]
    return "\n".join(lines)
