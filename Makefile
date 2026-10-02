# Shortcuts for the commands everyone runs. Each line is what CI runs, nothing more.
.PHONY: setup demo check test lint format doctor eval

setup:        ## install everything
	uv sync --all-extras --dev

demo:         ## a report page for a simulated account: expected ranges, budget curves, an approval
	uv run paid-media-agent demo --visual

check:        ## the CI gate: lint, format, types, tests
	uv run ruff check . && uv run ruff format --check . && uv run mypy src && uv run pytest -q

lint:
	uv run ruff check . --fix && uv run ruff format .

test:
	uv run pytest -q

doctor:
	uv run paid-media-agent doctor

eval:         ## opt-in: the 30-question eval on sample data (bills the model; not in CI)
	uv run paid-media-agent eval run $(if $(MODEL),--model $(MODEL)) $(if $(IDS),--ids $(IDS)) $(if $(REPEAT),--repeat $(REPEAT))
