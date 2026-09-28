# Agent workspace

Use this folder for runtime skills and local working files.

| Path | Purpose |
| --- | --- |
| `skills/` | Runtime skills and the general paid-media wiki |
| `skills/company-context/` | Your private business context, written by your coding agent or edited by hand |
| `sources/` | Original briefs and exports used to prepare context |
| `in/`, `analysis/`, `out/` | Runtime input, calculations, and report artifacts |

The root `skills` link points here, so local runs, `serve`, and Docker load the same runtime skills.
Local coding-agent workflows live in `.agents/skills/` at the repository root.

Start with [customization](../docs/customization.md). Business context and source files are
Git-ignored; a Docker image built from this directory can still include them, so keep sensitive
original briefs outside it. Runtime skills are read-only to the agent; update them locally and
restart `serve` (or rebuild the image) to pick up the changes.
