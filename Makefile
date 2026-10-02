# Shortcuts for the commands everyone runs. Each line is what CI runs, nothing more.
.PHONY: setup demo demo-video check test lint format doctor eval

setup:        ## install everything
	uv sync --all-extras --dev

demo:         ## an animated page: eight weeks of the agent on a simulated store, and its report
	uv run paid-media-agent demo --visual

demo-video:   ## the demo's replay as an MP4 for slides (needs Chrome and ffmpeg)
	uv run paid-media-agent demo --visual --no-open
	uv run python -m paid_media_agent.testing.demo_video workspace/out/demo.html docs/media/demo-replay.mp4

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
