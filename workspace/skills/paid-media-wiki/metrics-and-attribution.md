# Metrics and attribution

## Delivery metrics

- `CTR = clicks / impressions`
- `CPC = spend / clicks`
- `CPM = spend / impressions * 1000`
- `CVR = conversions / clicks` when clicks are the stated denominator
- `CPA = spend / conversions`
- `ROAS = conversion_value / spend`

Code handles zero denominators, decimal precision, units, and currency. A missing numerator or
denominator yields unavailable, not zero or infinity.

## Explaining a change

CPA, conversions and ROAS move for four kinds of reason, and they call for different actions:

- **Spend moved between campaigns** (mix): the account's CPA changes even when no campaign's own
  rates changed. The action, if any, is about allocation.
- **A campaign's funnel rates changed**: CPA per campaign is `CPM / (1000 * CTR * CVR)`, so a
  cost rise shows in CPM (auctions, audience, season), an ad problem in CTR, and a site, offer,
  or tracking problem in conversion rate. ROAS adds value per conversion (order value).
- **Diminishing returns**: a campaign that spends more converts the extra spend at a higher cost.
  That is expected from its response curve, not a new problem.
- **Campaigns starting or stopping.**

`explain_change` splits the change exactly (log-mean Divisia index: the parts add up to the
headline) and marks each part that is within Poisson noise for its conversion, click, or
impression counts. A small account's week-over-week change is often all noise; say so rather
than explain noise. When the headline is within noise but one campaign's rate moved by more than
noise, that campaign is the finding.

## Windows and completeness

Every metric carries an inclusive start date, inclusive end date, timezone, and completeness flag.
Comparison periods use equivalent day counts and maturation where possible. An in-progress period is
not compared to a complete period without an explicit caveat or aligned cutoff.

Relative phrases resolve deterministically: a "week" is Monday to Sunday and "last week" is the most
recent complete one; "last N days" counts back from the latest complete date a platform reports,
so two platforms with different completeness dates can have different windows, which the answer
must say; "this month" and "last month" are calendar months. The requested window stays in the
answer even when data ends inside it; the missing days are named, never silently dropped.

## Platform attribution

Platforms may differ in attribution window, click/view inclusion, modeled conversions, identity,
timezone, and late-arriving updates. Do not add their conversion counts as if they were deduplicated
people or incremental outcomes.

## Business outcomes

Warehouse or CRM data may own qualified leads, opportunities, pipeline, orders, revenue, retention,
and margin. Join it only with a documented key and grain. Report platform-attributed and business
outcomes separately before interpreting their relationship.

## Incrementality

Attribution assigns credit under a rule. Incrementality estimates what would not have happened without
the spend. The agent may recommend a holdout, geo test, conversion lift study, or other experiment when
causal confidence matters. It must not label attributed performance as incremental without evidence.

## Reconciliation

Before displaying a total:

- rows reconcile to the source aggregate;
- entity keys include parent context where names can repeat;
- recommendation amounts do not exceed the entity's eligible spend or budget;
- currency and units match;
- all included sources cover the same window and grain;
- suppressed or unavailable rows are counted and explained.

