"""The company-context runtime skill: prose the agent reads, created from a template once.

Numbers the code uses (target CPA or ROAS, monthly budget) live in the goals store, not here; this
file says what they mean for the business, how conversions are defined, and how campaigns are
named. It is Git-ignored and never overwritten.
"""

from __future__ import annotations

from pathlib import Path

TEMPLATE = """---
name: company-context
description: Business goals, conversion definitions, and campaign conventions. Read before analyzing this company's accounts.
---

# Company context

## Business

What we sell, who buys it, and the markets we serve.

## Measurement

Primary conversions (and, for Meta, the `conversion_action` set per account in
`config/accounts.toml`), attribution windows, reporting timezone, and currency.

## Goals

The numbers are set per account with `paid-media-agent goals set` (target CPA or ROAS, monthly
budget) and the agent reads them with `list_accounts` and `check_pacing`; they win over anything
written here. Use this section for what the goals mean: which one matters most, seasonal budget
plans, and when a target may be exceeded. Mark anything undecided as unknown.

## Conventions

Campaign naming, funnel stages, and planned launches or seasonal changes.

## Sources

Links and dates for the briefs or decisions behind these facts.
"""


def company_context_path(workspace_root: Path) -> Path:
    return workspace_root / "skills" / "company-context" / "SKILL.md"


def init_company_context(workspace_root: Path) -> tuple[Path, bool]:
    """Write the template when the skill is missing. Returns (path, created)."""
    path = company_context_path(workspace_root)
    if path.exists():
        return path, False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(TEMPLATE, encoding="utf-8")
    return path, True
