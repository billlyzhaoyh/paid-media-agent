# Live data contracts

History, anomaly checks, the budget bandit, and reports are only as good as the numbers a sync
stores. A **read contract** (`tools/contracts.py`) says, for one platform, which provider tools give
daily campaign performance and campaign settings, which arguments to send, where the rows are in
the response, and what unit money arrives in. Sync, reports, the read dispatcher, and
`doctor --live` all go through it; nothing else assumes a tool name or a payload shape.

## Choosing a contract

For each platform, the first contract whose performance tool is an authorized read in the current
catalog is used:

| Platform | Contract | Tools | Verified |
|---|---|---|---|
| Meta | `pipeboard_meta` | `get_insights` (`level=campaign`, one day per call), `get_campaigns` (paged) | From Pipeboard's published server source (`pipeboard-co/meta-ads-mcp` @ `093062c6`); not yet seen live |
| Google Ads | `pipeboard_google_gaql` | `execute_google_ads_gaql_query` with fixed GAQL | Tool and arguments from Pipeboard's CLI; response shape unverified, so rows are accepted nested or dotted, camelCase or snake_case |
| Any | `rows` (host) | `get_campaign_performance`, `list_campaigns` | Produced by this repository: the fixture catalog and the direct X and OpenAI adapters |

Reddit, TikTok, LinkedIn, and the rest have no live contract yet. Their sync reports "no read
contract matches", and their tools are still available to the agent for questions.

`sync` prints which contract each live account used, for example
`pipeboard_meta (published_source)`, and its `--json` summary lists them under `contracts`.

## What each contract handles

- **Meta.**
  - *Daily rows.* `get_insights` has no `time_increment`, so a row covers the whole requested
    range. Daily history takes one call per day, and a sync re-pulls only the 8-day maturity
    window. `backfill` fills older days once. If the live schema gains `time_increment`, the
    contract switches to one ranged call.
  - *Multi-day totals.* A total the agent asks for (`date_start` ≠ `date_stop`) is kept as a
    provider result and never enters daily history.
  - *Conversions.* Conversions and their value are summed from `actions` / `action_values` for the
    account's `conversion_action` (`config/accounts.toml`). Meta omits an action type with
    nothing to report, so absent means zero. Without a conversion action, conversions are recorded
    as **missing, not zero**, and the read lists the action types it saw.
  - *Budgets.* Budgets arrive as minor-unit strings (cents for USD, yen for JPY) and are stored in
    account currency. Campaigns that budget lifetime or at the ad-set level have no daily budget,
    so the bandit leaves them out.
- **Google Ads.**
  - *Performance.* One GAQL query per window returns `segments.date` rows with `cost_micros`,
    `conversions` (the account's primary conversions), and `conversions_value`.
  - *Settings.* A second query reads settings: `campaign_budget.amount_micros`, status, the
    bidding strategy, and target CPA and ROAS.
  - *Shared budgets.* A shared budget is recorded as `shared` with no daily budget, because it
    cannot be split per campaign.
- **All contracts.**
  - Pages are followed, up to 20 per call.
  - Each provider call counts against `PAID_MEDIA_SYNC_MAX_CALLS` (200), because hosted MCP plans
    meter calls. A sync that reaches it stops and names what it skipped.
  - Each account ends on its own yesterday, in its timezone.

## Signals: what limits spend

A contract may name a signals call, made after performance and settings in its own call, so a
field the account can't report never costs the performance read. It records:
- daily impression share and the share lost to budget or to rank, in `entity_daily_signals`;
- the latest status, in `entity_delivery_status`: channel type, status reasons, bid-strategy
  status, and recommended budget.

**Per platform:**
- **Google:** one GAQL query. It is unverified like the rest of the contract; `doctor --live`
  checks it per account.
- **Meta:** no signals call. It reports no impression share, and learning stage lives on ad sets.
- **Fixtures:** a `get_campaign_signals` tool.

The budget bandit uses these signals to tell budget-limited campaigns from demand- or
target-limited ones ([Budget bandit](budget-bandit.md#what-limits-spend)).

## Pipeboard responses

Pipeboard's Meta tools return `json.dumps(graph_response)`, which arrives as structured content
`{"result": "<json>"}`. The provider parses that string.

Provider failures come back **inside the payload** with `isError` false, as
`{"error": {"message", "details": {"error": {"code"}}}}`. The provider raises them as errors, so a
failed read or mutation is never mistaken for data or for an acknowledgement.

Graph rate-limit codes (4, 17, 32, 613, 80000–80014) and HTTP 429 are retried for reads after 2,
4, and 8 seconds. Writes are never retried.

## Reviewed reads

Pipeboard's published Meta server sends no `readOnlyHint`. The catalog denies unannotated tools by
default. The tools a contract needs are listed in `reviewed_reads`:
- `get_insights`, `get_campaigns`;
- `execute_google_ads_gaql_query`, since GAQL is search-only.

Each was reviewed against the server's source and only issues provider GET or search requests. The
catalog admits them as reads with the reason `reviewed_read`. A tool that does send a hint is
classified by its hint, and every other unannotated tool stays denied.

## Money units

`domain/money.py` converts between account currency and a provider's `minor` units (ISO 4217
exponents) or `micros`.
- **Contracts** convert settings budgets on the way in.
- **Writes** convert through a write-policy row's `provider_units`, such as
  `{ daily_budget = "minor" }`. Proposals, change events, cards, and the bandit stay in account
  currency. The canonical arguments, which the approval digest covers, hold provider units, and
  readback converts back before comparing.

`doctor --live` compares each campaign's daily budget with its daily spend. A median ratio
outside 0.2–10× fails with "the budget unit is probably wrong".

## `doctor --live`

`paid-media-agent doctor --live [--alias …]` is the acceptance check for a new connection. For
each account it checks, in order:

1. the catalog loaded;
2. which contract matched, and how far that contract is verified;
3. the contract's tools are authorized reads with the arguments it sends;
4. the account id has the platform's shape (`act_…` for Meta, ten digits for Google);
5. a read of the last three days gives normalized rows, with conversions present;
6. budgets are plausible against spend;
7. the provider calls a daily sync will make.

Reads made by the check land in history labelled `doctor`.

## Not verified yet

These need one live run (roadmap S12):
- Google and Reddit response shapes;
- whether Pipeboard's hosted Meta server matches the published source, including the
  `bulk_get_insights` tool the hosted server lists;
- Meta's own currency offsets for zero-decimal currencies;
- a write readback tool for Meta. `get_campaign_details` has no account argument, so it cannot be
  scoped.

When a live response differs, update the contract and replace the matching file in
`tests/fixtures/pipeboard/` with a sanitized real response.
