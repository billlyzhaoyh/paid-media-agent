# Budget bandit

`bandit/` splits a total daily budget across one account's campaigns to maximise conversions. It
follows Lyft's Contextual Budgeting System (CBS): Han & Gabor, *Contextual Bandits for
Advertising Budget Allocation* (AdKDD 2020), and Han & Arndt, *Budget Allocation as a Multi-Agent
System of Contextual & Continuous Bandits* (KDD 2021). It recommends budgets for configured
accounts (`allocate`, the `allocate` job, and the agent's `recommend_budgets` tool) and runs on
simulated accounts for evaluation (`paid-media-agent bandit`). It never changes a budget on its
own: a recommendation becomes a change only as a proposal that an approver approves.

## One decision

`recommend()` in `bandit/recommend.py` makes one decision. It follows the steps of CBS's
Algorithm 1.

1. **History** (`arms.py`). It loads each campaign's days as they stood on the decision day
   (`analytics/panel.py`, the same reader the anomaly checks use), along with the budget, status,
   pacing ratio (median spend ÷ budget over 14 days), and the date of the last budget change. A
   change this agent applied and read back after the last settings observation counts at once,
   from the change log, so a new decision holds it before the next sync sees it.
   Conversions are counted as follows:
   - matured days as reported;
   - recent days divided by the share the account's lag curve says has arrived (the paper's
     surrogate reward);
   - days with under 75% arrived are left out.

   A campaign is allocated only if it is enabled, has a daily budget, spent in the account's
   last three complete days, and has three settled days.
2. **Data checks.** The run stops trusting today's data when any of these fails:
   - no complete day was reported in the last four days (the sync stopped);
   - under half of the campaigns that were on and reporting the week before reported in the
     account's last three complete days (a pipeline dropping campaigns; one campaign without
     delivery is normal, as platforms omit rows with no impressions);
   - total spend on the newest settled day is outside 0.2 to 5 times its recent median.

   It then reuses each campaign's last good curve from `bandit_decisions` and records
   `fallback_used` (CBS §6).
3. **Global model** (`prior.py`). One model learns log(conversions + 1) from every campaign's days,
   so a campaign with little budget variation borrows what the others show. It predicts 512
   pseudo-samples per campaign, at spends across its recent range (at least ±25% of its median),
   averaged over weekdays. The samples sit on a 64-point grid with weight 8 each, which keeps
   TabPFN requests small.
   - **By default** the model is a pooled regression: a level per campaign, weekday effects, and
     one elasticity shared by all campaigns, with a 28-day half-life on older days (CBS §6.1).
   - **With `PAID_MEDIA_PREDICTOR=tabpfn`** (or `--predictor tabpfn`), TabPFN predicts the mean
     from the campaign's log cost per conversion, the weekday, and spend in cost-per-conversion
     units (see the results below for why).

   A campaign whose global-model curve is not a valid one (elasticity outside 0 to 1) gets no
   pseudo-samples and relies on its own history; the run notes it.
4. **Local model** (`posterior.py`). Each campaign's curve is fitted by normal-inverse-gamma
   Bayesian linear regression, with centred weekday terms, on its history plus the
   pseudo-samples:

   ```
   log(y + 1) = kappa1 + kappa2 * log(x / u + 1)
   ```

   When the fit is not a valid curve, the prior precision on kappa2 rises through 0, 1, 4, 16,
   64, 256 toward 0.5 until kappa1 ≥ 0 and kappa2 ± 1 sd lies inside (0, 1) (CBS §6.2).
5. **Policy** (`policy.py`). **Thompson sampling** draws one (kappa1, kappa2) per campaign. A draw
   is rejected if it falls outside the valid region or outside the middle quartiles of either
   parameter (the paper's production guardrails). **Greedy** uses the posterior mean.
6. **Allocation** (`allocate.py`). Water-filling finds the budgets that give every campaign the
   same marginal conversions per unit of budget, by bisection on that price, within each
   campaign's bounds:
   - at most 25% up or down;
   - at most 1.5 times the highest spend seen, divided by pacing;
   - at least 1;
   - no change within seven days of the last one, whose effect has not been measured yet.

   An optional `max_cpia` stops a campaign where its next conversion would cost more (CBS's
   profitability constraint). The total defaults to the current total of the campaigns being
   allocated.

Every run writes one `bandit_runs` row and one `bandit_decisions` row per campaign. A decision row
holds:
- the bounds and which bound bound it;
- the posterior;
- the Thompson draw and the rejected-draw count;
- the Thompson, greedy, and final budgets;
- the expected conversions;
- a propensity: the share of 200 redrawn allocations within 5% of the chosen budget, for
  off-policy evaluation later.

## On real accounts

`bandit/live.py` runs one decision per account alias. Every run is recorded (`mode = recommend`).

| Entry | What it does |
| --- | --- |
| `paid-media-agent allocate [--alias A] [--total N] [--propose]` | Prints current → recommended budgets with a reading per campaign; `--propose` creates proposals |
| The `allocate` job | Mondays when listed in `PAID_MEDIA_JOBS`, every account; proposes only with `PAID_MEDIA_BANDIT_PROPOSE=true` |
| `recommend_budgets` (agent tool) | Read-only recommendation for one account; the agent applies one only when asked, through `propose_change` |

**Goals** (`analytics/goals.py`). Each account decides on its own local date, under the goal in
force that day.
- **Target CPA.** A target CPA is an *average*. After the split, the expected account CPA (mean
  curves, over campaigns with a curve) is compared with the target. If it is higher, the free
  budget is bisected down to the largest total whose expected CPA meets it. Because the curves
  are concave, the expected CPA rises with the total.
  - Lowered campaigns carry `target_cpa` in `constrained_by`.
  - The run records `capped_by_target_cpa` and whether the target was reachable within the step
    limits (`target_cpa_reached`).
  - This is deliberately not CBS's CPIA cap: capping the *marginal* cost at an average target
    would keep the average near half the target and leave budget unspent. `max_cpia` stays a
    separate, explicit option.
- **Monthly budget.** Without an explicit total, the total is today's budgets × `budget_scale`
  from pacing: the daily spend that lands on the monthly budget divided by the projected daily
  spend. The step limits still apply, and the run records `total_source`.

**Proposals** (`bandit/proposals.py`). A campaign whose recommended budget moves at least
`PAID_MEDIA_BANDIT_MIN_CHANGE` (5%, and at least one currency unit) becomes a proposal:
- it uses the platform's admitted daily-budget operation, with the budget rounded to cents;
- the reason names the run, the expected conversions before and after, the elasticity, and any
  bound that applied;
- it is proposed in the host thread `host:bandit:<run_id>` by requester `bandit`;
- the decision row records its proposal id.

A newer run rejects the older run's proposals that still await a decision for the same
campaigns. Approvers find host proposals with `paid-media-agent proposals list` (or
`GET /proposals`) and approve them with `proposals approve <id>` or
`POST /proposals/{id}/approve`. Approval executes the change directly, with every executor check
([Writes and approvals](writes-and-approvals.md#host-proposals)).

**Outcomes.** The `bandit_outcomes` view (`history --view outcomes`, or `query_history` with
`view: outcomes`) joins each decision to its hold window. It gives the budget in force at the end
of the window, spend, conversions from matured days only, the expected conversions, and the
proposal's latest status. Each decision is labelled:
- `followed`: the recommended budget was in force;
- `overridden`: the proposal was rejected, or a different budget was in force;
- `superseded`: a newer decision came within the window;
- `pending`: the window has not matured.

Off-policy evaluation later uses followed decisions only.

## The spend unit

The curve measures spend `x` in units of the campaign's trailing cost per conversion, `u`. At the
campaign's usual spend, `x / u ≈ y`, so the fit gives kappa1 = (1 − kappa2)·log(y + 1). The
curve therefore passes through zero conversions at zero spend (kappa1 ≥ 0) exactly when it has
diminishing returns (kappa2 ≤ 1), and the fitted kappa2 is the campaign's elasticity of
conversions to spend.

Other units break one of CBS's constraints:
- **Currency:** realistic costs per conversion make kappa1 negative.
- **The campaign's median spend:** puts `x / u` near 1. There the `+ 1` halves the slope of
  log(x / u + 1), so the fitted kappa2 doubles to above 1 and the curve reads as convex.

## Choices measured in simulation

The closed-loop evaluation below chose these settings. Each change was measured on 8 seeds.

| Setting | Tried | Effect |
| --- | --- | --- |
| Pseudo-samples per campaign | 32, 128, 512 | Mean regret (before the noise fix below): Thompson 36, 32, 28; greedy 39, 29, 25; CPA rule 32. With few pseudo-samples, noisy history swings each campaign's elasticity; CBS tuned 128 to 512. |
| Noise estimate counts pseudo-samples | yes, no | Counting them shrank the noise variance about 6×; 80% intervals covered 30 to 37% of days. Counting real days only brings coverage to 77 to 81%. |
| Lognormal mean correction | off, on | Regret unchanged (27.9, 26.9). It fixes the level: log(y + 1) back-transforms to the median day, about 0.5 conversions a day low. |
| Local recency weighting | 28-day half-life, none | No clear difference in a prototype on 4 accounts; CBS weights the global model, so only the global model does. |

## Evaluation

`bandit/evaluate.py` scores the bandit on simulated accounts, where the true curves are known
([History and simulation](history-and-simulation.md)).

**Closed-loop regret.** Every policy runs the same account. First comes a six-week warm-up on an
operator's budget schedule, then the policy sets budgets every seven days for 60 days. The store,
views, and bounds are the ones a live run uses. Campaign-days share their noise across policies.
A policy's score is the expected conversions its realised spend buys under the true curves.
Regret is the oracle's score minus the policy's; the oracle knows the true curves and faces the
same bounds.

The baselines:
- **Static** keeps the warm-up's final budgets.
- **CPA rule** is a common manual heuristic. Weekly, it raises by 20% the campaigns whose last two
  weeks' cost per conversion is more than 10% below the account's, cuts by 20% those more than
  10% above, and rescales to the same total.

**Payout error** is CBS's Tables 1 and 2. At weekly cutoffs, each model predicts the next two
weeks' daily conversions at the spend that actually happened. Errors are reported overall and for
cold-start campaigns (under seven settled days). Coverage is the share of days inside each
model's central 80% interval. The models:
- **local** is the history-only Bayesian regression;
- **global** is the pooled model or TabPFN alone;
- **cbs** combines them.

### Results

**Pooled global model, 8 seeds** (`paid-media-agent bandit evaluate --seeds 8`): 5 campaigns, a
42-day warm-up, then 60 days under the policy. Regret is in expected conversions over the 60
days; the oracle earns 1,700 to 3,600.

| Policy | Mean regret | SD | Per seed |
| --- | --- | --- | --- |
| Static | 169.5 | 98.1 | 106.0, 227.4, 396.6, 191.8, 119.5, 138.9, 107.8, 68.1 |
| CPA rule | 32.3 | 9.7 | 16.3, 17.5, 36.2, 46.6, 36.0, 37.8, 35.8, 32.7 |
| Greedy | 22.9 | 16.1 | 12.7, 1.0, 7.9, 22.7, 38.9, 54.3, 26.1, 19.7 |
| Thompson sampling | 26.7 | 18.6 | 10.5, 2.9, 8.9, 14.6, 35.8, 56.8, 38.8, 45.6 |

- **Against static budgets:** Thompson sampling cuts regret by 84%.
- **Against the CPA rule:** it has the lower regret on average, but wins on only 5 of 8 seeds.
- **Against greedy:** over 60 days, greedy does slightly better. Exploration pays back over
  longer horizons than this, and the guardrails keep it small.
- **Constraints:** no decision broke a bound, a step limit, a hold, or the total.
- **Contraction:** between the first and last decisions, the mean absolute error of the posterior
  elasticity falls from 0.22 to 0.10, and its sd falls from 0.15 to 0.08.

Payout error over the same 8 accounts, in conversions per day (3,248 campaign-days; 126 cold-start):

| Model | Group | Bias | MAE | RMSE | 80% coverage |
| --- | --- | --- | --- | --- | --- |
| Local | all | 0.10 | 2.34 | 3.31 | 0.79 |
| Local | cold start | 0.45 | 2.15 | 2.82 | 0.75 |
| Global (pooled) | all | −0.35 | 2.36 | 3.45 | − |
| Global (pooled) | cold start | −0.07 | 2.26 | 3.05 | − |
| CBS | all | 0.14 | 2.34 | 3.31 | 0.79 |
| CBS | cold start | 0.43 | 2.18 | 2.91 | 0.70 |

Poisson noise dominates the error: a campaign at six conversions a day varies by about 2.4 from
day to day. The three models forecast about equally well. The combined model's value is in the
curve's shape away from recent spend, which these forecasts at actual spend barely test. The
intervals are close to calibrated overall and somewhat narrow for cold starts.

**TabPFN as the global model, 3 seeds** (`--predictor tabpfn`). CBS's global model is a Random
Forest over about 10,000 campaigns. It is described by context (ad copy, bonus, region,
audience, weekday) and daily spend, with recency handled by sample weights rather than a time
feature, and it predicts the mean. Our first TabPFN setup differed in two ways that mattered:
- **It predicted the median.** Conversions are small counts, so log(y + 1) takes a few discrete
  values, and their median is a step function of spend: flat in most places and steep at the
  steps.
- **Spend was raw.** Log spend was unnormalised, alongside a numeric campaign code and a day
  index.

A diagnostic on 30 campaign-cutoffs measured each feature set's implied elasticity against the
true curve over the same spends (true mean 0.69):

| Global model | Mean elasticity | Mean abs. error |
| --- | --- | --- |
| Pooled regression (default) | 0.67 | 0.11 |
| TabPFN, first setup (median; code, platform, weekday, day index, log spend) | 0.54 | 0.32 |
| … without the day index | 0.50 | 0.33 |
| … with the campaign one-hot encoded | 0.50 | 0.31 |
| … predicting the mean | 0.58 | 0.29 |
| TabPFN, current (mean; log cost per conversion, weekday, spend in cost-per-conversion units) | 0.72 | 0.18 |

The day index and the campaign encoding were not the problem. The median and the spend scale
were. Closed-loop regret with the current setup (270,000 tokens):

| Global model | Thompson regret, seeds 1–3 | Mean |
| --- | --- | --- |
| TabPFN, first setup | 135.1, 6.9, 41.1 | 61.0 |
| TabPFN, current | 19.9, 28.2, 28.4 | 25.5 |
| Pooled regression | 10.5, 2.9, 8.9 | 7.4 |
| (CPA rule) | 16.3, 17.5, 36.2 | 23.3 |

The pooled regression still wins here, and the simulator favours it for three reasons:
- the true curves have exactly the pooled model's shape;
- the campaigns' elasticities are similar (0.55 to 0.9);
- each decision has 5 campaigns and about 450 days to learn from, not thousands of campaigns with
  descriptive context.

With so little data, a shape-free model's slope over a campaign's ±25% spend range stays noisy.
TabPFN may earn its place on real accounts, whose curves need not be power laws; that needs real
data to judge. Until then the pooled model is the default.

## Limitations

- **Evaluated in simulation only.** The simulated curves have exactly the shape the local model
  assumes, and real campaigns will fit it less well. `bandit_outcomes` measures how real
  recommendations turn out, but only as they are followed and mature.
- **Overconfident posterior.** With 512 pseudo-samples, the posterior is narrower than the real
  error when the global model is wrong about a campaign (CBS notes the same skew). Campaigns cut
  to a fraction of their budget show it most. In `bandit simulate --seed 3`, two campaigns cut by
  about 90% end at elasticity 0.84 ± 0.07 and 0.91 ± 0.05, against true 0.58 and 0.755. The run
  still came within 9 conversions of the oracle over 60 days.
- **One account at a time.** Budgets are split within one account and currency. Shared and
  lifetime budgets are not allocated.
- **Target CPA uses mean curves.** The cap judges the expected CPA with each campaign's mean
  curve, so a Thompson draw can land a little above or below it. A campaign without a curve is
  left out of the expected CPA. `max_cpia` (marginal) remains configuration-only.
- **Fallback curves skip the mean correction.** A reused (fallback) curve lacks it, because only
  the curve parameters are stored.
