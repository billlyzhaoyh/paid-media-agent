# Bidding and budget

## Pacing

Pacing is average daily spend over a window divided by the daily budget. `summarize_window` computes
it per campaign when the `list_campaigns` artifact is supplied.

- Platforms may overspend a daily budget on strong days and underspend on weak ones; most balance
  over a calendar month. Pacing above 1.0 for two days is normal; pacing above 1.0 for a month is
  a budget that is effectively larger than configured.
- Pacing well below 1.0 means the budget is not the constraint. Raising it will not add delivery;
  look at bids, audience size, approval status, or creative fatigue.
- A paused campaign with residual spend on the pause day is not an anomaly.

## Changing budgets

- Prefer steps of roughly 20 to 30 percent. Large jumps reset learning on auction platforms and make
  before-and-after comparison unreadable.
- Give a change a full weekly cycle before judging it, longer when conversions are sparse.
- Increase budget where the marginal result is still efficient, which usually means the campaign is
  pacing at or above budget with stable or improving efficiency. Cut where spend rises and
  efficiency falls across two windows, not one day.
- Every budget change is a proposal with a before value, an after value, a reason, and a reversal.

## Splitting a budget across campaigns

`recommend_budgets` answers "how should I split the budget" for one account. It fits each
campaign's spend response from stored history, holds the account total, and returns the current
and recommended budget for every campaign with a code-written reading, the expected conversions
now and as recommended, and the limit that stopped a move. Read the readings back; do not
recompute them.

- It never moves a campaign more than 25% at once, never above 1.5 times the most it has spent,
  and holds a campaign whose budget changed in the last 7 days while that change is measured.
- Expected conversions are the model's estimate from a few weeks of history. Say so, and say that
  the gain is small when it is small.
- If its data checks failed, it used the last good curves; say that the data looked incomplete.
- It changes nothing. Apply a recommendation only when the user asks, one campaign at a time,
  through the writes skill, with the run id in the reason. Recommendations the operator asked the
  host to propose already wait for approval; do not duplicate them.
- `query_history` with `view: outcomes` shows whether past recommendations were followed and how
  many conversions the week after each one brought against what was expected.

## Bidding strategies, in general terms

- Automated strategies (target CPA, target ROAS, maximize conversions) need conversion volume and a
  learning period; changing targets often restarts learning.
- Manual or capped bids give control but need attention; they are rarely the first lever.
- Bid changes and budget changes at the same time cannot be attributed. Change one thing at a time.

## What the tools own

Arithmetic, pacing ratios, and window coverage come from `summarize_window` and `compare_periods`.
Quote them. Do not compute a percentage or an average in prose.
