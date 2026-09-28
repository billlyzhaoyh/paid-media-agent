# Writes and approvals

The approval flow is an execution protocol, not a prompt convention.

## Contracts

- `ChangeSet` is the canonical proposal and presentation source.
- `ApprovalClaim` binds one proposal revision and digest to one requester, approver, account, tool,
  expiry, nonce, and signature.
- `WriteReceipt` reports a verified, rejected, failed, or unknown terminal state.

The review card and execution payload are derived from the same persisted `ChangeSet`. A surface
cannot submit replacement arguments during approval.

## Host checks

Before execution, trusted code verifies:

1. proposal exists and is awaiting approval;
2. revision and digest match;
3. approval is signed, fresh, unused, and for the current thread;
4. requester and approver identities satisfy policy;
5. account alias resolves to the configured provider account;
6. catalog revision and current tool schema are acceptable;
7. tool is admitted by write policy;
8. global incident kill switch permits execution.

The executor atomically moves the proposal from awaiting approval to executing. Only the worker
that saves this transition may consume the claim and call the provider. It makes one mutation
attempt and performs bounded readback. A timeout after the call creates an unknown state until
read-only reconciliation proves the result.

Proposal updates compare the complete previously read record before saving. A concurrent approval,
edit, or rejection returns `proposal_changed` instead of overwriting newer state. Repeated execution
returns the existing receipt; it never replaces a verified result with a refusal. If a process
stops during execution, inspect the provider through read-only tools before creating a new proposal.
The agent does not automatically retry an interrupted mutation.

## Rejection tests

Tests must prove denial for edited payloads, expired claims, replayed actions, foreign users, foreign
accounts, missing signatures, stale schemas, unknown tools, direct mutation calls, and runtime/profile
attempts to bypass the dispatcher.

## Proposal and execution services

- `propose_change` builds the `ChangeSet` from the current catalog entry and the reviewed
  `WritePolicy` (`WriteOperation` names the readback tool, target argument, editable fields, and
  risk). The before value comes from the authorized read provider, never from the model.
- `execute_change` is the only gated tool. The loop persists the call in `pending_tool_calls` and
  stops; other calls in the same batch still run. A pause survives a restart, and a paused call is
  taken exactly once, so two approvals cannot run it twice. Resuming grants nothing by itself: the executor loads the persisted proposal and the latest unused
  `ApprovalClaim` for that revision, verifies the HMAC signature, digest, scope, requester, and
  expiry, checks the current catalog entry and schema, then claims the proposal and consumes the
  approval exactly once. Multiple valid approval claims cannot create multiple execution attempts.
- Only `ProposalService.approve` creates claims. Surfaces call it with an opaque routing id or
  proposal id; Slack button values carry no payload.
- Readback runs through the authorized read path with bounded attempts and wall time. A timeout
  after submission is reconciled by readback: matched after-state is `verified`, matched before-state
  is `failed`, anything else is `unknown`. No mutation is ever retried.
- `WriteGate` admits fakes unconditionally and refuses live providers while
  `PAID_MEDIA_WRITES_ENABLED` is false or the live-write release gates are not met (see
  `docs/operations/live-write-runbook.md`).

## Host proposals

The budget bandit (`bandit/proposals.py`) proposes budget changes itself, outside any
conversation. They use the normal `ProposalService.propose`, with the same admitted operations,
schema checks, live before-value, digest, and risk flags. They live in threads named
`host:bandit:<run_id>` with requester `bandit`. Callers cannot use the `host:` prefix:
`AgentRunner` refuses it, so no conversation can claim or pause in that namespace.

With no paused call to resume, approving a host proposal (`POST /proposals/{id}/approve`, or
`paid-media-agent proposals approve`) creates the claim and runs `WriteExecutor.execute`
directly. The checks are the same as after a resume: claim signature, digest, scope, requester,
expiry, catalog and schema, the write gate, one mutation, and readback. Approving again returns
the stored receipt. Rejecting needs no conversation either.

`GET /proposals` (approvers only) and `paid-media-agent proposals list` show everything awaiting
a decision, because nothing else surfaces host proposals. A newer bandit run rejects the older
run's proposals that still await a decision for the same campaigns, with the note "superseded".
Slack cards are not posted for host proposals.

## Live-write notes

- The reviewed mutation set is data: `WritePolicyFile` rows in `config/write-policy.example.toml`.
  `validate_against(catalog)` keeps only admitted rows whose tool is a MUTATION entry with the
  listed target, editable, validate-only, and idempotency arguments in its current schema and whose
  readback tool is an authorized READ. Everything else becomes a `PolicyIssue` that `doctor` prints.
- The ChangeSet digest binds `schema_hash` (the mutation tool's schema at proposal time) and
  `policy_digest` (the admitting policy row). Execution rejects `stale_catalog` when the schema
  changed and `stale_policy` when the row changed, alongside the proposal digest checks.
- `classify_risk` derives reviewer facts from the operation, schema, and actual change:
  `status_flip`, `starts_delivery`, `budget_delta`, `budget_increase`, `publishes_live`,
  `access_change`, `destructive_change`, `sensitive_data_transfer`, `standing_automation`,
  `bulk_capable`, `policy_high_risk`. They are shown on cards and in the proposal view.
- `execute_change` pauses only when the proposal id exists on the current thread. Unknown ids,
  malformed ids, and proposals from other threads run straight into the executor's refusal, so a
  reviewer is never asked to approve something that cannot execute.
- When the policy row names a `validate_only_arg`, the executor calls the provider in validation
  mode first; a refusal yields `failed` with `mutation_attempted=false`. The single real attempt
  follows. `provider_acknowledged` on the receipt separates "the provider answered" from
  "readback proved it".
- Money stays in account currency everywhere a person sees it: proposals, cards, receipts, change
  events, and the bandit. A policy row's `provider_units` (`minor` or `micros`) converts the
  approved value into the provider's unit in the canonical arguments, rounded to the currency's
  smallest unit first, and converts readback values back before comparing. The proposal's
  "after" value is what the provider will hold, so a proposal of 57.555 USD reads 57.56. See
  [Live data contracts](live-data-contracts.md#money-units).
- `WriteGate` order: kill-switch file, then for live providers `writes_enabled`, pinned reviewed
  revision equal to the current one, and the canary tool allowlist. Fakes pass after the kill
  switch. The live `PipeboardWriteProvider` refuses any tool whose `readOnlyHint` is not `false`.
