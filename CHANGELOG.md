# Changelog

## Unreleased

### Added

- Paid-media analysis through one shared assembly and agent loop, with a credential-free fixture demo.
- A local setup console and CLI for models, account connections, and running the agent.
- Pipeboard tool discovery and read-only direct adapters behind a host-controlled account catalog.
- Deterministic period comparisons and HTML/PDF reports with source reconciliation.
- Typed proposals, verified human approvals, one mutation attempt, and bounded readback receipts.
- A local-first server: one `serve` process runs the API and Slack, with proposals, approvals,
  receipts, and thread ownership in a DuckDB state file.
- Runtime skills and editable business context shared by the CLI, API, and Slack.
- Read history in the state file: every read's daily snapshots, campaign settings, and a change
  log of proposals, outcomes, and changes made outside the agent, with views for the latest,
  matured, and lag-corrected numbers. `sync`, `backfill`, and `history` commands; a
  `query_history` tool; and scheduled jobs in `serve` (daily sync, weekly and monthly reports)
  with `POST /jobs/{name}`. Adds `PAID_MEDIA_JOBS`, `PAID_MEDIA_JOB_HOUR_UTC`, and
  `PAID_MEDIA_SYNC_DAYS`.
- Anomaly checks: a `check_anomalies` tool, an `anomalies` command, and an optional daily job flag
  campaign-days outside the range predicted from history, accounting for budget changes, spend,
  and conversions still arriving. `PAID_MEDIA_PREDICTOR` chooses a local model (default), Prior
  Labs' hosted TabPFN-3.5 (opt-in, cached, and capped by `PAID_MEDIA_TABPFN_DAILY_TOKENS` and
  `PAID_MEDIA_TABPFN_MONTHLY_TOKENS`), or the ±50% day-over-day rule. `summarize_window` no
  longer flags days. `tests/eval/anomaly_backtest.py` compares them on simulated accounts.
- `simulate`: seeded synthetic campaigns with known response curves, delayed conversions, and
  labelled shocks, written to their own history file for testing prediction and allocation.
- A budget bandit, evaluated in simulation: `bandit simulate` and `bandit evaluate` split a daily budget
  across campaigns to maximise conversions (Lyft's Contextual Budgeting System, adapted), within
  step, spend-history, and hold-period bounds, and log every decision in `bandit_runs` and
  `bandit_decisions`. The global model is a local pooled regression, or TabPFN with
  `--predictor tabpfn` (predictive mean, with spend in cost-per-conversion units).
- Budget recommendations for configured accounts:
  - an `allocate` command;
  - a `recommend_budgets` agent tool;
  - an opt-in weekly `allocate` job.

  `allocate --propose` (or the job with `PAID_MEDIA_BANDIT_PROPOSE=true`) creates proposals that an
  approver reviews with `proposals list` and approves with `proposals approve`, or through
  `GET /proposals` and `POST /proposals/{id}/approve`. Approval applies the change once through
  the normal executor checks. A newer run supersedes the older run's pending proposals, and
  `history --view outcomes` shows how each recommendation turned out. Adds
  `PAID_MEDIA_BANDIT_POLICY`, `PAID_MEDIA_BANDIT_PROPOSE`, and `PAID_MEDIA_BANDIT_MIN_CHANGE`.
- Read contracts for live accounts (`tools/contracts.py`). Sync, reports, and the read path now
  find each platform's performance and settings tools, arguments, row location, and money unit
  through a contract instead of fixture tool names:
  - **Meta** through Pipeboard, built from its published server source. It reads one day per call,
    sums conversions from `actions` for the account's new `conversion_action`, and converts
    minor-unit budgets.
  - **Google Ads** through Pipeboard's GAQL tool. Its response shape is still unverified.

  Unannotated tools a contract needs are admitted as reviewed reads.
- `doctor --live` checks each live account end to end: catalog, contract, schema, a real read,
  conversions, budget units against spend, and calls per sync.
- Money units:
  - write-policy rows take `provider_units` (`minor` or `micros`), so proposals and receipts stay
    in account currency while the provider gets its own unit;
  - a reviewed, unreleased Meta budget row is in the example policy.
- Pipeboard robustness:
  - in-band provider errors raise instead of being read as data;
  - rate-limited reads retry after 2, 4, and 8 seconds;
  - pages are followed;
  - catalogs that fail to load are reported in `/health` and by `doctor --live`.
- Sync limits and scheduling:
  - `PAID_MEDIA_SYNC_MAX_CALLS` caps provider calls per sync;
  - each account syncs to its own yesterday;
  - the sync job prunes old Slack dedupe keys.
- `backup` and `restore` commands and a `backup` job: a consistent Parquet export of the state file
  while `serve` runs, restored only into a new file.

- **Account goals.** Target CPA, target ROAS, and a monthly budget per account, versioned by the
  day they take effect.
  - Set them with `goals set` / `goals show`, the setup console, or `GET/POST /goals`.
  - The agent reads them in `list_accounts` and can only propose a change
    (`host__set_account_goals`), which runs through the same approval, receipt, and change log
    as a platform change.
- **Pacing.** The `check_pacing` tool, the `pacing` command, and `GET /pacing` show:
  - month-to-date spend against the monthly budget and the projected month end (weekday-adjusted
    run rate);
  - the daily spend that lands on budget;
  - CPA and ROAS, with lag-corrected conversions, against target.
- **Analyses judged against goals.** `compare_periods` and `summarize_window` add code-written
  `against_goals` readings.
- **Budget recommendations follow goals.**
  - The total is cut when the expected account CPA would exceed the target CPA.
  - Without an explicit total, the total follows the monthly budget's pacing.
  - Runs record `total_source`, `expected_cpa`, and whether the target was reached.
  - The CPIA binding is reported.
- **Company context.** `context init` creates the company-context skill from its template.
- **Fix.** Rewriting `accounts.toml` from the console keeps `conversion_action`.

Live provider writes remain disabled by default and require the documented release gates.

### Changed

- Simulated conversions follow a pure power law in spend (zero spend buys nothing), and each
  campaign-day draws its own noise, so runs with different budgets share their noise.

- Replace LangChain, LangGraph, and Deep Agents with a small agent loop in `harness/`. One
  OpenAI-compatible adapter reaches Anthropic, OpenAI, Gemini, OpenRouter, and other providers with
  no extra packages; reasoning blocks are replayed unchanged. Conversations and changes paused for
  approval are stored in DuckDB, so an approval survives a restart and runs once. Platform tools are
  bound after `discover_tools` finds them, replacing provider tool search and the LLM selector.
  Pipeboard uses the MCP SDK directly. Adds `PAID_MEDIA_MODEL_ZERO_DATA_RETENTION` for OpenRouter;
  removes `PAID_MEDIA_TOOL_SELECTOR_MODEL` and the per-provider extras.
- Remove Managed Deep Agents, LangSmith (sandbox, gateway, deploy wizard), and Postgres. `serve`
  runs the API and Slack Socket Mode in one process; Docker runs one container.
- Clarify the README's capabilities, setup, deployment, and company customization guidance.
- Rebrand as Paid Media Agent by StructureML: StructureML mark, console palette and IBM Plex
  fonts, and warm report defaults with a distinct decrease color. Self-hosted PDFs install IBM Plex.
