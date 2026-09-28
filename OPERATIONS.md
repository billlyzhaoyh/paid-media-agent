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
| `uv run paid-media-agent sync` | Pull the last 28 days and campaign settings into history |
| `uv run paid-media-agent backfill --start 2026-01-01` | Pull an older range into history |
| `uv run paid-media-agent history --view daily` | Show stored history: coverage, daily, settings, changes, lag |
| `uv run paid-media-agent simulate --days 180` | Simulate campaigns with known response curves into their own file |
| `uv run paid-media-agent anomalies --days 7` | Flag recent campaign-days outside their expected range |
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
keys, thread ownership, and the history of every read. DuckDB lets a single
process hold the file for writing, and while it does no other process can open it, even read-only.
That is why the API and Slack run in one process and Docker runs one container. A second process
that needs the file reports that `serve` is running; stop it or use its API.
`uv run paid-media-agent test state` opens the file and applies migrations.

A change paused for approval survives a restart: approve it afterwards and it runs once. If the
conversation moves on before a decision, the paused call is abandoned; approving it then returns
409 `conversation_expired` and nothing executes.

Follow [Self-hosting](docs/self-hosting.md) for API credentials, Slack tokens, Docker, and hosting.
Install the `self-host`, `slack`, and `reports` extras, or use the Docker image, which includes
PDF libraries and IBM Plex fonts. Slack uses native status and streaming with generic tool progress. Keep any customization
in the shared tools and skills, not tool-specific message renderers.

## Scheduled jobs and history

`serve` runs the jobs in `PAID_MEDIA_JOBS` (default `sync,report_weekly,report_monthly`) at
`PAID_MEDIA_JOB_HOUR_UTC` (default 6): a daily sync of the trailing `PAID_MEDIA_SYNC_DAYS` (28), a
weekly report on Mondays, and a monthly report on the 1st. A server started after the hour still
runs that day's jobs; a failed job waits for its next slot. `POST /jobs/{name}` runs one now, and
`GET /jobs` lists recent runs. With `serve` running, `paid-media-agent sync` asks it to run the
job over the API (it needs `PAID_MEDIA_API_TOKENS`); `backfill` and `history` need `serve` stopped.
The agent itself reads history through its `query_history` tool.

History is append-only. A day pulled again adds a snapshot, so conversions reported late are
visible, and `history --view lag` shows how many arrive after each number of days. A day counts
as matured once pulled at least 7 days later (14 for LinkedIn). Campaign settings can only be
observed from the first sync onward, so days before it show no budget. A budget or status change
that did not come through this agent's approvals is logged as `external_detected`. See
[History and simulation](docs/architecture/history-and-simulation.md).

The local `report` command uses known campaign-performance adapters. Other provider schemas can
be explored through `ask`; they need a normalization mapping before inclusion in typed reports.
HTML reports render on the host. PDF rendering requires WeasyPrint and its native libraries in the
host process. Automatic Slack PDF uploads are not included. Use the artifact API or local output
files for downloads.

On macOS, install the libraries with `brew install pango` and let Python find them by starting
commands with `DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib` (or export it in your shell).
`paid-media-agent doctor` reports "WeasyPrint ready" once PDFs can render. Reports use IBM Plex
when it is installed and fall back to system fonts otherwise; the Docker image includes Plex.

The model reads skills under `/skills` and uses `/workspace` for thread scratch files. Host-created
analysis artifacts stay on the host and are accessed through analysis tools. The tool policy exposes
filesystem operations but no arbitrary shell or subagent execution.

## Anomaly checks

`check_anomalies` (the agent's tool), `paid-media-agent anomalies`, and the optional `anomalies`
job check recent campaign-days in stored history, so run `sync` first. `PAID_MEDIA_PREDICTOR`
chooses how:

| Value | What runs | Data leaves the machine |
| --- | --- | --- |
| `local` (default) | An expected range learned from the account's own history | No |
| `tabpfn` | Prior Labs' hosted TabPFN-3.5, with `TABPFN_TOKEN` | Feature rows: codes, dates, metric values |
| `none` | The ±50% day-over-day rule | No |

TabPFN receives no campaign names, account ids, or credentials. Before each call the host asks
Prior Labs for the cost (free) and the account's remaining tokens, and refuses the call if it would
exceed them or the local caps (`PAID_MEDIA_TABPFN_DAILY_TOKENS`, default 1,000,000;
`PAID_MEDIA_TABPFN_MONTHLY_TOKENS`, default 5,000,000). Each check makes two calls and bills at
least 20,000 tokens; a repeated identical check is answered from the cache. Any refusal or failure
falls back to the day-over-day rule and says so. `predictor_calls` in the state file logs every
call. `PAID_MEDIA_ANOMALY_BAND` (default 0.95, or `--band` on the command) sets how much of normal
variation the expected range covers: 0.8 catches more at the cost of more false alarms.

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
use an in-memory database, so their proposals and history end with the process; the code fails
closed. Every decision and execution outcome is also recorded in the history's change log.

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
