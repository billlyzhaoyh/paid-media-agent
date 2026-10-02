# Capability map

| User capability | Agent entry | Trusted implementation | State | Main failure signal |
|---|---|---|---|---|
| Run fixture demo | `paid-media-agent demo` | `runtime/local.py` + scripted model + `tools/compute.py` | thread + `workspace/analysis` artifacts | fixture/schema mismatch |
| Discover tools | `discover_tools` | `tools/catalog.py` search; binds tools for the thread when the catalog is too large to bind whole | catalog revision | stale or unknown tool |
| Read platform data | `<platform>__<tool>` | `tools/reads.py` dispatcher + guard | artifact metadata | auth, scope, schema, or partial source failure |
| Query history | `query_history` (rows, or totals with `group_by`); `paid-media-agent history` | `analytics/history.py` fixed queries over the views | history tables in the state file | unknown alias or field; days never pulled |
| Build history | reads, `sync`, `backfill`, scheduled jobs | `tools/contracts.py` per platform; `analytics/ingest.py`, `analytics/sync.py`, `scheduler.py` | pulls, snapshots, settings, `job_runs` | no matching contract, provider failure, or call limit, listed as unavailable |
| Check a live connection | `paid-media-agent doctor --live` | `analytics/live_check.py` over the contracts | `doctor` pulls in history | catalog not loaded, schema gap, no rows, implausible budget unit |
| Back up state | `paid-media-agent backup`, `restore`; `backup` job | `store/backup.py` (Parquet export under the write lock) | `state/backups/pma-<stamp>` | folder exists, not a backup, target exists |
| Simulate accounts | `paid-media-agent simulate` | `sim/simulator.py`, `sim/scenario.py` | `sim-<scenario>.duckdb` with `sim_truth` | invalid scenario name |
| Check anomalies | `check_anomalies`; `paid-media-agent anomalies`; `anomalies` job | `analytics/anomalies.py`, `predict/` | `anomaly_checks`, `anomaly_flags`, `predictor_calls` | too little history, predictor unavailable (rule runs, labelled) |
| Set and read goals | `list_accounts`; `goals set/show`; console Accounts → Goals; `GET/POST /goals` | `analytics/goals.py` (`GoalStore`, versioned by date) | `account_goals` | unknown alias, invalid value, state file busy (use the API) |
| Propose a goal change | `propose_change` with `host__set_account_goals` | `tools/host_writes.py` through `ProposalService` and `WriteExecutor` | proposal, claim, receipt, `change_events` (account) | invalid field, self-approval, kill switch |
| Check monthly pacing | `check_pacing`; `paid-media-agent pacing`; `GET /pacing` | `analytics/pacing.py` over `entity_daily_latest` and goals | none (computed) | no recent spend, no monthly budget (reported, not judged) |
| Explain a change | `explain_change`; `paid-media-agent explain`; `GET /explain` | `analytics/drivers.py` (LMDI over `entity_daily_latest`; curves from `bandit/fit.py`) | none (computed) | no history in a window, no conversions in one window (reported, not split) |
| Forecast budget scenarios | `what_if_budgets`; `paid-media-agent whatif`; `POST /what-if` | `bandit/whatif.py` over `bandit/fit.py` | none (computed) | unknown campaign, no running campaigns, data checks fail (last good curves) |
| Know what limits spend | `recommend_budgets` constraint; `history --view constraints/signals` | `tools/contracts.py` signals, `bandit/constraints.py`, `bandit/ceiling.py`, `bandit/platform_rules.py` | `entity_daily_signals`, `entity_delivery_status`, `bandit_decision_constraints` | signals call fails (falls back to spend history), too few days for a ceiling |
| Recommend budgets | `recommend_budgets`; `paid-media-agent allocate`; `allocate` job | `bandit/` over `analytics/panel.py`; `predict/` for TabPFN | `bandit_runs`, `bandit_decisions`, `bandit_outcomes` | data checks fail (last good curves reused), no valid curve or hold (budget kept) |
| Propose budgets as the host | `allocate --propose`; `allocate` job with `PAID_MEDIA_BANDIT_PROPOSE` | `bandit/proposals.py` via `ProposalService` | proposals in `host:bandit:<run_id>` threads | operation not admitted, proposal refused (noted per campaign) |
| Review host proposals | `proposals list/approve/reject`; `GET /proposals`, `POST /proposals/{id}/approve` | `AgentRunner` host path → `WriteExecutor` | claims, receipts, `change_events` | not an approver, already decided, gate refusal |
| Evaluate the bandit | `paid-media-agent bandit simulate`, `bandit evaluate`, `bandit whatif-eval` | `bandit/evaluate.py`, `sim/scenario.py` | scenario files | invalid scenario name |
| Read a large result | `read_artifact` (pages, or a read's rows filtered and totalled) | `tools/artifact_read.py` over `ArtifactStore` | none | unknown or invalid artifact id; filters on non-row artifacts |
| See model usage and cost | `paid-media-agent usage`; `history --view usage`; `doctor` | `harness/usage.py` from the loop | `llm_calls` | cost not reported by the provider (shown as such) |
| Evaluate answers | `paid-media-agent eval run/report/baseline`; `make eval` | `evals/` (runner, checks, judge) | `eval_runs`, `eval_results` in `workspace/state/evals.duckdb` | no model key, rate limits (`--rpm`), judge reply not JSON (recorded) |
| Summarize performance | `summarize_window` | `tools/summary.py` | per-entity totals, pacing, and daily series artifact | unsupported metric, grain, or window |
| Compare performance | `compare_periods` (pages merged; failed reads listed by the host) | `tools/compute.py`, `tools/performance.py` | `PeriodComparison` artifact | incompatible window, grain, unit, or currency; missing days |
| Check an answer's figures | every final answer, automatically | `grounding.py` `answer_check`, run by `harness/loop.py` | the draft and a host note in the thread; `llm_calls` purpose `repair` | figures no tool returned: one repair, then marked unverified |
| Generate report | `render_report` | `reports/render.py` + bridge | artifact receipt | reconciliation or render failure |
| Propose change | `propose_change` (or by `bandit_run_id` for a recommendation) | `tools/writes.py` `ProposalService`; `bandit/proposals.py` `propose_decision` | persisted ChangeSet; `bandit_decisions.proposal_id` | invalid target or policy denial; stale, ineligible, or already-proposed recommendation |
| Edit proposal | a new proposal in chat (Block Kit edit on a custom channel) | proposal revision service | new digest and revision | stale approval |
| Approve or reject | Slack Approve/Reject buttons or the API | approval service | signed claim or rejection | identity, expiry, replay, or signature failure |
| Execute change | `execute_change` after the approval pause | `WriteExecutor` behind `WriteGate` | attempt record | kill switch, gate refusal, stale catalog or policy, provider error |
| Discover admitted mutations | `discover_write_operations` | validated `WritePolicyFile` | policy issues in `doctor` | row fails validation against the current catalog |
| Verify change | resumed graph | bounded readback adapter | WriteReceipt | mismatch or unknown outcome |
| Deliver artifact | `render_report` files | host artifact bridge | artifact receipt | unsafe path, type, size, or delivery error |
| Read business context | runtime skills | `workspace/skills/company-context/` | curated Markdown, excluded from Git | missing definitions or targets remain unknown |
| Run the server | `serve` (`slack` alias), `docker compose up` | shared components + DuckDB state (`runtime/self_hosted.py`, `store/`) | server thread | auth, state file busy, or adapter mismatch |
| Run locally | `paid-media-agent ask`, `report` | the same components compiled by `runtime/local.py` | in-memory thread and history | model key or catalog mismatch |
| Onboard and operate | `setup` console or CLI groups | `admin/actions.py` (host-side, no model) | `.env`, `config/accounts.toml`, process logs | doctor failures, invalid key, gate refusal |

Update this table whenever a capability, entry point, state owner, or terminal condition changes.
