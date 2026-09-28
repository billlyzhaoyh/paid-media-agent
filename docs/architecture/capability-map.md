# Capability map

| User capability | Agent entry | Trusted implementation | State | Main failure signal |
|---|---|---|---|---|
| Run fixture demo | `paid-media-agent demo` | `runtime/local.py` + scripted model + `tools/compute.py` | thread + `workspace/analysis` artifacts | fixture/schema mismatch |
| Discover tools | `discover_tools` | `tools/catalog.py` search; activates tools for the thread | catalog revision | stale or unknown tool |
| Read platform data | `<platform>__<tool>` | `tools/reads.py` dispatcher + guard | artifact metadata | auth, scope, schema, or partial source failure |
| Query history | `query_history`; `paid-media-agent history` | `analytics/history.py` fixed queries over the views | history tables in the state file | unknown alias; days never pulled |
| Build history | reads, `sync`, `backfill`, scheduled jobs | `analytics/ingest.py`, `analytics/sync.py`, `scheduler.py` | pulls, snapshots, settings, `job_runs` | provider failure listed as unavailable |
| Simulate accounts | `paid-media-agent simulate` | `sim/simulator.py`, `sim/scenario.py` | `sim-<scenario>.duckdb` with `sim_truth` | invalid scenario name |
| Check anomalies | `check_anomalies`; `paid-media-agent anomalies`; `anomalies` job | `analytics/anomalies.py`, `predict/` | `anomaly_checks`, `anomaly_flags`, `predictor_calls` | too little history, predictor unavailable (rule runs, labelled) |
| Allocate budgets (simulated) | `paid-media-agent bandit simulate`, `bandit evaluate` | `bandit/` over `analytics/panel.py`; `predict/` for TabPFN | `bandit_runs`, `bandit_decisions` in the scenario file | data checks fail (last good curves reused), no valid curve (budget held) |
| Summarize performance | `summarize_window` | `tools/summary.py` | per-entity totals, pacing, and daily series artifact | unsupported metric, grain, or window |
| Compare performance | `compare_periods` | `tools/compute.py` | `PeriodComparison` artifact | incompatible window, grain, unit, or currency |
| Generate report | `render_report` | `reports/render.py` + bridge | artifact receipt | reconciliation or render failure |
| Propose change | `propose_change` | `tools/writes.py` `ProposalService` | persisted ChangeSet | invalid target or policy denial |
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
