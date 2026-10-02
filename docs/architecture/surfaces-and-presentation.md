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

**The demo.** `demo --visual` (`testing/demo_visual.py`) renders this report for a simulated
account and opens it. A simulation has what real data cannot give: planted anomalies and true
response curves, so the page also shows what was caught and how close each fit is.
- The account is fixed (seed, dates) and kept in `workspace/state/sim-demo.duckdb`.
- With `TABPFN_TOKEN` set, TabPFN is asked live: three calls, about 30,000 tokens, then answered
  from that state file's cache.
- Without a token, TabPFN's answers recorded for this account
  (`fixtures/data/demo_tabpfn_cache.json`) are replayed, matched by question and shape, and only
  when the account built on this machine is the same one. Otherwise the local model draws the
  panels. The page states which happened.
- The seed was chosen with the local model, before TabPFN saw the account. On it TabPFN flagged 4
  campaign-days: 3 of the 4 planted anomalies and 1 false alarm. The local model flagged 5 (3
  caught, 2 false alarms); the rule flagged 15 (4 caught, 11 false alarms).
