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

## Anomaly checks

`analytics/anomalies.py` reads campaign-days from the snapshots as they stood on the check date,
so a backtest sees what a live check would. For each metric it builds features per campaign-day:
campaign, platform, weekday, day index, the trailing 7-day median, the value a week earlier, a
naive expectation, and the budget in force (spend) or that day's spend (conversions). A predictor
returns the 2.5th, 50th, and 97.5th percentiles for each day in the window; a value outside that
range is flagged, with a score for how far outside it is.

Conversions train only on matured days. The conversions window trails the spend window by the
days it takes most conversions to arrive (the account's lag curve), and each day's range is scaled
by the share that has arrived, so every day is checked once and a recent day is not mistaken for a
drop. Without a predictor, with too little history, or when the predictor is unavailable, the
±50% day-over-day rule runs on the same days and every flag is labelled `dod_rule_fallback`.

Predictors live in `predict/`. `LocalPredictor` learns the spread of real values around the naive
expectation. `TabPFNPredictor` calls Prior Labs' hosted TabPFN-3.5 over REST, and only when
`PAID_MEDIA_PREDICTOR=tabpfn`. `GuardedPredictor` wraps both: an identical request is answered from
`predictor_calls`, and a TabPFN call runs only if the provider's free cost estimate fits the
account's remaining tokens and the local daily and monthly caps. Every call, cache hit, refusal,
and failure is logged there. Checks and flags go to `anomaly_checks` and `anomaly_flags`.

`tests/eval/anomaly_backtest.py` compares the methods on simulated accounts with labelled shocks.
On three seeds of eight weekly checks (about 450 campaign-days each):

| Method | Precision | Recall | False alarms per seed |
| --- | --- | --- | --- |
| ±50% day-over-day rule | 0.07 | 0.90 | 169 |
| Local band, 95% | 0.24 | 0.80 | 33 |
| TabPFN band, 95% | 0.34 | 0.78 | 21 |
| Local band, 80% | 0.07 | 0.85 | 153 |
| TabPFN band, 80%\* | 0.10 | 0.94 | 129 |

\*Measured on the earlier simulator, whose curves had a spend threshold; the other rows are
current. On that simulator the rest of the table was within 0.06 of these numbers.

At the rule's recall, the TabPFN band has fewer false alarms; at the default 95% it has an eighth
of them and misses more. Budget changes cause 17 to 21 of the rule's false alarms per seed and 2
to 4 of the 95% bands'. The bands miss shocks on low-volume campaigns, where a drop to zero or a
doubling is within normal Poisson variation. The local band is calibrated but less sharp than
TabPFN.

Known limitation: training includes every past day, anomalies too. A shock a few days before the
window can widen the range for the same campaign and hide a second one; TabPFN is more affected
than the local band because it weights recent, similar rows. Two mitigations were tried and
rejected on this backtest (on the earlier simulator): dropping training days more than 4 to 8 robust deviations from their
expectation (TabPFN precision 0.35 to 0.28, false alarms 18 to 26) and dropping days earlier
checks flagged (the band narrows with each check and false alarms grow). Neither improved recall.

## Explaining a change

`analytics/drivers.py` (`explain_change`, `paid-media-agent explain`, `GET /explain`) explains why
CPA, conversions, or ROAS changed between two windows, from `entity_daily_latest`. It works per
campaign, with conversions lag-corrected as pacing corrects them.

- **Identity.** Conversions per unit of spend is `E = sum_i w_i * e_i`, where `w_i` is the
  campaign's share of spend and `e_i = 1000 * CTR * CVR / CPM`. CPA is `1/E`, conversions `S * E`,
  and ROAS multiplies each `e_i` by value per conversion.
- **Exact split.** For campaigns that spent in both windows, the logarithmic mean Divisia index
  (LMDI-I, Ang 2004 and 2015) splits `ln(E1/E0)` into spend mix and each funnel rate, with no
  residual. A campaign that spent in only one window moves the aggregate away from the continuing
  campaigns' rate. Its share is `(its conversions - E_both x its spend) / (S x L(E, E_both))`,
  also exact. A factor that goes to zero takes its campaign's whole contribution (Ang & Liu
  2007). The log shares are scaled to percentage points of the headline change, so they add up to
  it exactly.
- **Noise.** The headline and each campaign's conversion-rate, click-through, and impression-cost
  moves are tested against Poisson noise on the reported counts (two-sided, 95%). A headline within
  noise can still hide one campaign whose rate moved by more; the reading says so.
- **Curve check.** For campaigns whose daily spend moved by 10% or more, the fitted response curve
  (`bandit/fit.py`) gives the change in conversions per unit of spend expected from the spend
  change alone. That part is diminishing returns, not a new problem.
- **Context.** Each campaign carries what limits its spend. Settings changes seen in the windows
  (budget, status, bid strategy, targets) are listed.
- **Scope.** Accounts sharing a currency are explained together; mixed currencies give one report
  per account.

The simulator can plant causes to check this: `ScenarioParams.events` holds `SimEvent`s that
change a campaign's CPM, CTR, or conversion rate from a given day. The default (none) leaves every
scenario unchanged. `tests/unit/test_sim_events.py` checks that each planted cause is named on the
right campaign and funnel step. It also checks that a budget increase shows as spend mix plus a
rate change its curve expects, and that a quiet account is mostly called noise.

## Scheduling

`scheduler.py` runs inside `serve`, which owns the state file. `PAID_MEDIA_JOBS` selects the
scheduled jobs (`sync`, `report_weekly` on Mondays, `report_monthly` on the 1st) at
`PAID_MEDIA_JOB_HOUR_UTC`. `anomalies` (daily, after the sync) can be added to the list. A job is due once its hour has come on one of its days and no scheduled
run has started that day. A failed run waits for the next slot. `POST /jobs/{name}` runs any job
now; `paid-media-agent sync` uses it when `serve` holds the file. Every run is kept in `job_runs`.

## Simulation

`sim/simulator.py` generates campaigns whose expected final conversions follow a power law in
spend `x`, `E[y] = exp(kappa1) * x ** kappa2` with `0.55 <= kappa2 <= 0.9`. Zero spend buys
nothing and each extra unit buys less, which are the assumptions the
[budget bandit](budget-bandit.md) makes. The simulator adds:

- weekday seasonality;
- Poisson noise;
- pacing below budget;
- conversions that arrive over several days;
- labelled shocks (tracking outage, overspend, conversion surge);
- campaigns that start late.

Budgets change at random intervals, like an operator's, so each curve can be identified from
history. Each campaign-day draws its noise from its own seeded generator. Two runs that set
different budgets therefore share their pacing and noise draws (common random numbers), and
policies can be compared directly.

`paid-media-agent simulate --scenario NAME` runs a scenario through the same ingest path as real
reads into `workspace/state/sim-NAME.duckdb`, with the ground truth in `sim_truth`. Simulated data
never enters the production state file. A seed fixes the whole scenario. `ScenarioDriver` runs
one day at a time with budgets from the caller; `run_scenario` drives it with the operator's
schedule, and the bandit's evaluation drives it with a policy.
