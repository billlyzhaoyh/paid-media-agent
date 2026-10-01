---
name: paid-media-writes
description: Prepare, review, edit, approve, reject, or reconcile a paid-media change while preserving the human approval and provider verification boundary.
---

# Paid-media writes

Use this skill whenever a request would change campaign, budget, bid, status, targeting, creative,
conversion, audience, or another provider resource.

1. Read `/skills/paid-media-wiki/write-safety.md`.
2. Confirm the user asked for a change, not only an analysis.
3. Call `discover_write_operations`. It lists the admitted operations with their editable fields,
   units, risk, and the current execution gate. Never guess a mutation name or field.
4. Identify the target from an authorized read (for example the campaign id in a
   `<platform>__get_campaign_performance` or `<platform>__list_campaigns` result).
5. Call `propose_change` with the account alias, the admitted operation name, the target id, the
   changed fields, the reason, a measurement plan, and a reversal plan. The host reads the current
   value, derives risk flags, builds the typed proposal, and persists it. Nothing executes.
6. In your next reply, call `execute_change` with the proposal id and revision. The runtime pauses
   and shows the reviewer the proposal's summary, written from the record (before, after, risk
   flags, reason, measurement and reversal plans), with an approve or reject card, in place of any
   text you write. Do not ask for approval in prose or wait for a chat reply first; a turn that
   ends without the call is paused on the proposal anyway.
   Only the runtime approval action authorizes execution; chat text does not.
7. On edit, the host creates a new revision; earlier approvals are invalid. Re-present the new
   revision.
8. After resume, report the receipt: `verified`, `rejected`, `failed`, or `unknown`, with the
   verified state, whether the provider acknowledged the call, and the reason. Do not claim success
   from anything except a `verified` receipt. A `rejected` receipt with a gate reason
   (`writes_disabled`, `kill_switch`, `tool_not_released`) means the operator has not released the
   live path; say so plainly.
9. If the receipt is `unknown`, explain that no blind retry is safe, check state with a read tool, and
   offer a new proposal if needed.

If execution returns `proposal_changed`, reload it with `get_proposal`. Do not resubmit a proposal
that is executing or verifying. If its worker stopped, check the provider through read-only tools
before proposing another change.

To apply a `recommend_budgets` recommendation, call `propose_change` with the account alias, the
campaign as `target_ref`, and the recommendation's `run_id` as `bandit_run_id`, leaving `tool_name`
and `changes` out. The host takes the recommended budget, reason, and plans from the run; never type
the budget. A run more than 2 days old is refused: run `recommend_budgets` again.
Proposals from the budget bandit itself (requester `bandit`) are reviewed by an approver outside
the conversation; do not re-propose them.

To change an account's goals when the user asks (for example "set our target CPA to 40"), propose
`host__set_account_goals` with the account alias as `target_ref` and only the fields that change
(`target_cpa`, `target_roas`, `monthly_budget`; null clears one). It is approved and verified like
any change and takes effect from today in the account's timezone. It never touches a platform.

Never call a provider mutation directly, reveal raw ids or credentials, or suggest that a prompt can
bypass the approval policy.
