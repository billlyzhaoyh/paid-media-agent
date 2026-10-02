# Runtime profiles

## Shared assembly

`build_agent_components` is the center. It receives typed settings, a runtime profile, and an
authorized tool catalog. It returns the model, tools, dispatcher, approval gate, and system prompt.
It performs no network calls and stores no process-global mutable state. `build_agent` binds them
to the loop in `harness/loop.py` over the profile's state store.

## Agent loop

`harness/` is a small loop the project owns. `Agent.send` appends the user message, calls the model,
runs its tool calls through the dispatcher, and repeats until the model answers without tools or the
per-run model-call budget is spent. Transient model errors retry with backoff; a failed call ends the
run with a readable message. Every tool call gets exactly one result before the next model call,
including calls left unanswered by a crash. An approval-gated call is stored as pending and the run
stops; `Agent.resume` approves or rejects it and continues. Models are reached through one
OpenAI-compatible adapter in `harness/models.py`; provider reasoning blocks are stored with the
message and replayed unchanged.

## One configured profile

`runtime/configured.py::configured_profile` resolves the catalog, reviewed write policy, and
approval policy from `PAID_MEDIA_APPROVER_IDS`. `PAID_MEDIA_DATA_MODE=sample` uses only fixtures;
`live` requires connected providers and excludes synthetic platforms. The default `auto` mode
selects providers from configured credentials and otherwise uses fixtures. Two callers compile it:

- `runtime/self_hosted.py::build_self_hosted_runtime` runs the components behind the FastAPI
  boundary and the Slack adapter. Conversations, paused calls, proposals, claims, receipts, dedupe
  keys, thread ownership, and [history](history-and-simulation.md) live in the DuckDB file at
  `PAID_MEDIA_STATE_PATH`. `sync` and `backfill` open the same file when `serve` is not running.
- `runtime/local.py::build_configured_runtime` runs them with an in-memory database for
  `paid-media-agent ask` and `report`.

`build_local_runtime` is the fixture-only variant that takes an injected model: the demo and the
test suite.

## State

`store/` owns process-local state in DuckDB: `Store` holds one connection, gives each worker thread
its own cursor, and serializes writes behind one lock. Repositories in `store/operational.py`
implement the protocols in `persistence/interfaces.py`. A proposal update is a compare-and-swap on
the SHA-256 of the expected record; a claim is consumed by an `UPDATE … WHERE used_at IS NULL`.
Migrations are numbered in `store/migrations.py` and never edited after they ship.

DuckDB lets one process hold a database file for writing, and no other process can open it while
it does. `serve` therefore runs the API and the Slack adapter together, and another process gets
`StoreBusy`. Tests, the demo, `ask`, and `report` use in-memory databases. `store.transaction()`
groups several statements; bulk inserts bind a whole batch as one JSON parameter (`json_rows`).

## What the model can read

The repository is the filesystem. `/skills` is a relative link to `workspace/skills/`, the runtime
skills and wiki. Writes are allowed under `/workspace` except `workspace/skills`, which stays
read-only. Coding-agent files in `.agents/skills/`, raw sources in `workspace/sources/`, the state
file in `workspace/state/`, and secret paths are denied. Business context belongs in
`workspace/skills/company-context/`. See [customization](../customization.md). Provider reads and
report generation use host tools.

## Parity contract

For the same model, catalog, thread state, and user request, the server and the local CLI expose
the same authorized capabilities and terminal domain objects. Timing and presentation may differ.
Capability and approval policy may not.

## Runtime components

- `runtime/profiles.py` defines `RuntimeProfile`: artifacts, accounts, catalog provider, read and
  write providers, write policy, approval policy, signer, state store, and run mode.
- `runtime/catalog.py::load_catalog` applies the admitted names from the write-policy file to the
  local policy before the live catalog is classified, so the catalog, the policy, and the gate
  agree on one reviewed set. With a live catalog the profile uses `PipeboardReadProvider` and the
  gated `PipeboardWriteProvider` and marks the provider as not fake; the fixture fake is used only
  with the fixture catalog, so a production receipt can never come from a fake.
- `harness/files.py` roots the file tools at the checkout, reads skills from `/skills/`, and denies
  `.env`, `.venv`, `.git`, `workspace/state/`, and any write outside `/workspace/`.
- No generic MCP connector binds provider tools to the model directly, which would bypass the
  authorized catalog. Tools always enter through the assembly.
