# Changelog

## Unreleased

### Added

- Paid-media analysis through one shared assembly and agent loop, with a credential-free fixture demo.
- A local setup console and CLI for models, account connections, and running the agent.
- Pipeboard tool discovery and read-only direct adapters behind a host-controlled account catalog.
- Deterministic period comparisons and HTML/PDF reports with source reconciliation.
- Typed proposals, verified human approvals, one mutation attempt, and bounded readback receipts.
- A local-first server: one `serve` process runs the API and Slack, with proposals, approvals,
  receipts, and thread ownership in a DuckDB state file.
- Runtime skills and editable business context shared by the CLI, API, and Slack.
- Read history in the state file: every read's daily snapshots, campaign settings, and a change
  log of proposals, outcomes, and changes made outside the agent, with views for the latest,
  matured, and lag-corrected numbers. `sync`, `backfill`, and `history` commands; a
  `query_history` tool; and scheduled jobs in `serve` (daily sync, weekly and monthly reports)
  with `POST /jobs/{name}`. Adds `PAID_MEDIA_JOBS`, `PAID_MEDIA_JOB_HOUR_UTC`, and
  `PAID_MEDIA_SYNC_DAYS`.
- `simulate`: seeded synthetic campaigns with known response curves, delayed conversions, and
  labelled shocks, written to their own history file for testing prediction and allocation.

Live provider writes remain disabled by default and require the documented release gates.

### Changed

- Replace LangChain, LangGraph, and Deep Agents with a small agent loop in `harness/`. One
  OpenAI-compatible adapter reaches Anthropic, OpenAI, Gemini, OpenRouter, and other providers with
  no extra packages; reasoning blocks are replayed unchanged. Conversations and changes paused for
  approval are stored in DuckDB, so an approval survives a restart and runs once. Platform tools are
  bound after `discover_tools` finds them, replacing provider tool search and the LLM selector.
  Pipeboard uses the MCP SDK directly. Adds `PAID_MEDIA_MODEL_ZERO_DATA_RETENTION` for OpenRouter;
  removes `PAID_MEDIA_TOOL_SELECTOR_MODEL` and the per-provider extras.
- Remove Managed Deep Agents, LangSmith (sandbox, gateway, deploy wizard), and Postgres. `serve`
  runs the API and Slack Socket Mode in one process; Docker runs one container.
- Clarify the README's capabilities, setup, deployment, and company customization guidance.
- Rebrand as Paid Media Agent by StructureML: StructureML mark, console palette and IBM Plex
  fonts, and warm report defaults with a distinct decrease color. Self-hosted PDFs install IBM Plex.
