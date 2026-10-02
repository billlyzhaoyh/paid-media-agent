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

**The demo.** `demo --visual` (`testing/demo_visual.py`) renders this report for Northwind, a
simulated store, and opens it. A simulation has what real data cannot give: planted anomalies
and true response curves, so the page shows what was caught and what a budget split produced
against the best possible one.

- **One store.** Six weeks under its operator's budgets, then eight in which the agent
  reallocates weekly (`bandit/evaluate.py` `run_closed_loop`). The same store is then checked for
  unusual days and asked for its next budgets.
- **What was unusual.** The section opens with the finding from the scores, shows each method's
  alerts split into real problems and false alarms, and draws only the charts with a flagged or
  planted day, each flag labelled with what it turned out to be.
- **What the budget moves bought.** Three runs of the same store, all scored by the simulation's
  true curves: budgets left alone, the agent's weekly moves, and the best possible split
  (`reports/insights.py` `build_trial`). Each campaign's budget is shown before the agent, now,
  and as recommended next. The spend-response curves are in a collapsed block.
- **How the model is applied.** Each section shows it on one real case, from the run's own
  figures (`reports/insights.py` `_anomaly_how`, `_budget_how`).
  - Unusual days: `check_anomalies` keeps the table each request held (`AnomalyReport.inputs`).
    The page shows three of its rows for the most extreme real flag, the range that came back,
    and the verdict. Two more days say why a range per row beats a fixed rule: a budget change
    the range followed, and a day the ±50% rule flagged that the model left alone.
  - Budgets: two of a campaign's days, two of the spend levels the global model was asked
    about with its answers, and what the fitted curves say the next 100 a day buys in each
    campaign, beside the true figure.
- **Next steps.** `testing/demo_narrative.py` runs the real agent loop with the project's
  instructions, `calculate`, and two tools that return the page's own figures. Its reply is
  validated, and every figure in it is checked against the tool results; if either fails, a
  code-written text is used and the page says so.
- **Recording and replay.** `demo --visual` never calls TabPFN or a model. It replays
  `fixtures/data/demo_tabpfn_cache.json` (the weekly budgets the agent set with TabPFN's
  predictions, and TabPFN's answers for the final checks) and `fixtures/data/demo_narrative.json`
  (the agent's text). The recorded decisions rebuild the same store on any machine; the
  recordings are used only if they did, and otherwise the pooled and local models and the code
  text are used. `demo --visual --record` makes the recordings: 11 TabPFN calls (about 110,000
  tokens) and a few model calls.
- **What it measured.** The seed was chosen with the pooled model, before TabPFN saw the store.
  - Alerts: TabPFN 4 (3 of 3 planted, 1 false alarm); the local model 8 (3, 5); the rule 24
    (2, 22).
  - Budget: 39.6 conversions a day with the agent and TabPFN, 37.3 left alone, 40.0 best
    possible: +6.1%, 86% of the available gain. With the pooled model the agent reached +6.2%.
