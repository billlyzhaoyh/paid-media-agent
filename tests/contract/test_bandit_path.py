"""The bandit in closed loop on a simulated account, and its CLI commands."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from click.testing import CliRunner

from paid_media_agent.bandit.evaluate import compare_policies, kappa_contraction
from paid_media_agent.cli import main
from paid_media_agent.sim.simulator import ScenarioParams


async def test_thompson_sampling_beats_static_budgets_and_the_cpa_rule() -> None:
    """The plan's acceptance run: fixed seed, 5 campaigns, 60 days after a 6-week warm-up.

    This seed is one where Thompson sampling also beats the CPA rule; across seeds it does on
    average but not every time (docs/architecture/budget-bandit.md has the table).
    """
    params = ScenarioParams(
        scenario_id="accept",
        seed=3,
        n_campaigns=5,
        start=date(2026, 1, 5),
        cold_starts=0,
        shock_rate=0.0,
    )
    results = await compare_policies(
        params,
        warmup_days=42,
        days=60,
        policies=("oracle", "static", "cpa_rule", "thompson"),
    )
    oracle = results["oracle"].expected_conversions
    regret = {p: oracle - r.expected_conversions for p, r in results.items()}
    assert regret["thompson"] < regret["cpa_rule"] < regret["static"], regret
    assert regret["thompson"] < 0.1 * regret["static"]
    assert all(not r.violations for r in results.values())
    contraction = kappa_contraction(results["thompson"])
    assert contraction["error_last"] < contraction["error_first"]
    assert contraction["sd_last"] < contraction["sd_first"]
    assert results["thompson"].decisions == 9


def test_the_cli_simulates_and_evaluates(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAID_MEDIA_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setenv("PAID_MEDIA_STATE_PATH", str(tmp_path / "state" / "pma.duckdb"))
    runner = CliRunner()
    small = ["--campaigns", "3", "--warmup", "35", "--days", "14"]
    made = runner.invoke(
        main, ["bandit", "simulate", "--scenario", "cli", *small, "--start", "2026-01-05", "--json"]
    )
    assert made.exit_code == 0, made.output
    report = json.loads(made.output)
    assert report["decisions"] == 2 and report["violations"] == []
    assert report["global_model"] == ["pooled:pooled-loglog/1"]
    assert {r["entity_ref"] for r in report["rows"]} == {"sim-001", "sim-002", "sim-003"}
    assert Path(report["path"]).name == "sim-cli.duckdb"
    again = runner.invoke(main, ["bandit", "simulate", "--scenario", "cli", *small])
    assert again.exit_code != 0 and "--force" in again.output

    evaluated = runner.invoke(main, ["bandit", "evaluate", "--seeds", "1", *small])
    assert evaluated.exit_code == 0, evaluated.output
    assert "thompson" in evaluated.output and "constraint violations: 0" in evaluated.output
    assert "cbs" in evaluated.output
