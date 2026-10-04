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
| `uv run paid-media-agent doctor --live` | Read each account through its live tools and check the data path end to end |
| `uv run paid-media-agent demo --visual` | Open an animated page that replays eight weeks of the agent on a simulated store, and write the report it produced. Replays a recording; calls nothing |
| `make demo-video` | Record that replay as `docs/media/demo-replay.mp4` (needs Chrome and ffmpeg; uses its own browser profile) |
| `uv run paid-media-agent demo --with-proposal` | The same flow as text: offline analysis and simulated approved change |
| `uv run paid-media-agent ask "Compare campaign performance last week"` | Run a question with your configured model |
| `uv run paid-media-agent report --cadence weekly` | Render a report without a model (`--insights` adds expected ranges and budget curves) |
| `uv run paid-media-agent sync` | Pull recent days and campaign settings into history |
| `uv run paid-media-agent backfill --start 2026-01-01` | Pull an older range into history |
| `uv run paid-media-agent history --view daily` | Show stored history: coverage, daily, settings, changes, lag |
| `uv run paid-media-agent simulate --days 180` | Simulate campaigns with known response curves into their own file |
| `uv run paid-media-agent anomalies --days 7` | Flag recent campaign-days outside their expected range |
| `uv run paid-media-agent allocate --alias demo-google` | Recommend how to split an account's daily budget; `--propose` creates proposals |
| `uv run paid-media-agent goals set --alias demo-google --target-cpa 30` | Set an account's target CPA/ROAS or monthly budget |
| `uv run paid-media-agent pacing` | Month-to-date spend against the monthly budget, and CPA/ROAS against target |
| `uv run paid-media-agent explain --alias demo-google` | Why CPA (or `--metric conversions/roas`) changed, newest week against the week before |
| `uv run paid-media-agent whatif --alias demo-google --set g-101=+20%` | Forecast spend, conversions, and CPA at different budgets; `--total +10% --split best` |
| `uv run paid-media-agent usage --days 7` | Model calls: tokens, cache hits, reported cost, failures, latency |
| `uv run paid-media-agent eval run --model openrouter:anthropic/claude-haiku-4.5` | The 30-question eval on sample data, graded and stored (bills the model) |
| `uv run paid-media-agent eval regrade RUN` | Re-check a stored eval run with the current checks (no model is called) |
| `uv run paid-media-agent context init` | Create the company-context skill from its template |
| `uv run paid-media-agent proposals list` | Proposals awaiting approval, including the budget bandit's |
| `uv run paid-media-agent proposals approve ID` | Approve one through the running API; it is applied once and read back |
| `uv run paid-media-agent bandit simulate` | Let the budget bandit run a simulated account and compare it with the truth |
| `uv run paid-media-agent bandit evaluate --seeds 3` | Regret and forecast error of the bandit and its baselines on simulated accounts |
| `uv run paid-media-agent bandit whatif-eval --seeds 5` | How close what-if forecasts come to the simulated truth |
| `uv run paid-media-agent writes kill-switch on` | Stop mutations |
| `uv run paid-media-agent test state` | Open the DuckDB state file and apply migrations |
| `uv run paid-media-agent backup` | Export the state file as Parquet (through `serve` when it runs) |
| `uv run paid-media-agent restore FOLDER --to FILE` | Build a new state file from a backup |
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
weekly report on Mondays, and a monthly report on the 1st. Add `backup` for a daily Parquet export
of the state file. Each account's sync ends on its own yesterday, in its timezone. A server started after the hour still
runs that day's jobs; a failed job waits for its next slot. A report whose windows reach back
before the stored history (a monthly report needs 56 days) is recorded as `skipped`, with the
reason, rather than failed. `POST /jobs/{name}` runs one now, and
`GET /jobs` lists recent runs. With `serve` running, `paid-media-agent sync` asks it to run the
job over the API (it needs `PAID_MEDIA_API_TOKENS`); `backfill` and `history` need `serve` stopped.
The agent itself reads history through its `query_history` tool.

History is append-only. A day pulled again adds a snapshot, so conversions reported late are
visible, and `history --view lag` shows how many arrive after each number of days. A day counts
as matured once pulled at least 7 days later (14 for LinkedIn). Campaign settings can only be
observed from the first sync onward, so days before it show no budget. A budget or status change
that did not come through this agent's approvals is logged as `external_detected`. See
[History and simulation](docs/architecture/history-and-simulation.md).

Sync and the `report` command read each platform through its read contract (below). Other
provider schemas can be explored through `ask`; they need a contract before they reach history or
typed reports.

## Live accounts

Before the first sync of a live account, run `uv run paid-media-agent doctor --live`. For each
account it checks:
- the catalog loaded;
- which read contract matched, and how far that contract is verified;
- the contract's tools and arguments are present;
- the account id shape;
- a read of the last three days normalizes, with conversions present;
- budgets are plausible against spend;
- how many provider calls a daily sync will make.

Fix each failure it names before trusting history, anomalies, or budget recommendations. See
[Live data contracts](docs/architecture/live-data-contracts.md).

- **Meta** through Pipeboard:
  - Use `provider_account_id = "act_…"`, and set `conversion_action` in `config/accounts.toml` to
    the action type that counts as a conversion. Without it, Meta conversions are recorded as
    missing; `doctor --live` lists the types your account reports.
  - Daily rows cost one call per day, so a sync re-pulls only the 8-day maturity window. Run
    `backfill` once for older days.
  - The contract comes from Pipeboard's published source; the first live run confirms it.
- **Google Ads** through Pipeboard: use the customer id as ten digits. The GAQL tool's response
  shape is unverified until the first live run.
- **Other platforms**, including Reddit, have no live contract yet. `sync` lists them as
  unavailable; the agent can still read them for questions.
- **Call limit.** Each sync or backfill stops after `PAID_MEDIA_SYNC_MAX_CALLS` provider calls
  (200) and says what it skipped, because hosted MCP plans meter calls.
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

## Goals and pacing

Each account can have a target CPA or target ROAS and a monthly budget, in its own currency:

```bash
uv run paid-media-agent goals set --alias demo-google --target-cpa 30 --monthly-budget 25000
uv run paid-media-agent goals show
uv run paid-media-agent pacing
```

- **Versioned by date.** A set applies from today in the account's timezone (or `--from`), and
  unchanged goals carry over. `--clear FIELD` removes one. `history --view goals` lists every
  version and who set it: `cli`, `console`, `api:<caller>`, or `proposal`.
- **Where they're used.**
  - `list_accounts` shows them to the agent.
  - `compare_periods` and `summarize_window` add `against_goals` readings.
  - `pacing` / `check_pacing` compare month-to-date spend with the monthly budget and project the
    month end.
  - `allocate` cuts its total when the expected CPA would exceed the target. Without `--total`,
    it follows the monthly pacing.
- **Who changes them.** You change them directly: the CLI, the setup console (Accounts → Goals),
  or `POST /goals` for approvers. With `serve` running, the CLI goes through its API. The agent
  can only propose a change (`host__set_account_goals`), which an approver approves like any
  other change.
- **Business context.** `uv run paid-media-agent context init` creates the `company-context`
  skill for the prose around the goals.

## Budget allocation

The budget bandit splits each account's total daily budget across its campaigns to maximise
conversions, CBS-style (a global model, one curve per campaign, and Thompson sampling). It reads
stored history, so `sync` first. Moves are at most 25% per decision, and a campaign whose budget
changed in the last 7 days is held.

- **`paid-media-agent allocate`** prints the recommendation for every account (or `--alias`).
  `--propose` turns each move of at least `PAID_MEDIA_BANDIT_MIN_CHANGE` (5%) into a proposal
  from requester `bandit`. Nothing is applied until an approver approves it.
- **The agent's `recommend_budgets` tool** answers "how should I split the budget" the same way.
  It changes nothing; applying a recommendation is a normal proposal in the conversation.
- **The `allocate` job** (add it to `PAID_MEDIA_JOBS`) runs on Mondays for every account. It
  proposes only when `PAID_MEDIA_BANDIT_PROPOSE=true`.
- **Reviewing.** `paid-media-agent proposals list` shows proposals awaiting a decision. Approve or
  reject one through the running API with `proposals approve ID` or `proposals reject ID`
  (`POST /proposals/{id}/approve`); approval applies the change once and reads it back. The
  caller is the first `PAID_MEDIA_API_TOKENS` entry and must be an approver. Bandit proposals do
  not appear as Slack cards.
- **Outcomes.** `history --view outcomes` shows whether each recommendation was followed and how
  many conversions its week brought against what was expected, from matured days only.
- **What limits spend.** Each recommendation labels every campaign as limited by its budget,
  by demand, by its bid target, or by a learning phase, and gives the evidence.
  - Evidence: the platform's own signals where it reports them (Google impression share lost to
    budget or rank, status reasons), otherwise how much of its budget it spends.
  - Only budget-limited campaigns can take more budget. Budget a demand- or target-limited
    campaign can't spend moves to one that can, and a campaign in learning is held.
  - `history --view constraints` and `--view signals` show the record.
  - `bandit evaluate --scenario constrained --spend-model both` compares this with the old
    linear model on simulated accounts where half the campaigns stop at a ceiling.
- **Goals.** With a target CPA, the total is cut when the model expects the account's average CPA
  to exceed it (the output says whether the step limits let it get there). With a monthly budget
  and no `--total`, the total is today's budgets scaled toward the daily spend that lands on the
  budget. Each run records its `total_source`.
- **Policy.** `PAID_MEDIA_BANDIT_POLICY` is `thompson` (explores within guardrails, the default) or
  `greedy`.

For evaluation on simulated accounts:

- `bandit simulate` warms a scenario up on an operator's budget schedule, then lets the bandit set
  budgets weekly. It prints each decision next to the true elasticity and writes every run to
  `bandit_runs` and `bandit_decisions` in `workspace/state/sim-<scenario>.duckdb`.
- `bandit evaluate` compares the bandit with static budgets, a CPA rule, and an oracle over several
  seeds, and reports each model's forecast error and interval coverage.

All of these use a local pooled regression as the global model. `PAID_MEDIA_PREDICTOR=tabpfn`
(or `--predictor tabpfn`) uses TabPFN instead:
one call per decision (at least 10,000 tokens each), under the same caps and cache as anomaly
checks. See [Budget bandit](docs/architecture/budget-bandit.md).

## Explaining changes and what-ifs

Both read stored history, so `sync` first. Neither changes anything.

- **`paid-media-agent explain`** (the agent's `explain_change`, `GET /explain`) splits a change
  in CPA, conversions, or ROAS into spend moving between campaigns and each campaign's CPM,
  click-through rate, conversion rate, and value per conversion. The parts add up to the change.
  - It marks what is within noise, and how much of a campaign's rate change its own spend change
    explains (diminishing returns).
  - It lists the settings changes in the windows.
  - The default is the newest 7 days of data against the 7 before; `--current START:END` and
    `--previous START:END` choose others.
- **`paid-media-agent whatif`** (the agent's `what_if_budgets`, `POST /what-if`) forecasts one
  account at today's budgets and at a scenario, with 80% ranges.
  - Scenarios are `--set CAMPAIGN=+20%` / `--set CAMPAIGN=150`, or `--total +10%` / `--total 1500`
    with `--split proportional|best`.
  - It reports the cost of each extra conversion, budget that campaigns limited by demand or a bid
    target cannot spend, how many changes a large move takes, the curves' best split of the same
    total, and, with goals set, the expected CPA against target and where the month would land.
  - `bandit whatif-eval` measures the forecasts against simulated truth (see
    [Budget bandit](docs/architecture/budget-bandit.md#what-if-forecasts)).

## Model usage and evals

- **Which model.** Use a Sonnet-class model for the agent: on the 30-question eval, Sonnet 5.5
  (`openrouter:anthropic/claude-sonnet-5.5`) passed 25 and Haiku 4.5 passed 13, at about
  $0.10 and $0.03 a question. Scheduled jobs (sync, reports, anomalies, allocation) do not call a
  model, so there is nothing to save with a cheaper one there yet. Re-run `eval run --model X`
  before switching.

- **Usage.** Every model call attempt is recorded in `llm_calls`, with its tokens, cache reads,
  the provider's reported cost (OpenRouter reports one), status, and latency.
  - `paid-media-agent usage [--days 7] [--by model|day|thread|purpose]` summarises it.
  - `doctor` prints the last week in one line.
  - The model cannot read it.
- **Caching and context.**
  - Anthropic models through OpenRouter use prompt caching (`PAID_MEDIA_PROMPT_CACHE=auto`; `off`
    to compare).
  - Threads past `PAID_MEDIA_CONTEXT_BUDGET_TOKENS` (60,000) send older tool results as short
    stubs; the stored thread is unchanged.
- **Evals.** `paid-media-agent eval run` asks 30 questions on the sample accounts, one fresh
  runtime each, with 28 days synced and goals set.
  - Each answer is graded by checks (tools called, expected figures, numbers found in tool
    results, nothing applied without approval, no errors) and by a judge model
    (`PAID_MEDIA_EVAL_JUDGE_MODEL`, default Claude Sonnet 5.5 on OpenRouter).
  - Runs go to `workspace/state/evals.duckdb`. `eval baseline RUN` marks the one to compare
    against, and `eval report` shows regressions, fixes, and cost and latency changes.
  - `--rpm` keeps calls under a key's rate limit (15 a minute by default).
  - A full run costs a few dollars. It is never part of `pytest` or CI.
  - See [Evals](docs/architecture/evals.md).

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
conversation and sends them back unchanged. Every authorized platform read tool is bound
while their schemas fit `PAID_MEDIA_READ_TOOLS_BUDGET_TOKENS` (default 6,000), which keeps the prompt
identical across calls so it caches. Past the budget, tools are bound as `discover_tools` finds them,
at most `PAID_MEDIA_MAX_SELECTED_TOOLS` at a time.

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
