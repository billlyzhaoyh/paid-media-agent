# Goals and economics

Paid media converts a limited budget into attention, qualified action, and business value. The agent's
job is not to maximize an isolated platform metric. It is to help allocate spend under a business goal,
measurement limits, channel constraints, and risk tolerance.

## Goal hierarchy

1. Business outcome: revenue, pipeline, profit, customers, or another explicit value.
2. Qualified conversion: the event that is credibly linked to that outcome.
3. Platform optimization event: the signal available quickly enough for bidding and learning.
4. Delivery inputs: spend, reach, impressions, clicks, audience, creative, bid, budget, and status.

The hierarchy may not align. A high-volume platform event can be weak business value. A low-volume
channel can assist later conversion. State the gap instead of optimizing the easiest number.

## Economic questions

Before recommending spend, establish:

- objective and decision horizon;
- marginal budget available or at risk;
- value and quality of the conversion;
- gross margin or allowable acquisition cost when supplied;
- sales or purchase lag;
- channel role in discovery, capture, retargeting, or expansion;
- operational constraints such as minimum budget, learning, inventory, geography, and creative supply.

Average historical ROAS or CPA does not equal the return on the next dollar. Budget changes should be
small enough to learn from, reversible, and evaluated after a suitable maturation window.

## No universal thresholds

The public agent ships no universal good CTR, CPA, ROAS, frequency, or budget threshold. A threshold is
valid only when its goal, unit, window, segment, attribution definition, and source are explicit.
Repository fixtures may include example targets; label them as examples.

## Configured goals

Each account can carry a target CPA or target ROAS and a monthly budget, set by the operator
(`paid-media-agent goals set` or the setup console) and versioned by the day they take effect.
They are the thresholds this deployment uses: `list_accounts` shows them, `against_goals` in
`compare_periods` and `summarize_window` and `check_pacing` judge against them, and
`recommend_budgets` cuts its total when the expected CPA would exceed the target. A target CPA is
an average; the cost of the next conversion is higher, so do not read a marginal cost above
target as a miss. The agent never sets a goal itself: it proposes `host__set_account_goals`, and
the change applies after approval.

