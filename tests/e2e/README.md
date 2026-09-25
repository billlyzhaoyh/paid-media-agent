# Browser walkthrough

`console_walkthrough.mjs` covers direct model setup, a missing model key, Accounts → Run, the
"Run locally" card (API credentials, the local state check, and `serve` controls), the Docker card,
and optional Slack. Mocked responses also check process polling with keyboard focus and independent
account selection when two platforms share an ID. It captures a narrow viewport and uses reduced
motion. It never connects an account or starts a server against real credentials. The Python
contract tests own the state check and process endpoints.

Run against a **throwaway project copy**, with provider keys and tracing disabled. From the repository root:

```bash
mkdir -p /tmp/pma-console/workspace
mkdir -p /tmp/pma-console/config
cp -R instructions.md .env.example pyproject.toml uv.lock /tmp/pma-console/
cp -R workspace/skills /tmp/pma-console/workspace/
ln -s workspace/skills /tmp/pma-console/skills
cp config/*.example.toml /tmp/pma-console/config/
# Do not copy .env or config/accounts.toml. Use a fresh /tmp/pma-console directory.
uv run paid-media-agent --help
```

Start the console with an explicit project root so an editable install cannot write to your
source checkout. Use a clean shell with no exported provider credentials:

```bash
uv run python -c 'from pathlib import Path; from paid_media_agent.admin.server import run_console; run_console(Path("/tmp/pma-console"), port=8766, open_browser=False, require_token=False)'
```

With Node 22 and Playwright installed in your browser-testing environment:

```bash
node tests/e2e/console_walkthrough.mjs http://127.0.0.1:8766 ./artifacts/console-shots
```

A coding agent can also run the same journey in its in-app browser. Check that colors, fonts,
radii, and text sizes match the existing CORE tokens in `admin/static/app.css`; new screens must
not override those tokens. Check keyboard focus, visible errors, and preservation of unsaved
input while the `serve` process status updates.

The Python suite owns runtime and failure behavior in `tests/contract/test_console_api.py`.
The browser walkthrough owns rendered controls and navigation. No separate frontend build is required.
