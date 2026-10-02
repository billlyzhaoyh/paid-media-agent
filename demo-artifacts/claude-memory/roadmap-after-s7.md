---
name: roadmap-after-s7
description: "Approved slice order after S7 (S8 live data contract → S9 goals/pacing → S10 Slack loop → S11 evals/cost → S12 live acceptance → S13 depth), and why"
metadata:
  node_type: memory
  type: project
  originSessionId: 21dd3051-34b4-476d-a502-4a650ffd5ab5
  modified: 2026-09-28T14:52:56.859Z
---

On 2026-09-28 the user approved the roadmap in ~/.claude/plans/compiled-dreaming-corbato.md. S0–S7 are done on branch `local-first-runtime`. S3 is deferred.

Order while there is no live access: S8 → S9 → S10 → S11. S12 runs as soon as a Pipeboard token arrives. S13 comes after S12.

**Why:**
- The user has **no live ad account or Pipeboard access yet**.
- The first user is **the user themself, as a design partner** on their own accounts, so multi-client support is deferred.
- The audits found everything has only ever been tested on fixtures, and the live read path is likely broken: hardcoded fixture tool names, `rows`-only payload normalisation, Meta budgets in cents, and unparsed Meta `actions`.

**How to apply:** S8 is done (`0afca0a`, 2026-09-28) and S9 is done (`fd18586`, 2026-09-29). S9b (what limits spend) is done too (`c014392`). On 2026-09-29 the user put S10 (Slack) on hold and chose S14 next, then S11. S14 ("What changed, and what if?": the `explain_change` LMDI decomposition and the `what_if_budgets` forecast) is done (`7dd8e9d`). S11 is committed (`e260e0e`). On 2026-09-29 a four-reviewer code review found about 37 defects; the user chose one larger slice, S15 (review fixes plus eval-driven answer quality). S15 is committed (`236ca58`), with 416 tests passing. After the user topped up OpenRouter credits, the evals were run: Haiku 4.5 passed 13/30 and Sonnet 5.5 passed 25/30. The user's `.env` now sets `PAID_MEDIA_MODEL=openrouter:anthropic/claude-sonnet-5.5`. The follow-ups (a code-written pause summary that took Sonnet's write questions from 0/4 to 4/4, and `eval run --repeat N`) are committed as `5590761`. The user considers about $12 for a full repeat-3 Sonnet eval too much; run targeted `--ids` subsets instead. Idea for the next slice: a `calculate` tool so arithmetic isn't done in prose. A second code review of `e260e0e..HEAD` was started on 2026-09-30. The fair baseline is the regrade `399bf412` (10/30). The judge defaults to OpenRouter's claude-sonnet-5.5. Prompt caching is OpenRouter-only, which the user chose. Don't start live writes before S12.

S9 decisions the user made:
- Goals live in a versioned DuckDB `account_goals` table. The agent proposes changes through the host operation `host__set_account_goals`, which goes through the normal ProposalService/WriteExecutor path.
- A target CPA caps the account's expected **average** CPA. It is not used as the marginal CPIA cap.

S8 findings to remember:
- Pipeboard's published Meta server sends **no readOnlyHint**. Contract tools are admitted via `reviewed_reads`.
- `get_insights` has **no time_increment**, so sync makes one call per day.
- Errors come back in-band with `isError` false.
- Google and Reddit shapes are unverified. The first live step is `doctor --live`.

Related: [[open-items-live-writes]]
