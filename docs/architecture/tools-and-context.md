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

Core, write, and file tools are bound to every model call.

**Platform read tools.** Every authorized read tool is bound too, sorted by name after the
others, while their schemas fit `PAID_MEDIA_READ_TOOLS_BUDGET_TOKENS` (default 6,000; the
sample catalog is about 2,500). The tool list is then identical on every call and in every
thread, so the prompt cache holds and no turn is spent finding tools.

**A larger catalog** is bound as `discover_tools` finds what a thread needs:
- at most `PAID_MEDIA_MAX_SELECTED_TOOLS`;
- the set only grows, and the oldest leave only past the cap;
- the bound order is by name, so the list changes only when a new tool is found.

`discover_tools` also searches the catalog by keyword when the model is unsure which tool fits.

**Authorization is separate from binding.** Binding decides only which schemas the model sees:
any authorized read can be called, and nothing outside the authorized catalog can be, whatever
the model names. The same path works for every model provider; there is no provider-specific
tool search.

**The accounts are in the system prompt**, after the calendar: each alias with its platform,
currency, timezone, today, and current goals. That saves the `list_accounts` turn most questions
began with. It changes only when a goal or an account's day changes.

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

The dispatcher also appends each performance read and campaign listing to the history tables in
the state file ([History and simulation](history-and-simulation.md)). The model reads that history
only through `query_history`'s fixed queries.

## Offloaded results

A result over `PAID_MEDIA_RESULT_OFFLOAD_CHARS` becomes an artifact and a stub.
- The stub keeps the result's top-level `headline`, `summary`, `reading`, `caveats` and notes
  whole, up to 2,000 characters, next to a 400-character preview. Tools put those fields first,
  so a partial view still shows every account.
- The core tool `read_artifact(artifact_id, offset)` pages through any artifact, 5,000
  characters at a time. It reads by id only, is never offloaded itself, and is how the model
  reads the rest.
- For a platform read's rows, `read_artifact` instead filters (`entity_ref`, `start_date`,
  `end_date`, `fields`) and totals (`group_by`: entity, day, or total) with `compute.aggregate`,
  the same arithmetic as `summarize_window`. `query_history` takes `group_by` (account, entity,
  day, week) for the daily and signals views, with CPA, ROAS, CTR and CVR from the sums, and
  `fields`. The model never adds rows up.
- The first eval run showed why: a cross-platform summary's preview showed only Google, and the
  model reported it as the total for every platform.

## Arithmetic

Tools return the figures an analysis rests on. A figure none of them returned comes from the core
tool `calculate`, never from arithmetic in the reply: a total, a difference, a percentage change, a
share, or a monthly amount.
- **What it accepts.** One call takes up to 20 labelled expressions over figures from earlier
  results, e.g. `(216 - 180) * 30` or `pct_change(26.04, 27.97)`. Each has a `display` format:
  number, percent, or money.
- **How it evaluates.** Expressions are parsed, never executed. Only number literals, `+ - * /`,
  parentheses, and `sum mean min max abs round pct_change share` are admitted, in exact decimal
  arithmetic. Everything else is refused per item with a reason: names, attributes, powers,
  thousands separators (`max(1,234)` is ambiguous), and numbers beyond 10^15.
- **Figures asked for often are returned directly**, so they need no calculation:
  - `summarize_window` entities carry `share_of_spend`;
  - the `query_history` settings view carries each account's `budget_totals`: the current
    active daily budgets added up.
- **Why.** In the stored eval runs, arithmetic in prose was the most common grounding failure:
  13 of Haiku's answers and 2 of Sonnet's, some of it wrong.

## Verdicts

Tools return the judgements an answer needs, not only the figures, because models misread them:
"ROAS down from 2.44 to 2.68", "nine days ago" for twelve, "86% lower" for 46%.
- **Better or worse.** One polarity rule (`domain/analysis.py` `verdict`): lower CPA, CPC and
  CPM are better; higher ROAS, conversions, CTR and CVR are better; spend is neither.
  - Every change in `compare_periods` carries it (`cpa_change: "+8.2% (worse)"`), as do its
    attention lines, the `explain_change` and `what_if_budgets` readings, and the goal readings.
- **Rankings.** `compare_periods` names, per platform:
  - the best and worst CPA and ROAS among campaigns with at least 5 conversions;
  - the largest CPA rise;
  - campaigns whose spend rose while ROAS fell.
- **Windows.** `compare_periods` and `summarize_window` take a `window` preset (`last_week`,
  `last_n_days_of_data`, `month_to_date`, `last_month`) and return the dates they used.
  - "Of data" ends on the newest day every read covers.
  - A comparison's previous window is the same number of days immediately before.
- **Days ago.** Anomaly flags, `explain_change` known changes, and `query_history` change and
  settings rows carry `days_ago`, counted from the account's own today.
- **Budgets.**
  - `summarize_window` counts each campaign's days above its daily budget
    (`over_budget_days`), with the highest day and the platform's own allowance (Google may
    spend 2x a budget on one day).
  - Budgets come from the read's normalised settings, in account currency, never from raw
    provider units.
  - `check_pacing` says how far active budgets are from the daily spend that lands on the
    monthly budget.
- **Comparisons.** `summarize_window` compares each pair of accounts in the same currency on
  CPA and ROAS, both ways ("46% lower (better)"; "86% higher (worse)"). It refuses across
  currencies.
- **Offloading.** A stub keeps these fields (`against_goals`, `comparisons`, the resolved
  windows, `budget_totals`, `flags`) ahead of the notes.

## Evidence the host keeps

The model does not keep the books on what it read or what it proposed; the host does.
- **Failed reads.** A platform read that is refused, errors, or times out is found in the
  thread's current turn (`harness/tools.py` `failed_reads`); a later successful read of the same
  tool and account clears it. `compare_periods` and `summarize_window` list those accounts under
  `unavailable_sources`, suppress any cross-platform total, and say the numbers are missing, not
  zero. The model passes nothing.
- **Pages.** A platform read follows the provider's pages itself, up to 20, and returns every
  page's artifact; more pages than that is an error, never a partial read. The summary tools
  merge several artifacts for one account (pages, or one call per day): rows are keyed by entity
  and day, the newest artifact wins an overlap, totals reconcile against the provider's when
  that is still exact, and a day no artifact covers is named.
- **Proposals.** A paused proposal always shows the summary written from its record
  (`proposal_summary`), on every surface, in place of the model's text. To apply a budget
  recommendation the model passes `propose_change` the run's `bandit_run_id` and the campaign:
  the budget, reason, and plans come from the stored decision, and a stale, ineligible, foreign,
  or already-proposed decision is refused.

## Answer check

Every final answer is checked with the eval's grounding rule (`paid_media_agent/grounding.py`,
also used by `evals/checks.py`): its figures must be in the thread's tool results, the payloads
those results name, `calculate` results built on sourced figures, or the user's own words.
- When it fails (any money figure missing, or under 80% of figures found), the draft is kept for
  the record and a host note naming the figures sends it back once; that one extra model call is
  logged with purpose `repair`. Surfaces show only the answer that follows.
- If the second answer still fails, it is shown with a closing line naming the unverified
  figures.
- `PAID_MEDIA_ANSWER_REPAIR=false` turns it off. A grounded answer costs nothing extra.

## Context budget and caching

Every model call sends the whole thread, read back from the state file.

- **Context budget.** `harness/context.py` estimates the tokens of system prompt, tools, and
  messages (characters / 3.5).
  - Past `PAID_MEDIA_CONTEXT_BUDGET_TOKENS` (default 60,000; 0 turns it off), it replaces the
    oldest tool results **in the view sent to the model only** with a stub:
    `{stubbed, tool, artifact_ids, summary, note}`. Artifact ids are kept, so earlier reads can
    still be compared and rendered.
  - Results since the latest user message are kept, unless that turn alone is over budget, in
    which case its newest four are kept.
  - Stored history never changes, so a restarted agent builds the same view. Every call still
    has one result, and assistant messages are untouched.
- **Prompt caching.** For `anthropic/*` models on OpenRouter, requests carry OpenRouter's
  automatic `cache_control` (`PAID_MEDIA_PROMPT_CACHE=auto`, the default).
  - The cached prefix (tools, system prompt, earlier turns) moves forward as a thread grows, so
    each step of a tool loop reads the thread so far from cache.
  - The tool list and the system prompt are the same on every call (see Tool disclosure), so
    the prefix is read from cache after a thread's first call.
  - OpenRouter routes each conversation to a provider on its own unless told otherwise, and each
    provider has its own cache. Requests therefore carry a `session_id` fixed per deployment (a
    hash of the state path; for evals, per model and day), so every thread lands on the provider
    that already holds the shared prefix.
  - The direct `anthropic:` provider goes through Anthropic's OpenAI-compatible endpoint, which
    does not cache.
- **Usage.** Every attempt is a row in `llm_calls`, via `harness/usage.py`:
  - thread, caller, purpose, provider, model, attempt, status, latency;
  - the provider's token counts, including cache reads and writes;
  - the provider's reported cost: OpenRouter reports it, others record none;
  - messages sent, the token estimate, and how many results were stubbed.

  Usage rides on the reply, never in the stored thread. `paid-media-agent usage` and
  `history --view usage` summarise it for operators; the model cannot query it.

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
