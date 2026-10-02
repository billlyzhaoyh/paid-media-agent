---
name: spend-constraints-research
description: "2026-09-29 research on what caps ad spend per platform/campaign type and the proposed \"binding constraint\" slice for the budget bandit"
metadata:
  node_type: memory
  type: project
  originSessionId: 21dd3051-34b4-476d-a502-4a650ffd5ab5
  modified: 2026-09-29T09:17:14.599Z
---

**Research (2026-09-29).** Published at https://claude.ai/artifact/CLEaGDJVVydXFUXDoMBEhB.

Four things cap a campaign's spend: budget, demand/inventory, bid target (tCPA/tROAS/cost cap), and delivery (learning, pacing windows).

Key facts:
- **Google, since 2026-08-17:** budget-limited tCPA/tROAS campaigns bid *to* the target instead of beating it.
- **Maximize-type strategies** spend their full budget by design. For them, full spend is not evidence that budget is the constraint.
- **Pacing windows:** a month on Google, Microsoft, Apple and Amazon; a week on Meta, TikTok, LinkedIn and Pinterest.
- **Literature:** spend = min(ρ·budget, D(τ)), where D is censored on budget-limited days (Nuara et al. AAAI 2018; Karande et al. WSDM 2013; autobidding theory).

**Gap in our code:** `bandit/arms.py` models spend as pacing × budget. That assumes more budget always means more spend, and the code reads no constraint signals.

**Slice S9b is done** (`c014392`, 2026-09-29). Constrained-scenario Thompson regret fell from 68.8 to 5.7; default scenarios are unchanged. The ceiling applies only to campaigns classified as demand- or target-limited; the sim's budget-bound campaigns pace at 93%, so a flat 95% threshold would have wrongly capped them. It covered:
1. Read constraint signals through the read contracts.
2. Classify each campaign's binding constraint.
3. Add a censored spend-ceiling model.
4. Add per-platform rules as data.
5. Add demand ceilings to the simulator and compare regret.

**Why:** the current allocator can send budget to campaigns that can't spend it.

**How to apply:** when planning this slice, reuse the research page's matrix and signal list. Treat Meta's edit threshold and PMax lost-IS through the API as unverified.

Related: [[roadmap-after-s7]]
