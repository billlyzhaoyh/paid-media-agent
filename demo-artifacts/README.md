# Demo artifacts

Everything needed to present the Paid Media Agent demo from any laptop, already rendered. Nothing
here needs a model key, a TabPFN token, an install, or a network connection.

| File | What it is | How to use it |
| --- | --- | --- |
| `demo.html` | The animated demo page: two intuition diagrams, the feature engineering, and the eight-week replay | Open it in a browser. The replay starts when you scroll to it; space pauses and plays. |
| `demo-replay.mp4` | The replay as a 31-second video (1280×800) | For slides, or as a fallback if a browser is not to hand. |
| `demo_report.html` | The report the agent wrote for the store's last fortnight | Linked from the demo page ("Open the report the agent wrote"); keep it beside `demo.html`. |
| `demo_report.pdf` | The same report as a PDF | Hand out or attach. |
| `walkthrough.html` | A walkthrough of the repository and how the demo is built | Open it in a browser. It shows `demo-replay.mp4` and the two screenshots from this folder. |
| `demo-page.png`, `demo-report.png` | Full-page screenshots of the demo page and the report | For slides. |
| `claude-memory/` | Claude Code's project notes from the laptop this was built on | See below. |

The pages were rendered in the project's Docker image from the recording shipped in the package,
so they match what `make demo` produces.

## Rebuilding it on a new laptop

```bash
git clone https://github.com/billlyzhaoyh/paid-media-agent.git
cd paid-media-agent
git checkout demo-artifacts        # or main, once the demo pull request is merged
uv sync --all-extras --dev
make demo                          # opens workspace/out/demo.html; calls no model and no TabPFN
make demo-video                    # optional: re-records the MP4 (needs Chrome and ffmpeg)
```

The PDF is rendered only where the PDF libraries are installed, which the Docker image has:

```bash
docker build -t paid-media-agent .
docker run --rm -v "$PWD/workspace/out:/app/workspace/out" paid-media-agent \
  paid-media-agent demo --visual --no-open
```

## What is not in the repository

- **`.env`** with the model and TabPFN keys. The demo does not need it. Asking the agent real
  questions, running the evals, and re-recording do. Create a new one from `.env.example` with
  fresh keys.
- **`workspace/state/evals.duckdb`**, the stored eval runs. Their results are written up in
  `docs/architecture/evals.md`.

## Claude Code's notes

`claude-memory/` holds the notes Claude Code kept about this project: open items, the roadmap,
and research. To carry them to a new laptop, copy the files into Claude Code's memory folder for
the project there. The folder is named after the project's path:

```bash
mkdir -p ~/.claude/projects/<the-project-path-with-dashes>/memory
cp demo-artifacts/claude-memory/*.md ~/.claude/projects/<the-project-path-with-dashes>/memory/
```

They contain no key values.
