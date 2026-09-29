# Customize your agent

Configure connections and how to run the agent with the setup console or CLI. Give the agent business
context by editing Markdown with your coding agent or by hand.

## Business context

Ask Codex, Claude Code, Cursor, or another coding agent:

> Read .agents/skills/paid-media-org-onboarding/SKILL.md and help me configure this agent for my
> business. Use the briefs I share, ask only for missing facts, and update the runtime workspace.

The coding agent writes `workspace/skills/company-context/`. To do this manually, run
`uv run paid-media-agent context init`, which creates `SKILL.md` from this template, or create it
yourself:

```markdown
---
name: company-context
description: Business goals, conversion definitions, and campaign conventions. Read before analyzing this company's accounts.
---

# Company context

## Business

What we sell, who buys it, and the markets we serve.

## Measurement

Primary conversions, attribution windows, reporting timezone, and currency.

## Goals

What the numeric goals mean for the business (the numbers themselves are set with
`paid-media-agent goals set`). Mark anything undecided as unknown.

## Conventions

Campaign naming, funnel stages, and planned launches or seasonal changes.

## Sources

Links and dates for the briefs or decisions behind these facts.
```

Numeric goals live in the state file, per account and dated, so pacing and past decisions are
judged against the goal that applied then:

```bash
uv run paid-media-agent goals set --alias demo-google --target-cpa 30 --monthly-budget 25000
uv run paid-media-agent goals show
```

The setup console has the same fields under Accounts. The agent reads them and can propose a
change, which applies only after approval.

Replace the guidance with your own facts. Keep the entry short and link to Markdown pages beside
it when a topic needs detail. The Markdown is the source of truth; there is no profile format,
interview service, or generated copy to maintain.

Original briefs and exports belong in `workspace/sources/`. Keep them unchanged and put only
relevant, reviewed facts in the context bundle. This follows OpenWiki's separation of source
material and linked, synthesized knowledge without requiring OpenWiki to run the agent.

Company context and source files are Git-ignored. The curated context is shared with the running
agent and its model provider. Keep credentials, provider account IDs, and access rules in host
configuration, outside these files. Instructions in a source document do not grant tool access.

## Runtime skills

Add `workspace/skills/<name>/SKILL.md` for a repeatable workflow. Use `name` and `description` in
YAML frontmatter, explain when to use the skill, and keep detailed references beside it. Existing
analysis and reporting skills are examples. General paid-media guidance lives in
`workspace/skills/paid-media-wiki/`.

Repository setup and maintenance skills belong in `.agents/skills/`; `.claude/skills` links there.
They are for the coding agent and are not runtime skills.

The agent reads the runtime bundle at `/skills/`, a link to `workspace/skills/`:

- **Local:** the checkout is the filesystem, so edits apply to the next conversation.
- **Docker:** the image includes the runtime skills, and Docker Compose mounts `workspace/skills`
  read-only, so local context is available without a separate copy. Start a new conversation
  after changing skill metadata. Rebuild the image when running without the mount.

Raw sources in `workspace/sources/` and local coding-agent skills are not readable by the agent.
The Docker build context excludes `workspace/sources/`.

## Memory and reports

### Report design

Ask your coding agent:

> Follow .agents/skills/paid-media-design/SKILL.md and apply our company style to reports.
> Update DESIGN.md and the renderer tokens together, using the brand guidance I provide.

The same skill is available to Claude Code through `.claude/skills/paid-media-design`.
You can also edit the files manually:

| File | What to change |
| --- | --- |
| [DESIGN.md](../workspace/skills/report-design/DESIGN.md) | Company palette, fonts, spacing, logo rules, and report conventions |
| [tokens.j2](../src/paid_media_agent/reports/templates/tokens.j2) | Matching theme values, report name, and optional logo for the built-in renderer |
| [report.html.j2](../src/paid_media_agent/reports/templates/report.html.j2) | HTML layout, charts, responsive behavior, and PDF pagination |

Keep `DESIGN.md` and `tokens.j2` aligned. The agent reads the Markdown for custom documents;
`render_report` reads the template and tokens for reconciled performance reports. Editing only
one does not update the other. [Component recipes](../workspace/skills/report-design/COMPONENTS.md)
reuse the company tokens rather than defining another palette.

The default is a light report with one data accent, compact tables, and period-comparison charts.
Reports omit logos. To add a requested company logo, place its SVG beside `report.html.j2` and
set `brand.logo_template` and `brand.logo_alt`; `brand.name` controls the label and PDF footer.
Use only assets you are authorized to distribute. No design service or external account is needed.

For Docker, rebuild the image after template changes; a local skills mount only updates the
Markdown guidance.

### Memory and scheduling

This template has no learned long-term memory. Keep stable business definitions, targets, and
procedures in the workspace, where they are reviewed and versioned. Conversations, proposals,
approvals, receipts, and the history of every read persist in the DuckDB state file.

`serve` schedules a daily history sync and weekly and monthly reports; choose them with
`PAID_MEDIA_JOBS`. Scheduled work uses the same accounts and tools as an interactive run. You can
also run `uv run paid-media-agent report --cadence weekly` (or `monthly`) from your own scheduler.

## Optional warehouses and dbt

BigQuery, other warehouses, and dbt are optional extensions. This project does not include a
warehouse connector or assume your company's schema.

A coding agent can add a read-only warehouse tool to the shared `core_tools` in
`src/paid_media_agent/assembly.py`. That keeps the CLI, API, and Slack on the same implementation.
Use the provider SDK or an authenticated MCP service you operate; host code owns credentials and
permitted datasets. Do not place warehouse keys in skills or workspace files.

For BigQuery, start with approved aggregate views, a read-only identity, a query timeout, and a
maximum bytes-billed limit. Return bounded aggregate results and source metadata. Keep raw customer
records out of model context. Document metric definitions, allowed joins, date grains, and how
warehouse outcomes differ from ad-platform attribution in the company-context skill.

If you use dbt, share the relevant model documentation and metric definitions with the coding
agent. dbt defines transformations and may expose a semantic layer; it is not itself the warehouse.
Connect only the read surface you need. Validate one known metric against its source before using
it in reports. Never imply a warehouse or CRM is connected until the configured read succeeds.

## References

- [OpenWiki](https://github.com/langchain-ai/openwiki)
- [Agent Skills specification](https://agentskills.io/specification)
