# Evals

`paid-media-agent eval run` measures whether the agent answers paid-media questions well, what it
costs, and how long it takes. You run it by hand or with `make eval`. It is never part of `pytest`
or CI, because it calls real models and bills them.

## How a run works

The code is in `src/paid_media_agent/evals/`.

- **Questions.** `questions.json` holds 30 questions, each with what a good answer must do
  (`expect`) and machine-checkable fields.
- **One fresh runtime per question** (`runner.py`), so questions cannot affect each other:
  - the sample accounts, with data ending two days before today, as live platforms report;
  - an in-memory store with the last 28 days synced, the way `sync` fills it (history, settings,
    and delivery signals), so `explain_change`, `check_pacing`, `what_if_budgets`, and
    `recommend_budgets` have data;
  - goals set: `demo-google` has target CPA 30 and a monthly budget of 25,000; `demo-meta` has
    target ROAS 3;
  - the fake write provider. A change the agent proposes pauses for approval and nothing is
    applied.
- **Asking.** The question goes to a new thread. The transcript keeps every call with its
  arguments and result (offloaded results are read back), whether a change is waiting for
  approval, any provider mutation, and the thread's `llm_calls` totals.
- **Rate limits.** Calls to the model under test and to the judge are throttled (`--rpm`, 15 a
  minute by default), for keys with low limits.
- **Refusals stop a run.** If the model provider refuses the key or the account (HTTP 401, 402,
  403 in the model client's own error), the run stops and is marked incomplete. An answer that
  quotes a platform's HTTP error does not stop it.

## Question fields

| Field | Meaning |
| --- | --- |
| `id`, `category`, `text` | The question as a user would ask it |
| `expect` | What a good answer does; the judge reads it |
| `tools_all` | Groups of tool names (glob patterns allowed); each group needs at least one call |
| `tools_none` | Tools that must not be called (e.g. `propose_change` for a forecast question) |
| `figures` | A named set of figures computed from the sample rows (`figures.py`) that must appear |
| `grounded_min` | Share of stated numbers that must be found in tool results (default 0.8) |
| `expect_pause` | A change must be proposed and left waiting for approval |

## Checks

`checks.py` runs these deterministic checks on each answer:

- **no_error**: no crash, and not a "Model call failed…", "Stopped after…", or empty reply.
- **tools**: every `tools_all` group was hit and nothing in `tools_none` was called.
  - A group counts only for a call that worked. A call that errored or was refused does not
    count; a paused call waiting for approval does.
- **figures**: the expected figures appear in the answer as values, to the cent or rounded half
  up to a whole. "$13,862.42" is not 3,862.42. "Last week" can be the calendar week or the seven
  days ending on each platform's latest complete day.
- **grounded**: the numbers the answer states are looked for in the tool results and the
  artifacts they name.
  - Numbers include amounts, decimals, percentages, 12.4k / 1.2M / 3B, rates such as
    "$190/day", multiples such as "6.17x", and both ends of a range such as "2.4-3.9".
  - Each number is matched at the answer's own precision, rounded half up.
  - JSON results are read as numbers, not as text, so long floats keep their digits.
  - **Related pairs.** The difference or sum of two related figures also counts:
    - two in one record (a budget now and next);
    - or the same field in two sibling records (a proposal's `before` and `after`, a what-if's
      `baseline` and `forecast`).
  - **Percentages.** A percentage matches only:
    - a share times 100;
    - a value under a rate, share, change or ratio key;
    - or a percentage written in a result.
    A whole percentage that matches only some other figure (a count, `local_band95`) is
    unchecked, neither found nor missing.
  - **Skipped figures.** Dates, ids (`g-101`, `art_…`), hashes, years, small whole counts, and
    durations ("90 days") are skipped. Their digits are stripped from source text too, so
    "3,862.42 + 14" is not grounded by a date. Commas are removed only as thousands separators,
    so CSV cells never merge.
  - **Calculations.** A `calculate` result is a source only when every number in its
    expression is itself found in another result, or in an earlier sourced calculation. Units
    and calendar lengths (whole numbers below 32, and 100, 1000, 365, 52, 30.4) are exempt. A
    calculation on an invented number grounds nothing, and is named in the detail.
  - **When it fails.** The check fails when too few checked numbers are found, or when any
    money figure is not found: it was either invented or computed in prose. A money figure has a
    currency symbol (`$ € £ ¥`), an ISO code ("7,400 USD"), or two decimals.
  - **The same rule runs on every answer.** The code is `paid_media_agent/grounding.py`; the
    runtime sends an answer that fails it back once ([Answer check](tools-and-context.md#answer-check)).
    The transcript records `repaired` and the first `draft`, and the repair call counts in the
    question's model calls and cost. Ids and dates are skipped in the answer as in the sources,
    so a proposal id's "979b" is not 979 billion.
- **writes**: no provider mutation, and `expect_pause` questions are left waiting for approval.

## Judge

`judge.py` uses `--judge` or `PAID_MEDIA_EVAL_JUDGE_MODEL` (default
`openrouter:anthropic/claude-sonnet-5.5`).

- **Input:** the question, the expectation, the calls with their results (truncated to about
  12,000 characters), whether a change is waiting for approval, and the answer.
- **Scores:** four criteria, each from 1 to 5 with a reason:
  - `correct`: agrees with the tool results and the expectation;
  - `grounded`: nothing invented, and no arithmetic in prose;
  - `complete`: answers the question with the caveats the data needs;
  - `clear`: leads with the answer and is concise.
- **Verdict:** it passes when `correct` is at least 4 and nothing is below 3.
- **Failures:** a reply that isn't JSON gets one retry, then is recorded as a judge error.
- **Recording:** judge calls are recorded in `llm_calls` with purpose `eval_judge`.

A question **passes** when every check passes and, when a judge ran, the judge passes.
- An answer that failed a check is not judged, because it fails either way. Its judgement is
  stored as skipped and counted in the totals. The judge was a third of a run's cost.
- `--judge-all` judges every answer anyway.
- `--no-judge` runs the checks alone.

**Spending little:**
1. Iterate with `--ids` and `--no-judge`; the checks cost nothing to grade.
2. Re-grade stored runs (`eval regrade`) after changing the checks; that calls no model.
3. Judge with Sonnet 5.5 for baselines and before a commit.

Each result stores the model calls' cache use: `cache_write_tokens`, and `call_usage` with each
call's input, cached, and written tokens in order. The report shows it per question as
`5c 88%/4%w`: calls, the share of input read from cache, and the share written to it.

## Results

Runs are stored in `workspace/state/evals.duckdb`:
- `eval_runs`: model, judge, anchor, git commit, questions hash, settings, totals, and a
  baseline flag;
- `eval_results`: one row per question, with the verdict, checks, judge scores and reasons, the
  answer, calls, tokens, cost, and seconds.

`eval run` prints each verdict (with why a failure failed), the totals, and the comparison with
the baseline:
- pass rate;
- judge passes and mean scores;
- cost of the model under test plus the judge;
- model calls;
- cache hit rate;
- p50 and maximum seconds.

`eval run --repeat N` asks each question N times, stored as `q01_wow_spend#1`, `#2`, and so on.
- A question passes when a majority of its attempts pass.
- Reports show each question as k/N.
- A regression or fix is a flip of that majority, not of one attempt.

A single run moves by a few questions from the model's own variance, so compare repeated runs
before trusting a small change.

`eval regrade RUN` re-runs the deterministic checks on a stored run's transcripts, keeping its
judge verdicts and any "incomplete" marker, and calls no model. It is how a baseline is compared fairly after the checks
change. `eval baseline RUN` marks a baseline. Comparisons across a different question set or
judge are shown, with a warning that they are not like for like. `eval report [RUN] --against baseline` lists regressions
(passed before, failing now), fixes, and cost and latency deltas.

## Reading a report

- **A regression** means a question passed on the baseline and fails now. Read its checks first:
  they are deterministic. Then read the judge's reasons.
- **Judge scores** are one model's opinion, and a stronger judge is steadier. Compare runs with
  the same judge.
- **Figures follow the real date.** Data ends two days before today and "last week" moves with
  the calendar, so figures are computed per run and baselines compare verdicts, not numbers.
- **Cost** is what the provider reported (OpenRouter does). `cost n/a` means the provider reports
  none.

## Baseline (2026-09-29)

Run `717aa9bf` is the baseline. It used Claude Haiku 4.5 through OpenRouter, judged by Claude
Sonnet 5.5, with the anchor on 2026-09-27.

| | |
| --- | --- |
| Passed | 12 of 30 (40%); judge passed 12 |
| Mean judge scores | correct 3.27, grounded 3.10, complete 2.90, clear 3.87 |
| Cost | $0.83 for the agent + $0.63 for the judge; 126 model calls |
| Cache | 59% of input tokens read from cache |
| Latency | p50 16.2 s, max 46.7 s per question |

**Passed:** anomalies on Meta, both unsupported-data questions, the three write proposals (they
pause for approval and nothing is applied), refusing to bypass approval, explaining CPA, pacing
against goals, search terms (not available), the target lever, and conversion lag.

**What the failures say about the agent.** These are the work list for the next slices:
- **Windows.**
  - "Last week" is taken as the seven days ending yesterday rather than the last complete
    Monday-to-Sunday week, and then compared with a full week (q01, q02, q12).
  - "Last month" was once resolved a year off (q15).
- **Cross-platform totals.** A summary covering only Google was presented as the total for
  every platform (q05).
- **Reading tool fields.**
  - Budget lost share was read as overspend (q04).
  - The recommended total was read as current spend (q20).
  - The best split's total was misread (q18).
  - An anomaly score was read as a percentage (q21).
- **Capabilities.** Deletion was offered instead of refused (q11), and delete and raw mutate
  were not listed as refused (q14).
- **Missing caveats.** The attribution caveat was left out of a platform comparison (q30).
  Within-noise moves were called real (q22).

**Two harness fixes came from the first run.**
- The judge had been seeing truncated tool results.
- Grounding had flagged durations ("90 days") and simple differences (a budget moving 180 → 216
  stated as "+36").

The first run also found a real tool problem: the per-campaign spend-mix signs in
`explain_change`, now fixed ([History and simulation](history-and-simulation.md)).

### Prompt caching

Five questions (q12, q16, q17, q22, q28) were run with `--no-judge` and caching off, then on:

| Caching | Cost | Input from cache | Model calls |
| --- | --- | --- | --- |
| off | $0.32 | 0% | 28 |
| auto | $0.19 (-41%) | 63% | 27 |

Latency was unchanged. The report question (q12), with the longest tool loop, fell from $0.13
to $0.06.

## After S15 (2026-09-29, commit `236ca58`)

The corrected checks first re-graded the old baseline (`eval regrade`, run `399bf412`), which
scores 10 of 30. Both new runs used the same questions and the same judge (Sonnet 5.5):

| Agent model | Run | Passed | Judge passed | Mean correct / grounded / complete / clear | Agent cost | p50 |
| --- | --- | --- | --- | --- | --- | --- |
| Haiku 4.5, before the fixes (regraded) | `399bf412` | 10/30 | 12/30 | 3.27 / 3.10 / 2.90 / 3.87 | $0.83 | 16.2 s |
| Haiku 4.5, after the fixes | `6f6c6384` | 13/30 | 15/30 | 3.53 / 3.17 / 3.03 / 3.70 | $0.92 | 15.6 s |
| Sonnet 5.5 | `5e4f7dcc` | 25/30 | 25/30 | 4.30 / 4.00 / 4.10 / 4.07 | $3.06 | 23.3 s |

**Haiku, compared with the regraded baseline**
- **Fixed:**
  - q01 and q09 (the calendar windows);
  - q06 and q21 (the anomaly fields);
  - q11 (deletion is refused);
  - q22 (ROAS).
- **Regressed:** q16, q28 and q29. The judge's reasons are the model's own reading errors, and
  each question is one sample, so one run moves by a few questions.
- **What is left is mostly the model:**
  - arithmetic in prose ("86% lower");
  - misreading which campaign is dearer;
  - not fetching data it could have.

**Sonnet 5.5** passes 25 of 30 at about 3.3 times the cost.
- Four of its five failures share one pattern (q09, q10, q25, q26): it proposes the change and
  pauses for approval with no text alongside. The judge wants the summary in the message.
- The product still shows the review card, but the conversation says nothing. The runtime could
  write that summary itself from the proposal, whatever the model does.

**After the pause summary.** The loop now writes the approval summary when a model pauses without
text. Sonnet 5.5 passed each of those four write questions on both attempts
(`--ids q09,q10,q25,q26 --repeat 2`, run `ee698b0a`), where it failed all four before. Adding
those four to the full run's 25 suggests 29/30, which a full run with repeats should confirm.

## After the second review (2026-09-30)

The second review found that grounding still had false failures and blind spots:
- a proposal's before and after were never paired;
- the digits of `local_band95` and of long floats were stripped;
- CSV cells were merged;
- any number could ground a percentage;
- rates, multiples, ranges, and currency codes went unchecked.

Re-grading the stored runs with the corrected checks called no model:

| Run | Before | After | Change |
| --- | --- | --- | --- |
| Haiku baseline (`399bf412` → `ba201d2b`) | 10/30 | 12/30 | q06 (the 95% band) and q09 (+$36 from before and after) were false failures |
| Haiku after S15 (`6f6c6384` → `0de6c6a8`) | 13/30 | 12/30 | q09 now fails: "~$1,080/month" is 36 × 30 worked out in prose |
| Sonnet 5.5 (`5e4f7dcc` → `9d8cce31`) | 25/30 | 25/30 | none; "about 660 USD" is checked to the nearest ten |
| Sonnet 5.5, write questions (`ee698b0a` → `7b79004d`) | 8/8 | 8/8 | none |

A targeted live run of the changed paths passed 6 of 6 (`05c97e31`, Sonnet 5.5, $0.58 + $0.20
for the judge): pacing (q04), the approval questions (q09, q10, q25, q26), and the recommended
total (q20).

## `calculate` (2026-09-30)

These are targeted runs of the 12 questions whose stored answers did arithmetic in prose (q02, q03,
q05, q06, q09, q11, q13, q17, q18, q20, q24, q30). They ran one attempt each.

| Agent | Run | Passed | Grounding check failed | Answers calling `calculate` | Cost |
| --- | --- | --- | --- | --- | --- |
| Haiku 4.5 | `b978f679` | 4/12 (was 2/12 in `0de6c6a8`) | 2 (was 7) | 2 of 12 | $0.39 + $0.30 |
| Sonnet 5.5 | `45ec3a43` | 6/6, stopped at q13: out of credits | 0 | 3 of 6 | $0.72 + $0.27 |

**Haiku rarely calls the tool.** Its remaining failures are mostly the model reading numbers,
not doing sums:
- direction ("ROAS down from 2.44 to 2.68", CPA "improves" as it rises);
- date differences ("nine days ago" for twelve);
- relative comparisons ("86% lower" again, without `calculate`).
Those need tools that return the verdicts, not a better calculator.

## Verdicts (2026-09-30, partial)

**Haiku 4.5 on the questions that failed on interpretation** (`f37a4b47`): 2 of the first 4
questions passed. The account then ran out of credits (HTTP 402 at q15).
- **q04 (pacing): fixed.** `over_budget_days` replaced "no campaign exceeded its daily
  budget".
- **q13:** still passes.
- **q03:** the model compared 7-day windows where the question asked for two weeks.
- **q06:** it still read `band_distance` as a percentage. The flag's reading now also states the
  distance in the metric's own units.

**A no-judge probe of q30 on Haiku.** The model:
- used the `window` preset;
- quoted "46% lower (better)" (it said "86%" before);
- kept the attribution caveat.

It missed only on a hand-computed gap, which `comparisons` now states too. The Sonnet run and
the rest of the Haiku questions are still to do.

## Cost per answer (2026-10-01): before

These are the measurement questions (q01, q04, q16, q17, q20, q30), from stored runs, before
the cost slice:
- **All read tools bound.** Every authorized read is bound, so no `discover_tools` turn.
- **Accounts in the prompt.** No `list_accounts` turn.
- **Pinned provider.** A `session_id` keeps every thread on one OpenRouter provider.
- **Judge on passing answers only.** No judge call for an answer that already failed a check.

| Agent | Run | Calls per question | Input per question | From cache | Agent cost per question |
| --- | --- | --- | --- | --- | --- |
| Sonnet 5.5 | `5e4f7dcc` | 4.3 | 65k | 57% | $0.096 |
| Haiku 4.5 | `6f6c6384` | 5.2 | 59k | 63% | $0.037 |

The three-call questions read only 25–33% from cache: their first call wrote the prefix instead
of finding one an earlier question had cached.

## Cost per answer (2026-10-01): after

The same six questions, no judge, all passing:

| Agent | Run | Commit | Calls per question | Input per question | From cache | Written | Agent cost per question |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Haiku 4.5 | `53a4fb98` | `fded5f8` (cost slice) | 3.0 | 41k | 72% | 28% | $0.021 |
| Sonnet 5.5 | `f2dac9ff` | `fded5f8` (cost slice) | 4.0 | 86k | 70% | 30% | $0.098 |
| Sonnet 5.5 | `b4b35544` | S18b (system breakpoint) | 3.5 | 72k | 85% | 15% | $0.057 |

- **The cost slice cut Haiku's calls and cost** (5.2 to 3.0 calls, $0.037 to $0.021), but not
  Sonnet's ($0.096 to $0.098).
- **Why.** Per-call usage showed every question's first call writing the whole shared prefix
  (Sonnet, 15.8k tokens) while later calls read the thread perfectly. Automatic caching places
  its one breakpoint at the end of the conversation, so the tools and system prompt never had
  their own entry. The pinned session was not enough.
- **S18b adds an explicit breakpoint at the end of the system prompt.** First calls now read the
  prefix (Haiku write questions `ba778b3b`: 2 of 3 wrote nothing), and Sonnet costs $0.057 a
  question, 41% less than before either slice.

## S18 and S18b (2026-10-01)

**S18 on Haiku** (`92883660`: q09, q10, q16, q20, q25, no judge): 3/5.
- **q09 and q10 regressed.** Haiku proposed the change, then described it ("once approved, I'll
  execute it") without calling `execute_change`: nothing paused. S18's wording ("call
  `execute_change` with no message text") caused it.
- **S18b fixes it twice over.** The instructions again ask for the call in the next reply, and
  the runtime pauses any proposal a turn leaves unpaused. After: q09, q10, q25 all pause
  (`ba778b3b`, 3/3, $0.015 a question).

**Verdicts, judged on passing answers** (`1d3d866d` Haiku 2/6, `89d85fe5` Sonnet 7/10):
- **Goals the checks could not see.** Since the cost slice the accounts and goals are in the
  system prompt, which the judge and the grounding check did not get, so "target CPA 30" was
  "invented" (Sonnet q13, Haiku q30). S18b gives both the account section; Haiku q13 then passed
  (`ceb91396`).
- **Incremental CPA against the target.** Both models misjudged it on q17 ("46.83 is below the
  30.00 target"). `what_if_budgets` now states it per campaign and for the account, and Sonnet
  quotes it ("46.83 USD, 56% above the 30.00 USD target (worse)"; the cut "drops conversions that
  cost more than the target (better)").
- **Still open.** Haiku answered "this month" week over week (q15). Sonnet said conversions were
  not flagged where the anomaly check never covered the last days (q06). Haiku asked which
  account q17 meant before forecasting (`ceb91396`). Haiku's q30 omitted the attribution and
  maturity caveats.

## S18c: windows, coverage, and caveats (2026-10-01)

The three remaining misses each came from something a tool left unsaid. The runs were on 1
October, with sample data covering 2–29 September only.
- **q06, Sonnet** (`248349cf`): **passed** (judge 4/4/5/4). `check_anomalies` now names the
  conversion days it did not check (`not_checked`); before, the answer called them "within the
  expected range".
- **q15, Haiku** (`309f979c`, `c3c6f39a`): still fails.
  - **What the tools now do.** "Month to date" on a month with no data yet resolves to September
    to date, against the same days of last month, and says so. An empty read of 1 October says
    the data runs through 29 September. A window before the data's first day says the source has
    none, rather than "re-read".
  - **What remains.** The sample data has no August, so "the same days last month" cannot exist.
    Haiku says the data starts 2 September, then frames the gap as September of an earlier year.
  - **The question depends on the date.** Its expectation assumes a mid-month date and earlier
    data.
- **q30, Haiku** (`309f979c`): still fails on completeness. It summarized each platform in its
  own call, so no tool result carried the cross-platform caveat, and it did not say October has
  no data yet. `query_history` account totals now carry `comparisons`, the attribution caveat,
  and a `maturity` reading when called once for all accounts, but Haiku did not use them here.

**Stabilise the dates.** Run the date-dependent questions (q15, q30) with the eval's "today"
pinned mid-month, so they measure the agent rather than the calendar.

**Questions fixed rather than dates pinned.** Pinning the eval's date would not help: the sample
data covers 28 days, so the prior month is never in it, and the tools read the real date. Instead
q15's and q30's expectations now ask for what 28 days can answer:
- when this month has no complete day of data yet, say so and label the month used;
- when the prior month is outside the data, say so plainly rather than substitute a comparison.

`compare_periods` now names a previous window that lies entirely before the data first, ahead of a
current window that starts a day early.

| Question | Haiku 4.5 | Sonnet 5.5 |
| --- | --- | --- |
| q15 | fail (`ad856f3e`: omits that October has no data) | **pass** (`9bafb173`) |
| q30 | fail (`86228ea8`: no attribution caveat, September unlabelled) | **pass** (`cc3edd81`) |

Haiku's remaining misses are omissions the tools already state; Sonnet passes both.
