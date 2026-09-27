# Tools and context

## Catalog lifecycle

1. Load every tool from the eight configured Pipeboard MCP endpoints concurrently, with a
   20-second timeout per endpoint. A failed connector does not discard the others.
2. Normalize schemas and annotations into immutable catalog entries.
3. Apply local platform, mutation, delete, raw-mutate, and account policy.
4. Hash the authorized catalog revision.
5. Expose only a context-efficient selectable surface.
6. Re-resolve and validate the exact current entry immediately before invocation.

A loader retains the catalog in memory for its agent assembly; it does not refetch on each search
or model turn. Rebuild the runtime after credentials change, or explicitly refresh the loader.
Setup actions load a fresh live catalog even when the runtime is currently using sample data.
Account discovery calls independent listing tools concurrently and recognizes ad accounts, TikTok
advertisers, and GA4 properties. Fixture coverage remains Google, Meta, and Reddit.

## Tool disclosure

Core, write, and file tools are bound to every model call. Platform read tools are bound only after
`discover_tools` finds them: its catalog search activates the matching tools for the thread, at most
`PAID_MEDIA_MAX_SELECTED_TOOLS` at a time, dropping the oldest. Activation only decides which
schemas the model sees. Authorization is separate: any authorized read can be called, and nothing
outside the authorized catalog can be, whatever the model names. The same path works for every
model provider; there is no provider-specific tool search.

## Results

Tool results return typed, bounded summaries. Large rows are written under `workspace/analysis/`.
GA4 and newly connected platforms without verified spend-unit mappings keep their native payloads
as `provider_result` artifacts. They are not coerced into normalized spend comparisons.
Every offloaded artifact records:

- source platform and account alias;
- entity grain;
- requested and actual window;
- row count and schema version;
- quality flags;
- content hash;
- local path.

The model receives no credential-shaped values, raw headers, tokens, internal stack traces, or
unbounded provider payloads.

## Filesystem

The runtime may read wiki and skill files, write analysis artifacts, and render reports. It cannot
read secret files, coding-agent files, original business sources, the state file, or paths outside
the project. Runtime skills, including curated company context, are read-only. `harness/files.py`
checks each path as written and after resolving links, and search never descends into denied or
hidden directories. The system prompt lists each skill's name and description; the model reads a
skill's `SKILL.md` and linked pages only when it needs them.

## Trusted tool boundary

- `tools/catalog.py` classifies every `RawTool` with `classify()`. Denied reasons are explicit:
  `unknown_platform`, `malformed_schema`, `denied_name_policy`, `missing_mutation_metadata`,
  `destructive_hint`, `no_account_scope`, `mutation_not_admitted`, `duplicate_tool_name`.
- The revision is a hash over qualified name, schema hash, class, reason, and description. A schema
  change on one tool changes both its `schema_hash` and the catalog revision.
- `tools/reads.py` builds one model-facing tool per READ entry. The provider account argument is
  replaced by `account_alias`; the host injects the provider id and rejects any raw id in arguments.
- `harness/tools.py` is the single path every call takes. `ToolDispatcher` denies any name outside
  the assembled tools, denies direct calls to non-read catalog entries, and validates arguments
  with `jsonschema` against the schema the model saw, so a hallucinated or stale call fails closed.
  It then runs the tool, retries an idempotent read once after a provider timeout, offloads
  oversized results to artifacts, redacts secrets, and turns any exception into an error result.
- Live Pipeboard loading (`tools/pipeboard.py`) uses the MCP SDK's Streamable HTTP client with one
  short session per request. The bearer token lives only in that client's HTTP headers, never in
  state. A tool without `readOnlyHint` in its MCP annotations is treated as a mutation and denied.

## Direct adapters

Platforms outside Pipeboard live under `tools/direct/`. Each adapter publishes `RawTool` entries
with `readOnlyHint=true` and an account argument, so `build_authorized_catalog` classifies them
with the same policy as MCP tools, and a `CompositeReadProvider` routes execution by platform.
Direct adapters validate identifiers and ISO dates before building any query, bound every HTTP
call, and reduce provider errors to status codes. No direct adapter defines a mutation.
