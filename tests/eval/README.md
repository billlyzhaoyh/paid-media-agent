# Question eval

Fifteen business questions with expectations, run through the agent loop in-process on the
synthetic fixtures with your configured model, and graded against ground truth computed from the
same fixtures. Not part of `pytest`; it needs a model key.

```bash
PAID_MEDIA_MODEL=openrouter:anthropic/claude-haiku-4.5 \
  uv run python tests/eval/run_questions.py results.jsonl      # optional: question ids to run
uv run python tests/eval/grade.py results.jsonl                # numeric checks + a review table
```

`grade.py` verifies the figures that have a deterministic answer (spend, CPA per window) and
prints every answer's tools, timing, and the expectation to judge by hand. Add a question by
appending to `questions.json` with an `expect` line; add a numeric check in `grade.py` when the
answer has one.

The grader shifts its windows by the same fixture anchor the server uses (`PAID_MEDIA_FIXTURE_ANCHOR`,
default two days ago), so figures line up on any day.

# Anomaly backtest

`anomaly_backtest.py` simulates accounts with labelled shocks (tracking outage, overspend,
conversion surge) and operator budget changes, then runs weekly anomaly checks exactly as a live
check would and scores each method's precision and recall per campaign-day.

```bash
uv run python tests/eval/anomaly_backtest.py                     # the rule vs the local band
uv run python tests/eval/anomaly_backtest.py --band 0.8          # a wider expected range
TABPFN_TOKEN=... uv run python tests/eval/anomaly_backtest.py --tabpfn --seeds 1   # bills tokens
```

With `--tabpfn` each weekly check makes two TabPFN calls (about 20,000 tokens).
