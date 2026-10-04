# Changelog

## Unreleased

### Added

- A visual demo, and report panels that show what the models expect.
  - **`demo --visual`** (now `make demo`) opens an animated page for Northwind, a simulated
    store, built from diagrams:
    - two intuitions: a fixed ±50% rule against a learned range, and budget moving from a flat
      response curve to a steep one;
    - the feature engineering for each job, as a pipeline from a raw row to TabPFN's output;
    - a day-by-day replay of eight weeks: the agent's weekly budget moves, the conversions they
      gain, and its weekly check for unusual days against the rule's;
    - the result, the approval gate, and a link to the report the agent wrote.
  - **The report** for the store's last fortnight: what was unusual, what the budget moves
    bought, how TabPFN was applied (collapsed), next steps written by the agent and checked
    against the figures, and a change approved and read back.
  - **`make demo-video`** records the replay as `docs/media/demo-replay.mp4`.
  - **No token or model key needed.** The demo replays a recording of the weekly decisions,
    TabPFN's answers, and the agent's text, and never calls out. `demo --visual --record`
    makes the recording and `--record-watch` adds only what is missing. The pages say where
    each figure came from.
  - **`report --insights`** adds the expected-range and budget panels for a real account from
    stored history, with the configured predictor.
  - Chart geometry lives in `reports/charts.py`; a new `model` colour token marks what a model
    expected.

- Answers whose figures come from code, checked before they are sent.
  - **Arithmetic.** A `calculate` tool evaluates labelled expressions over figures from earlier
    results (exact decimals; names, powers and thousands separators refused). Prose arithmetic is
    not allowed.
  - **Verdicts.** Changes are marked better or worse; tools return rankings, window presets
    (`last_week`, `last_n_days_of_data`, `month_to_date`, `last_month`) with the dates they
    resolved to, `days_ago`, days over budget, account-to-account comparisons both ways, and
    incremental CPA against the target CPA.
  - **Answer check.** Every final answer runs the eval's grounding rule. An answer with figures
    no tool returned is sent back once, then marked unverified (`PAID_MEDIA_ANSWER_REPAIR`).
  - **Evidence the host keeps.**
    - Failed reads are listed as unavailable without the model naming them.
    - Reads follow provider pages, and summaries merge pages and per-day reads.
    - `query_history` and `read_artifact` total rows by account, campaign, day or week.
    - Empty reads, days before the data, and conversion days an anomaly check skipped are named.
  - **Proposals.** The reviewer always sees the summary written from the record. A
    recommendation is applied by its `bandit_run_id`, so the budget is never retyped. A proposal
    a turn leaves unpaused is paused by the runtime.
  - **Cost.**
    - Every authorized read tool is bound while they fit a token budget.
    - The accounts and goals are in the system prompt.
    - An explicit cache breakpoint covers the shared tools and instructions.
    - The judge skips answers that already failed a check.

    Sonnet 5.5 costs about $0.057 per measured answer, down from $0.096.
  - **Budget model.** The pooled elasticity has a weak prior, so short, barely varied histories
    stay plausible.
  - **Jobs.** A scheduled report longer than the stored history is recorded as skipped, not
    failed.

- Review fixes and eval-driven answer quality.
  - **Approvals.** Approving or rejecting a proposal decides only its own paused call, and a
    stale approval can no longer execute a newer proposal. The review card shows the paused
    proposal. Only the requester or an approver can edit or reject.
  - **Threads.** One turn runs at a time per thread. A failing progress callback no longer drops
    a pause.
  - **File tools.** Their rules ignore case, as macOS does. The model cannot write artifacts or
    reports.
  - **State and writes.**
    - Restoring an older backup opens after new migrations.
    - A connection dropped after a write is read back, not recorded as failed.
    - Only complete reads reach history.
    - Provider account ids are redacted from tool results.
    - Meta budgets use Meta's own currency offsets (`meta_minor`).
  - **Numbers.**
    - ROAS value is lag-corrected with its conversions (in `explain_change` and pacing).
    - The pacing run rate counts zero-spend days.
    - "Target CPA reached" is judged on the budgets recommended.
    - The what-if month projection builds on pacing.
    - Stale bandit proposals are superseded safely.
    - `explain_change` windows end on complete days.
    - The anomaly day-over-day rule corrects both days for lag.
    - Oversized report reads are reported as incomplete.
    - Platform minimum budgets hold rather than jump.
    - Each account gets its share of the sync call limit, newest days first.
    - Reconciliation compares against the whole read.
  - **Answers.**
    - The system prompt carries resolved calendar windows.
    - Offloaded results keep their headline, reading and caveats, and `read_artifact` pages
      through them.
    - `summarize_window` leads with a per-account headline, and cross-platform results carry
      attribution and coverage caveats.
    - `query_history` explains easily misread fields.
    - Anomaly flags show `band_distance`.
    - `recommend_budgets` names today's and the recommended total.
    - What-if best splits say which total they split.
    - `discover_write_operations` lists what is never available.
  - **Evals.**
    - Grounding ignores dates and ids and derives only from pairs in the same record.
    - Figures compare as values.
    - Failed calls do not satisfy a tool check.
    - Comparisons warn when runs were graded differently.
    - The eval agent's clock is fixed.
    - `eval regrade` re-checks stored runs.

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
