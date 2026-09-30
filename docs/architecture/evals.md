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
  - **When it fails.** The check fails when too few checked numbers are found, or when any
    money figure is not found: it was either invented or computed in prose. A money figure has a
    currency symbol (`$ € £ ¥`), an ISO code ("7,400 USD"), or two decimals.
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
`--no-judge` runs the checks alone.

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
