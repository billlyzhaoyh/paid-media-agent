# History and simulation

Every read keeps its artifact, and its rows also go to append-only tables in the DuckDB state
file. The history answers questions no single read can: how a day's conversions grew as late
conversions arrived, what the budget was on a given day, and who changed it. Prediction and budget
allocation build on it.

## Sources

- **Agent reads.** `ReadDispatcher` records every performance read and every campaign listing, with
  `source = agent_read` (`report` for the scheduled and CLI reports). A history write that fails
  is logged and never fails the read.
- **Sync.** `analytics/sync.py` re-pulls a trailing window (`PAID_MEDIA_SYNC_DAYS`, default 28) and
  the current campaign settings for every alias. Late conversions arrive as newer snapshots of the
  same days. `serve` runs it daily; `paid-media-agent sync` runs it on demand.
- **Backfill.** `paid-media-agent backfill --start ... --end ...` pulls an older range in 28-day
  reads. Settings cannot be observed in the past, so backfilled days have no budget.
- **Proposals and executions.** `ProposalService` and `WriteExecutor` report each decision to
  `analytics/changes.py`: proposed, revised, approved, rejected, and the receipt status. The
  receipt stays the authority; this log is what analysis joins against.

## Tables and views

| Name | What it holds |
| --- | --- |
| `pulls` | One row per provider call: source, tool, account, requested and actual window, artifact |
| `entity_daily_snapshots` | Every entity-day of every pull, never updated |
| `entity_settings_snapshots` | Status, daily budget, and bidding observed for each campaign |
| `change_events` | Agent proposals and outcomes, and `external_detected` changes |
| `entity_daily_latest` | The newest snapshot of each entity-day |
| `entity_daily_matured` | The newest complete snapshot pulled at least `maturity_days` after the day |
| `conversion_lag` | Share of matured conversions already reported N days after the day |
| `entity_settings_history` | One row per settings version with `valid_from` and `valid_to` |
| `entity_daily_panel` | Latest rows with the budget in force, pacing, and matured conversions |

`maturity_days` defaults to 7 days per platform (14 for LinkedIn); edit the table to match your
attribution windows. `pulled_on` is the pull date in the account's timezone, so a day's age is
counted in the same calendar as the day.

A settings observation that differs from the previous one is recorded as `external_detected` unless
a verified agent change to that field explains it. Budgets are read in account currency per day,
the unit the write policy uses; other budget shapes stay in the snapshot's `raw` JSON.

## Reading it

The model reads history through `query_history`: fixed, parameterized queries over the views,
filtered by alias, entity, and window. It never writes SQL, and results name accounts by alias,
never by provider id. People use `paid-media-agent history --view coverage|daily|settings|changes|lag`.

## Scheduling

`scheduler.py` runs inside `serve`, which owns the state file. `PAID_MEDIA_JOBS` selects the
scheduled jobs (`sync`, `report_weekly` on Mondays, `report_monthly` on the 1st) at
`PAID_MEDIA_JOB_HOUR_UTC`. A job is due once its hour has come on one of its days and no scheduled
run has started that day. A failed run waits for the next slot. `POST /jobs/{name}` runs any job
now; `paid-media-agent sync` uses it when `serve` holds the file. Every run is kept in `job_runs`.

## Simulation

`sim/simulator.py` generates campaigns whose final conversions follow the response curve the budget
allocator assumes, `log(y + 1) = kappa1 + kappa2 * log(x + 1)` for spend `x`. It adds weekday
seasonality, Poisson noise, pacing below budget, conversions that arrive over several days,
labelled shocks (tracking outage, overspend, conversion surge), and campaigns that start late.
Budgets change at random intervals, like an operator's, so each curve can be identified from history.

`paid-media-agent simulate --scenario NAME` runs a scenario through the same ingest path as real
reads into `workspace/state/sim-NAME.duckdb`, with the ground truth in `sim_truth`. Simulated data
never enters the production state file. A seed fixes the whole scenario.
