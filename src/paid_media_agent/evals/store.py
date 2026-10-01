"""Eval runs and results in their own DuckDB file, with one run marked as the baseline."""

from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from paid_media_agent.store.db import Store, utc_now

DEFAULT_PATH = Path("workspace/state/evals.duckdb")


class EvalStore:
    def __init__(self, path: Path | None = None, *, store: Store | None = None) -> None:
        self.store = store or Store(path or DEFAULT_PATH)

    def close(self) -> None:
        self.store.close()

    def start_run(
        self,
        *,
        model: str,
        judge_model: str | None,
        anchor: date,
        questions_sha: str,
        git_sha: str | None,
        settings: dict[str, Any],
    ) -> uuid.UUID:
        run_id = uuid.uuid4()
        self.store.write(
            "INSERT INTO eval_runs (run_id, started_at, git_sha, model, judge_model, anchor, "
            "questions_sha, settings) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [run_id, utc_now(), git_sha, model, judge_model, anchor, questions_sha,
             json.dumps(settings, default=str)],
        )  # fmt: skip
        return run_id

    def add_result(self, run_id: uuid.UUID, row: dict[str, Any]) -> None:
        self.store.write(
            "INSERT INTO eval_results (run_id, question_id, passed, checks, judge, answer, calls, "
            "model_calls, input_tokens, output_tokens, cached_tokens, cost_usd, judge_cost_usd, "
            "seconds, error, cache_write_tokens, call_usage, draft, prompt_context) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                run_id,
                row["question_id"],
                row["passed"],
                json.dumps(row["checks"]),
                json.dumps(row["judge"]) if row.get("judge") is not None else None,
                row.get("answer"),
                json.dumps(row.get("calls", []), default=str),
                row.get("model_calls"),
                row.get("input_tokens"),
                row.get("output_tokens"),
                row.get("cached_tokens"),
                row.get("cost_usd"),
                row.get("judge_cost_usd"),
                row.get("seconds"),
                row.get("error"),
                row.get("cache_write_tokens"),
                json.dumps(row["call_usage"]) if row.get("call_usage") is not None else None,
                row.get("draft"),
                row.get("prompt_context"),
            ],
        )

    def finish_run(self, run_id: uuid.UUID, totals: dict[str, Any]) -> None:
        self.store.write(
            "UPDATE eval_runs SET finished_at = ?, totals = ? WHERE run_id = ?",
            [utc_now(), json.dumps(totals), run_id],
        )

    def set_baseline(self, run_id: uuid.UUID) -> None:
        if not self.store.fetch("SELECT 1 FROM eval_runs WHERE run_id = ?", [run_id]):
            raise ValueError(f"no eval run {run_id}")
        with self.store.transaction() as cursor:
            cursor.execute("UPDATE eval_runs SET baseline = false WHERE baseline")
            cursor.execute("UPDATE eval_runs SET baseline = true WHERE run_id = ?", [run_id])

    def resolve(self, ref: str | None) -> uuid.UUID | None:
        """A run id, a unique prefix, 'latest', or 'baseline'."""
        if ref in (None, "latest"):
            rows = self.store.fetch(
                "SELECT run_id FROM eval_runs WHERE finished_at IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1"
            )
        elif ref == "baseline":
            rows = self.store.fetch("SELECT run_id FROM eval_runs WHERE baseline LIMIT 1")
        else:
            rows = self.store.fetch(
                "SELECT run_id FROM eval_runs WHERE CAST(run_id AS VARCHAR) LIKE ?", [f"{ref}%"]
            )
            if len(rows) > 1:
                raise ValueError(f"{ref} matches {len(rows)} runs; give more of the id")
        return rows[0][0] if rows else None

    def run(self, run_id: uuid.UUID) -> dict[str, Any]:
        (row,) = self.store.fetch_dicts("SELECT * FROM eval_runs WHERE run_id = ?", [run_id])
        row["totals"] = json.loads(row["totals"]) if row["totals"] else None
        return row

    def results(self, run_id: uuid.UUID) -> list[dict[str, Any]]:
        rows = self.store.fetch_dicts(
            "SELECT * FROM eval_results WHERE run_id = ? ORDER BY question_id", [run_id]
        )
        for row in rows:
            row["checks"] = json.loads(row["checks"])
            row["judge"] = json.loads(row["judge"]) if row["judge"] else None
            row["calls"] = json.loads(row["calls"])
            usage = row.get("call_usage")
            row["call_usage"] = json.loads(usage) if isinstance(usage, str) else usage
        return rows
