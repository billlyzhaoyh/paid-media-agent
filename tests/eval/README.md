# Question eval

Thirty business questions run through the agent on the sample accounts, each on a fresh runtime
whose last 28 days are synced first and whose goals are set (demo-google: target CPA 30, monthly
budget 25,000; demo-meta: target ROAS 3). Every answer gets deterministic checks (tools, figures,
grounded numbers, writes, errors) and a judge model's rubric. Runs are stored in
`workspace/state/evals.duckdb` and compared with a baseline. Not part of `pytest`; it needs model
keys and bills them.

```bash
uv run paid-media-agent eval run --model openrouter:anthropic/claude-haiku-4.5   # all 30
uv run paid-media-agent eval run --ids q16,q17 --no-judge                         # a few, checks only
uv run paid-media-agent eval run --repeat 3                                       # each question 3 times
uv run paid-media-agent eval baseline latest                                      # mark the baseline
uv run paid-media-agent eval report                                               # latest vs baseline
make eval MODEL=openrouter:openai/gpt-5.4-mini
```

The questions and their checks live in `src/paid_media_agent/evals/questions.json`; see
[docs/architecture/evals.md](../../docs/architecture/evals.md) for the schema, the checks, the
rubric, and how to read a report.

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
