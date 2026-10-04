# Conversation surfaces

The shared `AgentRunner` drives caller-owned graph threads. The API and Slack run in one `serve`
process and use the same proposal service, approval policy, and DuckDB state.

## Slack

The agent summarizes a proposed action before `execute_change` pauses for approval.

One async Bolt app serves Socket Mode and signed HTTP. Bolt verifies requests
and acknowledges events before starting the run. `SlackDelivery` sends assistant Markdown and
generic tool progress through Slack's native streaming API. The session is `processing` while
the agent works, `suspended` when approval is required, and `active` after completion or failure.
Tool progress includes the name and status, never arguments or raw results. No tool-specific
cards or paid-media report layouts are embedded in the Slack adapter.

The only custom Block Kit is a generic Approve/Reject action row. Button values are opaque
routing IDs. The host reloads the proposal and verifies reviewer identity, revision, and payload
before executing. Edits invalidate previous approval. Receipts remain ordinary agent messages.

HTTP events are processed in the server process after acknowledgment. Keep that process running;
this starter does not include a durable background queue or cross-process cancellation service.

## API

`POST /threads/{thread_id}/messages` returns a completed response with text, proposal, receipt,
and available actions. It is a small application API. Bearer tokens map to caller identities; threads belong to their creator.

Proposal routes support read, approve, edit, and reject. Report downloads use
`GET /threads/{thread_id}/artifacts/{name}`. The caller must own the thread, and the file must
appear in a persisted `render_report` result from that thread. Paths, file types, and sizes are
validated before delivery. Reports are generated as HTML/PDF files; the Slack adapter does not
automatically upload them.

## Setup and schedules

The setup console configures model access, connected accounts, and how to run the agent. It does not host
agent chat or collect business knowledge. Coding agents and humans edit business context and
runtime skills in `workspace/`; see [business context](../customization.md).

Schedule the CLI `report` command with your existing scheduler. It uses the same reporting tools
and approval boundary as the server. See [self-hosting](../self-hosting.md).

## Reports and the visual demo

The report is the one visual surface: an HTML page (and a PDF where WeasyPrint's libraries are
installed) rendered by `reports/render.py` from a typed `ReportPayload`. The model writes only the
bounded narrative; code owns the layout and every number.

Beyond the two-window comparison, a report can carry optional panels (`ReportPayload.insights`,
built by `reports/insights.py`):

- **Expected range and anomalies.** Every campaign-day the anomaly check judged, with the range
  it was judged against, the days flagged, and how the other methods (the local model, the ±50%
  day-over-day rule) judged the same days. It reuses `check_anomalies`' own request, so a check
  and its chart share one cached predictor call.
- **Budget response and recommendation.** Each campaign's days, the global model's suggested
  points, the fitted curve, and the current and recommended budgets from `recommend`.
- **A change.** A proposal's summary from its record, and its receipt.

Each panel names the model that drew it and where its figures came from: a live call, the stored
result of an identical earlier call, a recording, the local model, or the rule. Chart geometry is
computed in `reports/charts.py` and unit-tested; the template only draws. A company's own
template folder and token set fall back to the defaults for anything they do not define.

`report --insights` adds the first two panels for a real account from its stored history, with
the configured predictor (the free local model by default). Scheduled reports do not add them.

`ReportPayload.omit` leaves out standard sections (the account-performance charts, the
all-accounts table, the per-platform sections) for a page about one account's panels.

**The demo.** `demo --visual` (`testing/demo_visual.py`) builds Northwind, a simulated store,
and writes two pages: an animated demo page and the agent's report for the last fortnight. A
simulation has what real data cannot give: planted anomalies and true response curves, so the
pages show what was caught and what a budget split produced against the best possible one.

- **One store.** Six weeks under its operator's budgets, then eight in which the agent works
  each week: it checks the week just gone for unusual days (`check_anomalies`, reading history
  as it stood that day) and reallocates the budgets (`bandit/evaluate.py` `run_closed_loop`).
- **The demo page** (`testing/demo_story.py`, `testing/templates/demo.*`). One self-contained
  file with inline styles, script and data.
  - `build_story` turns the three runs, the weekly checks, the truth and the agent's text into
    one JSON document. The script only draws it.
  - Two intuition diagrams: a fixed rule against a learned range on one real series, and two
    true response curves with the budget moving from the flatter to the steeper.
  - Two feature pipelines: a raw row, the engineered row, the model, the output. The measured
    effect of the feature choices is quoted from this project's backtests and labelled as such.
  - The replay: one clock in days drives every panel, and `render(t)` depends on nothing else,
    so scrubbing, `?t=<day>` and video frames show exactly what playback shows.
- **The video** (`testing/demo_video.py`, `make demo-video`). One headless Chrome on its own
  throwaway profile sets the clock frame by frame over the DevTools protocol; ffmpeg joins the
  frames.
- **The report** (`demo_report.html`). The agent's own output: what was unusual in the last
  fortnight, what the budget moves bought, its next steps, and an approved change. Its "How
  TabPFN is applied" blocks are collapsed on screen and open in the PDF.
- **The agent's text.** `testing/demo_narrative.py` runs the real agent loop with the project's
  instructions, `calculate`, and two tools that return the report's own figures. Its reply is
  validated, and every figure in it is checked against the tool results; if either fails, a
  code-written text is used and the page says so.
- **Recording and replay.** `demo --visual` never calls TabPFN or a model. It replays
  `fixtures/data/demo_tabpfn_cache.json` (the weekly budgets the agent set with TabPFN's
  predictions, and TabPFN's answers for the weekly checks and the final ones) and
  `fixtures/data/demo_narrative.json` (the agent's text).
  - The recorded decisions rebuild the same store on any machine. The recordings are used only
    if they did; otherwise the pooled and local models and the code text are used.
  - A recorded answer is matched by purpose, shape and the sum of its training targets in
    hundredths, which tells the weekly checks apart and is a whole number on any machine.
  - `demo --visual --record` makes everything from scratch. `--record-watch` keeps what is
    recorded and asks TabPFN only for what is missing.
- **What it measured.** The seed was chosen with the pooled model, before TabPFN saw the store.
  - Eight weekly checks, 10 planted problems: TabPFN 8 alerts (6 real, 2 false); the local
    model 33 (8, 25); the ±50% rule 83 (9, 74). TabPFN misses more and alarms far less.
  - The last fortnight, in the report: TabPFN 4 alerts (3 of 3, 1 false); the local model 8
    (3, 5); the rule 24 (2, 22).
  - Budget: 39.6 conversions a day with the agent and TabPFN, 37.3 left alone, 40.0 best
    possible: +6.1%, 86% of the available gain. With the pooled model the agent reached +6.2%.
