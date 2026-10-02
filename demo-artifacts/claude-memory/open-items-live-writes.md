---
name: open-items-live-writes
description: "Standing open items and constraints for paid-media-agent (keys to rotate, live writes off, no push, Docker/Pipeboard unverified)"
metadata:
  node_type: memory
  type: project
  originSessionId: 21dd3051-34b4-476d-a502-4a650ffd5ab5
  modified: 2026-09-30T12:00:00.000Z
---

**Open items, as of 2026-09-28:**
- The user pasted TabPFN and OpenRouter keys in chat; they should rotate them. Never print token values from `.env`.
- Live writes stay off. Live platform budget units, such as Meta cents, are unverified.
- The Docker image and a live Pipeboard check are unverified. At the time Docker Desktop was not running and there was no Pipeboard token.
- On 2026-09-30, at the user's request, branch `local-first-runtime` was pushed to github.com/billlyzhaoyh/paid-media-agent (private) at `cfc4730`, and PR #10 was opened into `main`. Push later commits or change the PR only when asked, using `GITHUB_TOKEN=` for the personal account (git push works that way).
- The TabPFN anomaly backtest row in the docs comes from the old simulator and is marked with an asterisk.
- Found in S14 (2026-09-29):
  - **Lag tail.** Conversions that arrive after the 7-day maturity window are never counted, because `completeness` is 1.0 at maturity. The simulator puts about 4% of conversions there, so history and curves run a few percent low. This makes the what-if 80% ranges too narrow (61–80% coverage).
  - **`ask` and the console.** They use an ephemeral runtime whose history comes only from the agent's own reads, with no delivery signals. In that runtime g-101 is not treated as demand-limited, and what-if and recommendations differ from `serve`, which uses the synced state file.

- Found in S11 (2026-09-29):
  - **Flaky test (fixed 2026-09-30).** `test_allocate_path::test_the_agent_recommends_budgets_without_changing_anything` now pins the Thompson seed.
  - **Eval baseline.** Run `717aa9bf` in `workspace/state/evals.duckdb` passed 12 of 30 with Haiku 4.5. Agent-quality work list:
    - windows ("last week" as 7 days to yesterday);
    - Google-only totals presented as cross-platform;
    - misread fields (budget lost share, recommended total, best-split total, anomaly score);
    - offering deletion;
    - missing attribution caveats.
    These are listed in `docs/architecture/evals.md`.

- **Eval cost (2026-09-30).** The user considers a full $12 eval run (30 questions × 3 repeats with Sonnet) too expensive. Verify with `eval regrade` (free) plus a targeted `--ids` run of the changed paths (under $1). Stored runs `717aa9bf` (baseline), `6f6c6384`, `5e4f7dcc`, `ee698b0a`, and their regrades are listed in `docs/architecture/evals.md`.
- **S16 `calculate` (2026-09-30).** Committed as `ea88fb7` and pushed to PR #10. On the targeted questions Haiku went from 2/12 to 4/12 and called the tool in only 2 answers. Sonnet passed 6/6 before the OpenRouter credits ran out (HTTP 402 at q13).
- **S17 verdicts (2026-09-30).** Committed as `6e9c842` and pushed to PR #10. It covers audit items 1–5:
  - better/worse polarity and rankings;
  - window presets and `days_ago`;
  - budgets from normalised settings (the live unit bug), `over_budget_days`, and the pacing budgets-vs-needed verdict;
  - platform `comparisons`, stated both ways with the gap;
  - verdict fields kept in offload stubs.
  The live eval is partial: credits ran out at q15. After the user tops up (about $2.30), finish it with `eval run --ids q15,q17,q18,q20,q24,q30` on Haiku, then all ten (q03,q04,q06,q13,q15,q17,q18,q20,q24,q30) on Sonnet 5.5, and record the results in docs/architecture/evals.md. Items 6–9 are S18.
- **S18a cost slice (2026-10-01).** Committed as `e175e6b` and pushed to PR #10. It covers:
  - binding every read tool when they fit 6k tokens (stable and sorted; append-only discovery past that);
  - the accounts in the system prompt;
  - an OpenRouter `session_id` per deployment for sticky routing;
  - cache writes and per-call usage in eval results (migration 0013);
  - no judge on answers that failed a check (`--judge-all` overrides).
  The before numbers are in evals.md: Sonnet 4.3 calls, 65k input, 57% cached, $0.096 per question. The after run (`--ids q01,q04,q16,q17,q20,q30 --no-judge`, Haiku then Sonnet) waits for a credit top-up.
- **S18 audit items 6–9 + elasticity (2026-10-01), committed `fded5f8` and pushed to PR #10.** Six items:
  - code summary always on pause (Slack too), and `propose_change(bandit_run_id=...)`;
  - host-tracked failed reads (`failed_reads`, `ToolContext.failed_reads`) fill `unavailable_sources`;
  - reads follow pages (max 20), summaries merge artifacts per account (`tools/performance.py`);
  - `group_by`/`fields` on `query_history` and `read_artifact`;
  - runtime grounding (`grounding.py`), one repair then a flag, `PAID_MEDIA_ANSWER_REPAIR`, migration 0014 `draft`;
  - pooled slope prior (precision 1 at 0.5; sample slopes now 0.68/0.67/0.52).
  `make check` passed with 507 tests; regrades matched (Sonnet 25/30, Haiku 12/30). The live Haiku check (write questions + q09, q16, q20, `--no-judge`, about $0.40) waits for a top-up.
- **Live evals after the S18 top-up (2026-10-01).** About $2.70 spent in total. They found an S18 regression: q09 and q10 proposed without pausing. S18b, committed as `de072e3` and pushed to PR #10, fixes four things:
  - the runtime auto-pauses unpaused proposals (`host-pause-` calls);
  - an explicit system-prompt cache breakpoint (Sonnet $0.098 → $0.057 per question, 85% cached);
  - the account section counts as a grounding and judge source (migration 0015);
  - what-if `incremental_vs_target`.
  `make check` passed with 511 tests. The results are recorded in evals.md. Still open: q15 window choice (Haiku), q06 anomaly coverage wording, q30 caveats.
- **S18c (2026-10-01), committed `022157a` and pushed to PR #10.** Four changes:
  - `month_to_date` falls back to the latest month with data and compares against the same days of last month (`Resolved` with notes);
  - empty reads say why;
  - `query_history` account totals carry comparisons, caveats and maturity;
  - `check_anomalies` reports `not_checked`.
  Sonnet q06 now passes. Haiku q15 and q30 still fail: the sample data covers only Sep 2–29, so the q15/q30 expectations depend on the date. Suggestion: pin the eval's "today" mid-month.
- **Eval question fix (2026-10-01), committed `52071b9` and pushed to PR #10.** Pinning the eval date was rejected: the sample data covers 28 days and the tools read the real date. Instead:
  - q15/q30 expectations rewritten for 28 days of data;
  - `compare_periods` names a previous window entirely before the data first.
  Sonnet passes q15 and q30; Haiku still omits the caveats.
- **Submission status (2026-10-02).** The user dropped S12 and S10 and is preparing a hackathon submission.
  - PR #10 is merged into main (`55f1981`). PR #12 (README and eval-log results) is open on `local-first-runtime`.
  - The full Sonnet eval on the final code passed 28/30 (run `56a4fed0`, $0.046 per answer).
  - `gh` defaults to the upstream langchain-ai repo: pass `--repo billlyzhaoyh/paid-media-agent`.
- **Visual demo (2026-10-02), committed `f993335` and pushed to PR #12.** The user wanted a visual demo that showcases TabPFN.
  - `demo --visual` (now `make demo`) renders the report for a fixed simulated account (`testing/demo_visual.py`, seed 8) with new report panels (`reports/insights.py`, `reports/charts.py`, `templates/insights.html.j2`): expected ranges, budget curves, an approved change.
  - TabPFN answers are recorded in `fixtures/data/demo_tabpfn_cache.json` and replayed without a token, also in Docker/Linux. Live runs cost 30k tokens each; two were made (60k), one of them by accident: an empty `TABPFN_TOKEN=` env var does NOT override `.env`.
  - On the demo account TabPFN flagged 4 days (3 of 4 planted, 1 false alarm), local 5 (3, 2), rule 15 (4, 11).
  - `report --insights` adds the panels for real accounts. `make check` passed with 525 tests.
- **Demo page redesigned as a story (2026-10-02), committed `bd19b67` on branch `demo-story`, PR #13 open into `main`.** PR #12 was merged (`0c8ff7e`) first. The user found the first page unreadable (period bars, six charts with no point, curves with no result) and liked only "Next steps".
  - The page is now Northwind, a simulated store (seed 4, 6 weeks warm-up + 8 weeks of weekly reallocation): what was unusual; what the budget moves bought; next steps written by the agent; an approved change.
  - Measured: TabPFN 4 alerts (3 of 3 real, 1 false), local 8 (3, 5), rule 24 (2, 22). Budgets left alone 37.3 conversions a day, the agent with TabPFN 39.6 (+6.1%, 86% of the gain), best possible 40.0. The pooled model reached +6.2%, so TabPFN matched it and did not beat it.
  - `demo --visual` never calls TabPFN or a model, even with keys set; it replays `fixtures/data/demo_tabpfn_cache.json` and `demo_narrative.json`. `demo --visual --record` re-records (11 calls, 110k TabPFN tokens, about $0.10 of model spend). An accidental extra 30k tokens were billed before the replay-only rule was added; about 200k of the 1M daily cap was used on 2026-10-02.
  - After a second round of feedback ("show how TabPFN is applied: input, output, why it is a good approach"), both sections gained a visible "How TabPFN is applied" block with a worked example (`reports/insights.py` `_anomaly_how`, `_budget_how`; `AnomalyReport.inputs` keeps the request's table). No re-recording was needed. `make check` passed with 530 tests.
  - WeasyPrint limits found: no `paint-order`, a grid row with an SVG child cannot break across pages, inline-flex children need `flex: none`.
- **Animated demo page (2026-10-02), on branch `demo-story` for PR #13, uncommitted until the user approves.** The user found the report-style page too wordy and static, and wanted diagrams, a video of the agent acting day by day, the feature engineering highlighted, and quick intuition for both use cases.
  - `make demo` now opens `workspace/out/demo.html` (`testing/demo_story.py`, `testing/templates/demo.*`): two intuition diagrams, two feature pipelines, a 56-day replay player, the result and approval gate, and a link to the report (`demo_report.html`, kept as the agent's output with its how-blocks collapsed).
  - `make demo-video` writes `docs/media/demo-replay.mp4` (`testing/demo_video.py`).
  - Weekly TabPFN checks over the eight weeks were recorded with `demo --visual --record-watch` (16 calls, 160k tokens; about 360k of the 1M daily cap used on 2026-10-02). Measured: TabPFN 8 alerts (6 of 10 real, 2 false), local 33 (8, 25), rule 83 (9, 74).
  - `make check` passed with 535 tests.
- **The user is cost-conscious about tokens (2026-10-01).** They asked why spend was so high. Prefer stable prompts, fewer turns, and cheap evals.
- **Harness anti-pattern audit (2026-09-30), by impact:**
  1. No better/worse direction or ranking on CPA and ROAS changes (`PlatformHeadline`, explain and what-if readings).
  2. No per-day overspend check, and `summary.budgets_from_payload` reads the raw fixture budget shape, so live micros and cents are unconverted (a live bug).
  3. The model does date arithmetic: no window presets, no `days_ago`.
  4. No code-written platform-vs-platform comparison.
  5. Pacing gives no budget-vs-goal verdict.
  6. Raw rows with no filter or aggregate, and offload stubs drop `against_goals` and flags.
  7. The model re-types the proposal summary.
  8. No runtime grounding check of answers.
  9. `unavailable_sources` is bookkeeping left to the model, and paged reads can't be summarized.

**Why:** these are the user's standing constraints, carried across sessions.

**How to apply:** mention them when reporting a slice's status. Never run live mutations or live TabPFN calls in automated tests.

Related: [[roadmap-after-s7]]
