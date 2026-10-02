"""Onboarding routes: ordered steps whose status is derived from the current host state."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from paid_media_agent.domain.common import JsonValue

StepStatus = Literal["done", "todo", "optional", "blocked"]
ActionKind = Literal["form", "test", "run", "process", "link", "command", "accounts"]


class StepAction(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ActionKind
    label: str
    keys: tuple[str, ...] = ()
    """Env keys a `form` action edits."""
    action: str = ""
    """Server action name for `test`, `run`, `process`, `accounts`."""
    href: str = ""
    payload: dict[str, JsonValue] = Field(default_factory=dict)


class Step(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    description: str
    status: StepStatus
    cli: str
    action: StepAction | None = None
    note: str = ""


class Route(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    title: str
    tagline: str
    description: str
    steps: tuple[Step, ...]

    @property
    def done(self) -> int:
        return sum(1 for s in self.steps if s.status == "done")


def _get(detail: dict[str, JsonValue], *path: str) -> JsonValue:
    node: JsonValue = detail
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def build_routes(detail: dict[str, JsonValue]) -> list[Route]:
    env = _get(detail, "env") or {}
    model = _get(detail, "model") or {}
    slack = _get(detail, "slack") or {}
    self_hosted = _get(detail, "self_hosted") or {}
    writes = _get(detail, "writes") or {}
    accounts = _get(detail, "accounts") or []
    token_set = bool(_get(detail, "pipeboard", "token_set"))
    model_ready = bool(model.get("provider_supported")) and bool(_get(detail, "model_key_set"))
    direct_keys = {
        "x": (
            "X_ADS_CONSUMER_KEY",
            "X_ADS_CONSUMER_SECRET",
            "X_ADS_ACCESS_TOKEN",
            "X_ADS_ACCESS_TOKEN_SECRET",
        ),
        "openai_ads": ("OPENAI_ADS_API_KEY",),
    }
    direct_set = {
        name: all(isinstance(env, dict) and env.get(k) for k in keys)
        for name, keys in direct_keys.items()
    }
    accounts_path = str(_get(detail, "accounts_path") or "")
    real_accounts = accounts_path.endswith("config/accounts.toml")

    local = Route(
        id="local",
        title="Model & testing",
        tagline="Test your agent locally",
        description="Configure a model, try sample campaigns, and run reports from your terminal.",
        steps=(
            Step(
                id="tooling",
                title="Install dependencies",
                description="Install the Python packages this project needs. Requires Python 3.11 or later and uv.",
                status="done" if _get(detail, "tooling", "uv") else "todo",
                cli="uv sync --all-extras --dev",
            ),
            Step(
                id="demo",
                title="Try sample campaigns",
                description="Compare spend and conversions using sample ad accounts. No API key needed.",
                status="todo",
                cli="uv run paid-media-agent demo --with-proposal",
                action=StepAction(
                    kind="run", label="Run demo", action="demo_run", payload={"with_proposal": True}
                ),
            ),
            Step(
                id="model",
                title="Choose a model",
                description="Choose a model and add its provider key. Use Setup for the guided model picker.",
                status="done" if model_ready else "todo",
                cli="uv run paid-media-agent config set PAID_MEDIA_MODEL=anthropic:claude-sonnet-4-6 ANTHROPIC_API_KEY=...",
                action=StepAction(
                    kind="form",
                    label="Save model settings",
                    keys=(
                        "PAID_MEDIA_MODEL",
                        "ANTHROPIC_API_KEY",
                        "OPENAI_API_KEY",
                        "GOOGLE_API_KEY",
                        "PAID_MEDIA_MODEL_BASE_URL",
                    ),
                ),
            ),
            Step(
                id="model_test",
                title="Test the model",
                description="Send one short request to check that your model and API key work.",
                status="optional" if not model_ready else "todo",
                cli="uv run paid-media-agent test model --json",
                action=StepAction(kind="test", label="Test model", action="model_test"),
            ),
            Step(
                id="report",
                title="Run a weekly report",
                description="Compare the last seven complete days with the previous week. Save the report as HTML and PDF.",
                status="optional",
                cli="uv run paid-media-agent report --cadence weekly",
                action=StepAction(kind="command", label="Copy command"),
            ),
            Step(
                id="ask",
                title="Ask a question",
                description="Ask about campaign performance using your configured model and accounts.",
                status="optional",
                cli='uv run paid-media-agent ask "Which campaign moved the most in the last two weeks?"',
                action=StepAction(kind="command", label="Copy command"),
            ),
        ),
    )

    pipeboard = Route(
        id="pipeboard",
        title="Pipeboard",
        tagline="Ad platforms and Google Analytics",
        description="Connect Google, Meta, TikTok, Pinterest, Snap, Reddit, LinkedIn, and Google Analytics through Pipeboard. Choose the accounts and properties the agent can analyze.",
        steps=(
            Step(
                id="pb_account",
                title="Connect platforms in Pipeboard",
                description="Sign in, connect your ad platforms, and create a scoped read-only API token.",
                status="done" if token_set else "todo",
                cli="open https://pipeboard.co/connections",
                action=StepAction(
                    kind="link", label="Open Pipeboard", href="https://pipeboard.co/connections"
                ),
            ),
            Step(
                id="pb_token",
                title="Add your token",
                description="Add the API token you created in Pipeboard. It is stored locally.",
                status="done" if token_set else "todo",
                cli="uv run paid-media-agent config set PIPEBOARD_API_TOKEN=...",
                action=StepAction(kind="form", label="Save token", keys=("PIPEBOARD_API_TOKEN",)),
            ),
            Step(
                id="pb_test",
                title="Check the connection",
                description="Check which ad platforms and reporting tools your token can access.",
                status="blocked" if not token_set else "todo",
                cli="uv run paid-media-agent test pipeboard --json",
                action=StepAction(kind="test", label="Check connection", action="pipeboard_test"),
            ),
            Step(
                id="pb_accounts",
                title="Choose ad accounts",
                description="Find connected ad accounts and give each a short name to use in conversations.",
                status="done"
                if real_accounts and accounts
                else ("blocked" if not token_set else "todo"),
                cli="uv run paid-media-agent accounts discover --json",
                action=StepAction(
                    kind="accounts", label="Discover accounts", action="accounts_discover"
                ),
            ),
            Step(
                id="pb_live_test",
                title="Test a live account read",
                description="Run the integration test against a connected account. It reads data without changing campaigns.",
                status="optional",
                cli="PAID_MEDIA_LIVE_TESTS=1 uv run pytest tests/integration -q",
                action=StepAction(kind="command", label="Copy command"),
            ),
        ),
    )

    org_route = Route(
        id="org",
        title="Business context",
        tagline="Markdown maintained in your project",
        description="Give the agent your conversion definitions, targets, and campaign conventions.",
        steps=(),
    )

    direct = Route(
        id="direct",
        title="Direct connections",
        tagline="X and OpenAI Ads",
        description="Connect each platform with its own credentials, then choose the accounts to analyze.",
        steps=(
            Step(
                id="direct_x",
                title="X Ads",
                description="Add the app key and user access tokens from your X developer account. Requires Ads API access.",
                status="done" if direct_set["x"] else "optional",
                cli="uv run paid-media-agent config set X_ADS_CONSUMER_KEY=... X_ADS_CONSUMER_SECRET=... X_ADS_ACCESS_TOKEN=... X_ADS_ACCESS_TOKEN_SECRET=...",
                action=StepAction(
                    kind="form",
                    label="Save X Ads credentials",
                    keys=(
                        "X_ADS_CONSUMER_KEY",
                        "X_ADS_CONSUMER_SECRET",
                        "X_ADS_ACCESS_TOKEN",
                        "X_ADS_ACCESS_TOKEN_SECRET",
                    ),
                ),
            ),
            Step(
                id="direct_openai_ads",
                title="OpenAI Ads",
                description="Add a key issued for the OpenAI Ads API. A model API key does not grant Ads access.",
                status="done" if direct_set["openai_ads"] else "optional",
                cli="uv run paid-media-agent config set OPENAI_ADS_API_KEY=...",
                action=StepAction(
                    kind="form", label="Save OpenAI Ads key", keys=("OPENAI_ADS_API_KEY",)
                ),
            ),
            Step(
                id="direct_accounts",
                title="Choose ad accounts",
                description="Find accounts from your connected platforms and give each a short name.",
                status="done"
                if real_accounts and accounts
                else ("blocked" if not token_set and not any(direct_set.values()) else "todo"),
                cli="uv run paid-media-agent accounts discover --json",
                action=StepAction(
                    kind="accounts", label="Discover accounts", action="accounts_discover"
                ),
            ),
        ),
    )

    socket_ready = bool(slack.get("bot_token_set")) and (
        bool(slack.get("app_token_set"))
        if slack.get("transport") == "socket_mode"
        else bool(slack.get("signing_secret_set"))
    )
    slack_route = Route(
        id="slack",
        title="Slack",
        tagline="Talk to the agent in Slack",
        description="Use your own Slack app. The agent connects it over Socket Mode in the same process as the API.",
        steps=(
            Step(
                id="sl_app",
                title="Create a Slack app",
                description="Use config/slack-manifest.example.yaml, install the app to your workspace, and copy its tokens.",
                status="done" if slack.get("bot_token_set") else "todo",
                cli="open https://api.slack.com/apps?new_app=1",
                action=StepAction(
                    kind="link", label="Open Slack API", href="https://api.slack.com/apps?new_app=1"
                ),
            ),
            Step(
                id="sl_tokens",
                title="Add Slack credentials",
                description="Add a bot token, then an app-level token for Socket Mode or a signing secret for HTTP.",
                status="done" if socket_ready else "todo",
                cli="uv run paid-media-agent config set SLACK_BOT_TOKEN=... SLACK_APP_TOKEN=... SLACK_TRANSPORT=socket_mode",
                action=StepAction(
                    kind="form",
                    label="Save Slack settings",
                    keys=(
                        "SLACK_TRANSPORT",
                        "SLACK_BOT_TOKEN",
                        "SLACK_APP_TOKEN",
                        "SLACK_SIGNING_SECRET",
                    ),
                ),
            ),
            Step(
                id="sl_test",
                title="Test the connection",
                description="Check the bot token and, for Socket Mode, the app-level token.",
                status="blocked" if not socket_ready else "todo",
                cli="uv run paid-media-agent test slack --json",
                action=StepAction(kind="test", label="Test Slack", action="slack_test"),
            ),
            Step(
                id="sl_run",
                title="Start the agent",
                description="Start the API and Slack together on this machine. Connection status and logs appear here.",
                status="blocked" if not socket_ready else "todo",
                cli="uv run paid-media-agent serve",
                action=StepAction(kind="process", label="Start agent", action="serve"),
            ),
        ),
    )

    api_set = bool(self_hosted.get("api_tokens_set"))
    state_path = str(self_hosted.get("state_path") or "")
    self_route = Route(
        id="self_hosted",
        title="Run",
        tagline="Run on this machine or with Docker",
        description="One process serves the API and, when configured, Slack. State lives in a local DuckDB file.",
        steps=(
            Step(
                id="sh_tokens",
                title="API credentials",
                description="Create an API access token and a signing key. The token is shown once, so keep it somewhere secure.",
                status="done" if api_set and writes.get("signing_key_set") else "todo",
                cli="uv run paid-media-agent config generate PAID_MEDIA_API_TOKENS PAID_MEDIA_APPROVAL_SIGNING_KEY",
                action=StepAction(
                    kind="run", label="Generate credentials", action="generate_secrets"
                ),
            ),
            Step(
                id="sh_state",
                title="Check local state",
                description=f"Open {state_path or 'the state file'}, create it if missing, and apply migrations. A running agent holds the file, so the check then reports it as in use.",
                status="done" if self_hosted.get("state_exists") else "todo",
                cli="uv run paid-media-agent test state --json",
                action=StepAction(kind="test", label="Check state", action="state_test"),
            ),
            Step(
                id="sh_serve",
                title="Start the agent",
                description="Start the API, and Slack when its tokens are set, on this machine using the configured host and port.",
                status="todo",
                cli="uv run paid-media-agent serve",
                action=StepAction(kind="process", label="Start agent", action="serve"),
            ),
            Step(
                id="sh_docker",
                title="Run with Docker",
                description="Run the same process in a container. Credentials come from your local .env file and state stays in a named volume.",
                status="optional",
                cli="docker compose up -d --build",
                action=StepAction(kind="command", label="Copy command"),
            ),
            Step(
                id="sh_http_slack",
                title="Connect Slack over HTTP",
                description="Use an HTTP endpoint instead of Socket Mode. Add the signing secret and configure the request URL in Slack.",
                status="optional",
                cli="uv run paid-media-agent config set SLACK_TRANSPORT=http SLACK_SIGNING_SECRET=...",
                action=StepAction(
                    kind="form",
                    label="Save HTTP transport",
                    keys=("SLACK_TRANSPORT", "SLACK_SIGNING_SECRET"),
                ),
            ),
        ),
    )

    return [
        local,
        pipeboard,
        org_route,
        direct,
        self_route,
        slack_route,
    ]
