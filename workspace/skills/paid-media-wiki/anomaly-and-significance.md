# Anomalies and significance

Day-over-day swings are common in paid media. Most are not anomalies: weekends, month starts,
budget resets, learning phases after a change, delayed conversion attribution, and platform data
still filling in.

## Checking for anomalies

`check_anomalies` checks recent campaign-days in stored history. For each day it predicts the
expected spend (given the budget in force, weekday, and recent level) and conversions (given that
day's spend) and flags values outside the expected range. Each flag carries the observed value,
the expected value and range, a direction, and the method:

- `local_band95`: the expected range learned from the account's own history, on this machine.
- `tabpfn_band95`: the same check with the TabPFN model, when the operator enabled it.
- `dod_rule_fallback`: no model was available; the day moved by at least half versus the prior
  day. Treat these as weaker signals and say which method produced them.

Budget changes already explain spend steps, and spend explains conversion moves, so a flag is a
change the configuration does not account for. Recent conversions are checked only once most have
arrived (the notes say which days were not checked yet). A flag is still a prompt to look, not a
finding. Check, in order:

1. Did configuration change? Budget, status, or bid changes on or just before the day explain most
   spend steps. `query_history` with `view = changes` lists them, including changes made outside
   this agent.
2. Small numbers. A move from 2 conversions to 4 is not a trend. The check does not flag counts
   that are within a couple of the expectation, and low-volume campaigns can hide real problems.
3. Is it one campaign or the whole account? A tracking break usually hits every campaign in an
   account on the same day.
4. Is the day still filling in? The window and notes say how complete it is.

`summarize_window` reports day-over-day changes but does not judge them.

## Significance without a statistics engine

Deterministic tools report exact values; they do not run significance tests. Be explicit:

- Call a change a trend only when it persists across several days or two full windows.
- Say "within normal day-to-day variation" when a swing is inside the range seen across the window.
- Quote the size of the base: "conversions rose from 7.6 to 13.9" is more honest than "+83%".
- Do not extrapolate a partial week to a full week.

## What to recommend

Investigate before acting. A single flagged day rarely justifies a budget or status change. When a
change is warranted, propose it with the reversal plan and a measurement window long enough to see
the effect through a full weekly cycle.
