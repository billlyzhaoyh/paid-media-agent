# Changelog

## Unreleased

### Added

- Quality and cost you can see. Every model call attempt is recorded in `llm_calls` with tokens,
  cache reads and writes, the provider's reported cost, status, and latency (`usage` command,
  `history --view usage`, a `doctor` line). Anthropic models on OpenRouter use prompt caching
  (`PAID_MEDIA_PROMPT_CACHE`). Threads past `PAID_MEDIA_CONTEXT_BUDGET_TOKENS` send older tool
  results as stubs that keep their artifact ids; stored history is unchanged. `eval run`,
  `eval report`, and `eval baseline` replace the hand-run question scripts: 30 questions on
  synced sample accounts with goals, graded by deterministic checks (tools, figures, grounded
  numbers, writes, errors) and a judge model (`PAID_MEDIA_EVAL_JUDGE_MODEL`), stored with a
  baseline. Zero-data-retention routing is now sent to OpenRouter only.
- Why a KPI changed, and what if: an `explain_change` tool, `explain` command, and `GET /explain`
  split a change in CPA, conversions, or ROAS exactly (LMDI) into spend moving between campaigns
  and each campaign's CPM, click-through rate, conversion rate, and value per conversion. They
  mark noise, separate diminishing returns from new problems with each campaign's response
  curve, and list the settings changes in the windows. A `what_if_budgets` tool, `whatif`
  command, and `POST /what-if` forecast spend, conversions, and CPA for different budgets with
  80% ranges, using the bandit's curves and spend ceilings: the cost of each extra conversion,
  budget that cannot be spent, step advice, the curves' best split, and goals and monthly pacing.
  `bandit whatif-eval` checks forecasts against simulated truth. The simulator can plant CPM,
  CTR, and conversion-rate changes (`ScenarioParams.events`). Curve fitting is shared as
  `bandit/fit.py` (`fit_account`); recommendations are unchanged.
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

- **Budget recommendations know what limits each campaign's spend.**
  - **Classification.** Each campaign is labelled budget, demand, bid target, or learning, with
    evidence:
    - platform signals where reported: Google impression share lost to budget or rank, status
      reasons, and bid-strategy status, read by a new signals call;
    - otherwise its spend against budget and its bid strategy.
  - **Spend ceilings.** For demand- or target-limited campaigns, a spend ceiling fitted from
    censored history (spend = min(ρ·budget, D)) stops the budget at what the campaign can spend.
    Budget it can't use moves to campaigns where the budget binds, and expected conversions no
    longer count spend that can't happen.
  - **Holds and platform rules.** Learning phases hold the budget. Verified platform rules
    tighten steps and minimums: Demand Gen ±15%; TikTok ±30%, 2 days apart, and $20 minimum;
    Snapchat $5 minimum.
  - **Pacing readings** state each platform's window.
  - **Records.** New tables `entity_daily_signals`, `entity_delivery_status`, and
    `bandit_decision_constraints`, with `signals` and `constraints` history views.
  - **Evaluation.** `bandit evaluate --scenario constrained --spend-model both` compares the new
    model with the old on simulated accounts where campaigns stop at a ceiling.

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
