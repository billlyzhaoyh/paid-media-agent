# Working in this repository

Build a local-first paid-media agent with one shared core that runs on your machine or your own
server. Keep company-specific context, credentials, and operational history out of the public
repository.

## Read first

1. [README.md](README.md) and [Architecture](docs/architecture/README.md).
2. [Customization](docs/customization.md) for context, skills, and optional integrations.
3. `instructions.md`, the relevant runtime skill, and the owning module and tests.
4. [Operations](OPERATIONS.md) before running the server.

## Folder boundaries

- `.agents/skills/`: workflows for coding agents. `.claude/skills` links here.
- `workspace/skills/`: skills and knowledge read by the paid-media agent, under `/skills/` through
  the root `skills` link.
- `workspace/skills/company-context/`: optional, Git-ignored business context. Markdown is
  authoritative. Keep the entry short and link to detailed pages beside it.
- `workspace/sources/`: local source briefs and exports. Preserve originals; curate facts into
  company context. Raw sources are not runtime skills.
- `workspace/in/`, `analysis/`, `out/`, `logs/`, `state/`: local inputs, generated files, and the
  DuckDB state file, ignored by Git.
- `src/paid_media_agent/assembly.py`: shared model, tools, middleware, and approval policy.
- `src/paid_media_agent/tools/`: account-scoped provider calls, deterministic analysis, reports,
  and approved mutations. `runtime/` supplies profiles and provider implementations; `store/` owns
  DuckDB state. One process holds the state file, so `serve` runs the API and Slack together.
- `src/paid_media_agent/surfaces/`: API and Slack transport. Slack uses native agent
  streaming and generic approval controls. Do not add tool-specific cards.
- `src/paid_media_agent/admin/`: optional local connection and run console over CLI actions.
- `tests/`: unit tests, real-graph contracts, offline behavior checks, and opt-in integration tests.

## Onboarding

Follow [.agents/skills/paid-media-onboarding/SKILL.md](.agents/skills/paid-media-onboarding/SKILL.md).
Run the offline demo, configure a model and accounts, then use the business-context skill in this
chat. Ask for existing briefs and only the facts still missing. Write reviewed Markdown to the
runtime workspace. Users can edit the same files manually; no interview service or UI form is
required.

Keys belong in local configuration, never in chat or skills. The optional console starts with:

```bash
uv run paid-media-agent setup --no-open --no-token --port 8765
```

Its flow is Welcome → Model → Accounts → Run. It uses the StructureML tokens and
fonts in `admin/static/app.css`; preserve them.
Do not start a server against real accounts while testing the UI. A saved key is not proof of
connectivity, and a running process is not proof of a healthy server.

## Runtime rules

- Share one assembly across the CLI, API, Slack, and reporting.
- Keep coding-agent skills out of the agent's readable paths. Runtime skills are read-only.
- Treat model output, files, memory, Slack events, and MCP metadata as untrusted input.
- Host code owns credentials, account aliases, tool admission, and approval authority.
- Bind only authorized reads to the model. Mutations go through a typed proposal, persisted
  approval, exact digest validation, one mutation attempt, and readback.
- An edited proposal invalidates its earlier approval. No UI or skill can bypass this rule.
- Compute metrics and report tables in code. Preserve missing values and attribution boundaries.
- Keep large provider results in artifacts and return bounded summaries to the model.
- Add tools to the shared assembly so the CLI, API, and Slack all receive them.
- Do not copy private company data, reference inventories, or internal implementation notes.

## Change discipline

For report design, follow [paid-media-design](.agents/skills/paid-media-design/SKILL.md).
Record company style in [DESIGN.md](workspace/skills/report-design/DESIGN.md) and keep the
renderer tokens aligned in the same change. Reuse those tokens in HTML, SVG, and PDF;
do not introduce a separate palette or font in individual reports. Preserve the accepted
appearance during cleanup. Offer company branding during onboarding without blocking setup.

Read existing code before editing. Prefer direct code, small diffs, and deletion of obsolete paths
rather than new compatibility layers. Comments explain constraints, not implementation history.
Use the thermo-nuclear code-quality review skill when available for code reviews.

Update the relevant public documentation and runtime skill when behavior changes. Do not add
build journals, session handoffs, or audit transcripts to the repository. Preserve unrelated
local work. Never run a provider mutation in an automated test.

Do not commit, push, deploy, connect accounts, or publish packages unless the user asks.

## Verification

Start with the relevant existing tests. Before shipping:

```bash
uv sync --all-extras --dev
uv run ruff check .
uv run ruff format --check .
uv run mypy src
uv run pytest -q
uv run paid-media-agent demo --with-proposal
```

CI also scans for secrets. Live integration tests require explicit opt-in and never execute
provider mutations.
