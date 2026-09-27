# Operations

## Setup

Use Python 3.11+ and [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run paid-media-agent demo --with-proposal
```

For setup with a coding agent, use the [onboarding skill](.agents/skills/paid-media-onboarding/SKILL.md).
The optional `uv run paid-media-agent setup` console configures keys, accounts, and how to run it.
Business context is Markdown in `workspace/skills/company-context/`; see
[Customization](docs/customization.md).

Keep keys in `.env` or your host's secret store. Use `config show` for masked configuration. Commands
load allowlisted keys from the project `.env`; nonblank saved values take precedence over the
shell. The console is local-only. Do not expose its port as a hosted admin panel.

## Commands

Add `--help` for options and `--json` where supported for machine-readable output.

| Command | Purpose |
| --- | --- |
| `uv run paid-media-agent setup` | Optional browser setup for connections and running |
| `uv run paid-media-agent config show` | Inspect masked configuration |
| `uv run paid-media-agent config set KEY=VALUE` | Save local configuration |
| `uv run paid-media-agent config generate KEY` | Generate an API token or approval signing key |
| `uv run paid-media-agent models --provider anthropic --json` | Read the provider's current model list |
| `uv run paid-media-agent test model` | Send a short request to the configured model |
| `uv run paid-media-agent test pipeboard` | Check connected tool catalogs |
| `uv run paid-media-agent accounts discover` | Discover accessible accounts |
| `uv run paid-media-agent accounts list` | Show configured aliases |
| `uv run paid-media-agent catalog show` | Inspect tool admission and schemas |
| `uv run paid-media-agent policy validate` | Validate the write policy against the current catalog |
| `uv run paid-media-agent doctor` | Check configuration, dependencies, and runtime prerequisites |
| `uv run paid-media-agent demo --with-proposal` | Offline analysis and simulated approved change |
| `uv run paid-media-agent ask "Compare campaign performance last week"` | Run a question with your configured model |
| `uv run paid-media-agent report --cadence weekly` | Render a report without a model |
| `uv run paid-media-agent writes kill-switch on` | Stop mutations |
| `uv run paid-media-agent test state` | Open the DuckDB state file and apply migrations |
| `uv run paid-media-agent serve` | Start the API, and Slack in Socket Mode when configured |
| `uv run paid-media-agent slack` | Alias of `serve` |

## Running the server

`uv run paid-media-agent serve` starts the API. When `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` are set
and `SLACK_TRANSPORT=socket_mode`, the same process connects Slack in Socket Mode. With
`SLACK_TRANSPORT=http`, the API mounts the signed `/slack/events` endpoint instead.
`uv run paid-media-agent slack` is an alias of `serve`.

State lives in one DuckDB file, `PAID_MEDIA_STATE_PATH` (default `workspace/state/pma.duckdb`):
conversations, changes paused for approval, proposals, approval claims, receipts, Slack dedupe
keys, and thread ownership. DuckDB lets a single
process hold the file for writing, and while it does no other process can open it, even read-only.
That is why the API and Slack run in one process and Docker runs one container. A second process
that needs the file reports that `serve` is running; stop it or use its API.
`uv run paid-media-agent test state` opens the file and applies migrations.

A change paused for approval survives a restart: approve it afterwards and it runs once. If the
conversation moves on before a decision, the paused call is abandoned; approving it then returns
409 `conversation_expired` and nothing executes.

Follow [Self-hosting](docs/self-hosting.md) for API credentials, Slack tokens, Docker, and hosting.
Install the `self-host`, `slack`, and `reports` extras, or use the Docker image, which includes
PDF libraries and IBM Plex fonts. Run the `report` command from your own scheduler for recurring
reports. Slack uses native status and streaming with generic tool progress. Keep any customization
in the shared tools and skills, not tool-specific message renderers.

The local `report` command uses known campaign-performance adapters. Other provider schemas can
be explored through `ask`; they need a normalization mapping before inclusion in typed reports.
HTML reports render on the host. PDF rendering requires WeasyPrint and its native libraries in the
host process. Automatic Slack PDF uploads are not included. Use the artifact API or local output
files for downloads.

The model reads skills under `/skills` and uses `/workspace` for thread scratch files. Host-created
analysis artifacts stay on the host and are accessed through analysis tools. The tool policy exposes
filesystem operations but no arbitrary shell or subagent execution.

## Direct platforms

Pipeboard connects Google Ads, Meta Ads, TikTok Ads, Pinterest Ads, Snap Ads, Reddit Ads, LinkedIn
Ads, and Google Analytics. Catalogs load concurrently; tool selection keeps full schemas out of
every model request. Account discovery is host-side. Unknown or unscoped tools are excluded.

X Ads and OpenAI Ads have read-only direct adapters. Set their keys in `.env`, then run
`accounts discover` and map the accounts. X uses OAuth 1.0a. OpenAI Ads requires advertiser API
access. LinkedIn connects through Pipeboard.

Do not infer a connection from a saved key. Check the catalog and one real read before relying on it.

## Models

`PAID_MEDIA_MODEL` accepts `provider:model`. Every provider is reached through its OpenAI-compatible
Chat Completions endpoint, so none needs an extra package:

| Provider | Example | Key |
| --- | --- | --- |
| `anthropic` | `anthropic:claude-sonnet-4-6` | `ANTHROPIC_API_KEY` |
| `openai` | `openai:gpt-5.4-mini` | `OPENAI_API_KEY` |
| `google_genai` | `google_genai:gemini-3.8-flash` | `GOOGLE_API_KEY` |
| `openrouter` | `openrouter:anthropic/claude-haiku-4.5` | `OPENROUTER_API_KEY` |
| `groq`, `xai`, `deepseek`, `mistralai`, `moonshot`, `zhipu` | `groq:<model>` | the provider's key |

For any other OpenAI-compatible endpoint, set `PAID_MEDIA_MODEL_BASE_URL` and
`PAID_MEDIA_MODEL_API_KEY_ENV` to the name of its key environment variable. With OpenRouter,
`PAID_MEDIA_MODEL_ZERO_DATA_RETENTION=true` routes only to endpoints that do not retain prompts.

Reasoning models return reasoning blocks with each tool call; the loop stores them with the
conversation and sends them back unchanged. Platform tools are bound to the model only after
`discover_tools` finds them, at most `PAID_MEDIA_MAX_SELECTED_TOOLS` at a time.

## Writes

Account changes are disabled by default. The [live-write runbook](docs/operations/live-write-runbook.md)
covers reviewed tool policies, approver identities, catalog pinning, and the kill switch.
The conversation's approval pause does not replace the host's policy or digest checks.

`serve` keeps proposals, claims, and receipts in the DuckDB state file. `ask`, `demo`, and tests
use an in-memory database, so their proposals end with the process; the code fails closed.

## Verification

```bash
uv sync --all-extras --dev
make check
uv run paid-media-agent demo --with-proposal
```

Tests are offline by default. `PAID_MEDIA_LIVE_TESTS=1 uv run pytest tests/integration -q` opts into
read-only integration checks with your configured credentials. No automated test executes a live
provider mutation. Synthetic data is anchored two days before today; set
`PAID_MEDIA_FIXTURE_ANCHOR=2026-08-28` to reproduce the shipped windows.

Refer to [official sources](docs/sources/official-links.md) before changing provider contracts.
