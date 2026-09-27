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
