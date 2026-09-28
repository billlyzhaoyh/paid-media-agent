"""Command-line entry: setup console, demo, doctor, tests, accounts, reports, history, bandit,
and serve.

Every console action has a subcommand with `--json`, so coding agents and humans share one path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from typing import Any

import click

from paid_media_agent.admin import actions
from paid_media_agent.config import Settings, project_root
from paid_media_agent.domain.common import Platform
from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.testing.demo_script import run_demo


def _configure_logging(settings: Settings) -> None:
    logging.basicConfig(
        level=settings.paid_media_log_level.upper(), format="%(levelname)s %(name)s: %(message)s"
    )


def _emit(result: actions.ActionResult, as_json: bool) -> None:
    if as_json:
        click.echo(actions.as_json(result))
    else:
        click.echo(f"{result.status.upper():<5} {result.action}: {result.summary}")
        for key, value in result.detail.items():
            if isinstance(value, str | int | float | bool):
                click.echo(f"      {key}: {value}")
    if result.status == "fail":
        sys.exit(1)


@click.group()
def main() -> None:
    """Paid Media Agent command line.

    Every command sees the project `.env` the same way: allowlisted values are exported into this
    process before the command runs, so model and provider keys work without a manual `export`.
    """
    from paid_media_agent.admin.envfile import apply_env_file

    apply_env_file(project_root())


# ---------------------------------------------------------------- setup console


@main.command()
@click.option(
    "--port",
    type=int,
    default=None,
    help="Local port for the console. Defaults to $PORT when a host assigns one, else 8765.",
)
@click.option("--no-open", is_flag=True, help="Do not open the browser automatically.")
@click.option(
    "--no-token",
    is_flag=True,
    help="Serve without the per-run token so a coding agent's browser pane can open the plain URL. "
    "Same-origin calls only; for your own machine.",
)
def setup(port: int | None, no_open: bool, no_token: bool) -> None:
    """Start the local setup console and open it in the browser.

    Coding agents with a browser pane (Claude Code desktop, Cursor, the Codex app) run it with
    `--no-open --no-token` and open http://127.0.0.1:PORT in that pane. Hosts that assign a port
    pass it as $PORT.
    """
    import os

    from paid_media_agent.admin.server import run_console

    resolved = port if port is not None else int(os.environ.get("PORT", "8765"))
    run_console(project_root(), port=resolved, open_browser=not no_open, require_token=not no_token)


# ---------------------------------------------------------------- demo and doctor


@main.command()
@click.option(
    "--with-proposal",
    is_flag=True,
    help="Also run the governed fake-write flow after the analysis.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Print the final message and tool audit as JSON."
)
def demo(with_proposal: bool, as_json: bool) -> None:
    """Run the fixture-backed demo through the real agent loop with no network or secrets."""
    settings = Settings(paid_media_model="scripted:demo", paid_media_allow_self_approval=True)
    _configure_logging(settings)
    try:
        result = asyncio.run(run_demo(settings, with_proposal=with_proposal))
    except Exception as exc:
        raise click.ClickException(f"demo failed: {sanitize_exception(exc)}") from None
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    click.echo(result["answer"])
    if with_proposal:
        click.echo("")
        click.echo("Proposal review (from the persisted ChangeSet):")
        click.echo(json.dumps(result["proposal"], indent=2, default=str))
        click.echo("")
        click.echo(result["receipt_message"])
    click.echo("")
    click.echo(
        f"Tool audit: {len(result['audit'])} host-side reads; catalog revision {result['catalog_revision']}."
    )
    coverage = result["history"]["coverage"]
    click.echo(
        f"History: {sum(c['snapshot_rows'] for c in coverage)} entity-day snapshots from "
        f"{sum(c['pulls'] for c in coverage)} pulls across {len({c['account_alias'] for c in coverage})} "
        "accounts (in memory for the demo; `serve` and `sync` keep them in the state file)."
    )
    if result["history"]["changes"]:
        click.echo("Change log: " + "; ".join(result["history"]["changes"]) + ".")


@main.command()
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def doctor(as_json: bool) -> None:
    """Diagnose configuration without printing secret values."""
    from paid_media_agent.doctor import format_checks, run_doctor

    root = project_root()
    if as_json:
        _emit(actions.status(root), True)
        return
    settings = actions.load_settings(root)
    checks = run_doctor(settings, project_root=root)
    click.echo(format_checks(checks))
    if any(not c.ok for c in checks):
        sys.exit(1)


# ---------------------------------------------------------------- config


@main.group()
def config() -> None:
    """Show or change local `.env` settings without printing secrets."""


@config.command("show")
@click.option("--json", "as_json", is_flag=True)
def config_show(as_json: bool) -> None:
    result = actions.config_view(project_root())
    if as_json:
        _emit(result, True)
        return
    for key in result.detail["keys"]:
        mark = "set" if key["is_set"] else "unset"
        shown = key["value"] if key["is_set"] else ""
        click.echo(f"{key['name']:<40} {mark:<6} {shown}")


@config.command("set")
@click.argument("pairs", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True)
def config_set(pairs: tuple[str, ...], as_json: bool) -> None:
    """Set KEY=VALUE pairs in .env. Keys are validated against the settings schema."""
    updates: dict[str, str] = {}
    for pair in pairs:
        key, sep, value = pair.partition("=")
        if not sep:
            raise click.UsageError(f"expected KEY=VALUE, got {pair}")
        updates[key.strip()] = value
    _emit(actions.config_set(project_root(), updates), as_json)


@config.command("generate")
@click.argument("keys", nargs=-1, required=True)
@click.option("--json", "as_json", is_flag=True)
def config_generate(keys: tuple[str, ...], as_json: bool) -> None:
    """Generate strong values for host-owned secrets (signing key, API token)."""
    for key in keys:
        result = actions.generate_secret(project_root(), key)
        _emit(result, as_json)
        if not as_json and result.detail.get("show_once"):
            click.echo(f"      API token (shown once): {result.detail['show_once']}")
            if result.detail.get("usage"):
                click.echo(f"      Use as: {result.detail['usage']}")


# ---------------------------------------------------------------- accounts


@main.group()
def accounts() -> None:
    """Discover provider accounts and map public aliases."""


@accounts.command("discover")
@click.option("--json", "as_json", is_flag=True)
def accounts_discover(as_json: bool) -> None:
    result = actions.accounts_discover(project_root())
    if as_json:
        _emit(result, True)
        return
    click.echo(f"{result.status.upper()} {result.summary}")
    for row in result.detail.get("accounts", []):
        click.echo(
            f"  {row['platform']:<12} {row['provider_account_id']:<24} {row['name']}  {row.get('currency', '')} {row.get('timezone', '')}"
        )
    click.echo(
        "Map one with: paid-media-agent accounts add <alias> --platform <platform> --id <provider-id> --currency USD --timezone America/New_York"
    )


@accounts.command("list")
@click.option("--json", "as_json", is_flag=True)
def accounts_list(as_json: bool) -> None:
    _emit(actions.accounts_list(project_root()), as_json)


@accounts.command("add")
@click.argument("alias")
@click.option("--platform", required=True, type=click.Choice([p.value for p in Platform]))
@click.option(
    "--id",
    "provider_account_id",
    required=True,
    help="Provider account id from `accounts discover`.",
)
@click.option("--currency", default="USD", show_default=True)
@click.option("--timezone", default="UTC", show_default=True)
@click.option("--json", "as_json", is_flag=True)
def accounts_add(
    alias: str, platform: str, provider_account_id: str, currency: str, timezone: str, as_json: bool
) -> None:
    _emit(
        actions.accounts_add(
            project_root(),
            alias=alias,
            platform=platform,
            provider_account_id=provider_account_id,
            currency=currency,
            timezone=timezone,
        ),
        as_json,
    )


@accounts.command("remove")
@click.argument("alias")
@click.option("--json", "as_json", is_flag=True)
def accounts_remove(alias: str, as_json: bool) -> None:
    _emit(actions.accounts_remove(project_root(), alias), as_json)


# ---------------------------------------------------------------- catalog, policy, tests


@main.group()
def catalog() -> None:
    """Inspect the authorized tool catalog."""


@catalog.command("show")
@click.option(
    "--live", is_flag=True, help="Load the live Pipeboard catalog instead of the fixture."
)
@click.option("--json", "as_json", is_flag=True)
def catalog_show(live: bool, as_json: bool) -> None:
    result = actions.catalog_show(project_root(), live=live)
    if as_json:
        _emit(result, True)
        return
    click.echo(f"{result.summary}")
    for entry in result.detail.get("entries", []):
        click.echo(f"  {entry['class']:<9} {entry['name']:<48} {entry['reason']}")


@main.group()
def policy() -> None:
    """Validate the reviewed mutation set."""


@policy.command("validate")
@click.option("--live", is_flag=True, help="Validate against the live Pipeboard catalog.")
@click.option("--json", "as_json", is_flag=True)
def policy_validate(live: bool, as_json: bool) -> None:
    result = actions.policy_validate(project_root(), live=live)
    if as_json:
        _emit(result, True)
        return
    click.echo(f"{result.status.upper()} {result.summary}")
    for issue in result.detail.get("issues", []):
        click.echo(f"  {issue['tool']:<48} {issue['reason']}")


@main.command("models")
@click.option("--provider", required=True, help="Provider card ID, e.g. anthropic or openai.")
@click.option("--json", "as_json", is_flag=True)
def models(provider: str, as_json: bool) -> None:
    """List models from the provider's official API using its configured key."""
    _emit(actions.models_list(project_root(), provider), as_json)


@main.group()
def test() -> None:
    """Connection tests that never print secret values."""


@test.command("model")
@click.option("--json", "as_json", is_flag=True)
def test_model(as_json: bool) -> None:
    _emit(actions.model_test(project_root()), as_json)


@test.command("pipeboard")
@click.option("--json", "as_json", is_flag=True)
def test_pipeboard(as_json: bool) -> None:
    _emit(actions.pipeboard_test(project_root()), as_json)


@test.command("slack")
@click.option("--json", "as_json", is_flag=True)
def test_slack(as_json: bool) -> None:
    """Rich Slack adapter credentials (self-hosted path)."""
    _emit(actions.slack_test(project_root()), as_json)


@test.command("state")
@click.option("--json", "as_json", is_flag=True)
def test_state(as_json: bool) -> None:
    """Open the DuckDB state file and apply migrations (self-hosted path)."""
    _emit(actions.state_test(project_root()), as_json)


@test.command("all")
@click.option("--json", "as_json", is_flag=True)
def test_all(as_json: bool) -> None:
    root = project_root()
    results = [actions.model_test(root), actions.pipeboard_test(root)]
    if actions.load_settings(root).paid_media_runtime == "self_hosted":
        results += [actions.slack_test(root), actions.state_test(root)]
    if as_json:
        click.echo(json.dumps([r.model_dump(mode="json") for r in results], indent=2, default=str))
    else:
        for result in results:
            click.echo(f"{result.status.upper():<5} {result.action}: {result.summary}")
    if any(r.status == "fail" for r in results):
        sys.exit(1)


# ---------------------------------------------------------------- writes


@main.group()
def writes() -> None:
    """Write gates and the incident kill switch."""


@writes.command("kill-switch")
@click.argument("state", type=click.Choice(["on", "off"]))
@click.option("--yes", is_flag=True, help="Required to clear the kill switch.")
@click.option("--json", "as_json", is_flag=True)
def writes_kill_switch(state: str, yes: bool, as_json: bool) -> None:
    _emit(actions.kill_switch_set(project_root(), engaged=state == "on", confirmed=yes), as_json)


# ---------------------------------------------------------------- ask


@main.command()
@click.argument("question")
@click.option("--json", "as_json", is_flag=True)
def ask(question: str, as_json: bool) -> None:
    """Run one question locally through the same profile the deployment runs."""
    result = actions.ask_question(project_root(), question)
    if as_json:
        _emit(result, True)
        return
    click.echo(result.detail.get("answer", result.summary))
    if result.status == "fail":
        sys.exit(1)


# ---------------------------------------------------------------- reports


@main.command()
@click.option(
    "--cadence", type=click.Choice(["weekly", "monthly"]), default="weekly", show_default=True
)
@click.option(
    "--end", "end_date", default=None, help="Last complete day (YYYY-MM-DD). Defaults to yesterday."
)
@click.option(
    "--alias",
    "aliases",
    multiple=True,
    help="Account alias to include. Repeatable; default is every alias.",
)
@click.option("--no-render", is_flag=True, help="Compare only; skip the HTML/PDF report.")
@click.option("--json", "as_json", is_flag=True)
def report(
    cadence: str, end_date: str | None, aliases: tuple[str, ...], no_render: bool, as_json: bool
) -> None:
    """Run the deterministic cross-platform report: reads, comparison, and rendering, no model."""
    from paid_media_agent.reports.cadence import run_cadence_report
    from paid_media_agent.runtime.local import build_configured_runtime
    from paid_media_agent.tools.compute import ComputeError

    settings = Settings()
    _configure_logging(settings)
    root = project_root()
    end = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=1)

    async def _run() -> Any:
        runtime = await asyncio.to_thread(build_configured_runtime, settings, project_root=root)
        return await run_cadence_report(
            cadence=cadence,  # type: ignore[arg-type]
            end=end,
            accounts=runtime.profile.accounts,
            catalog=runtime.catalog,
            dispatcher=runtime.components.read_dispatcher,
            artifacts=runtime.profile.artifacts,
            aliases=aliases or None,
            render=not no_render,
        )

    try:
        run = asyncio.run(_run())
    except (RuntimeError, ComputeError) as exc:
        # A window the data does not cover is refused, not compared; say which and stop.
        click.echo(f"FAIL report: {sanitize_exception(exc)}", err=True)
        sys.exit(1)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "cadence": run.cadence,
                    "current": run.windows.current.model_dump(mode="json"),
                    "previous": run.windows.previous.model_dump(mode="json"),
                    "reads": list(run.read_artifacts),
                    "unavailable": list(run.unavailable),
                    "analysis_artifact_id": run.analysis_artifact_id,
                    "summary": run.summary,
                    "report": run.report,
                    "reconciled": run.reconciled,
                },
                indent=2,
                default=str,
            )
        )
        return
    click.echo(
        f"{run.cadence} report · {run.windows.current.start} to {run.windows.current.end} vs {run.windows.previous.start} to {run.windows.previous.end}"
    )
    for platform in run.summary["platforms"]:
        click.echo(
            f"  {platform['platform']:<12} spend {platform['spend_current']} vs {platform['spend_previous']} ({platform['spend_change']}); CPA {platform['cpa_current']} vs {platform['cpa_previous']}"
        )
    if run.unavailable:
        click.echo("  unavailable: " + "; ".join(run.unavailable))
    click.echo(
        f"  analysis {run.analysis_artifact_id} · reconciled={'yes' if run.reconciled else 'NO'}"
    )
    if run.report:
        click.echo(
            "  files: "
            + ", ".join(f["path"] for f in run.report["files"])
            + f" · pdf {run.report['pdf']}"
        )
    if not run.reconciled:
        sys.exit(1)


# ---------------------------------------------------------------- history


def _day(value: str | None, default: date | None = None) -> date | None:
    if value is None:
        return default
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise click.BadParameter(f"{value} is not a YYYY-MM-DD date") from None


def _server_job(settings: Settings, name: str) -> dict[str, Any]:
    """Run a job inside the `serve` process that holds the state file, over its API."""
    import httpx

    logging.getLogger("httpx").setLevel(logging.WARNING)
    tokens = settings.api_token_map()
    if not tokens:
        raise click.ClickException(
            "`serve` holds the state file and PAID_MEDIA_API_TOKENS is not set, so this command "
            "cannot ask it to run the job. Stop `serve`, or configure an API token."
        )
    host = settings.paid_media_api_host
    if host in ("0.0.0.0", "::", ""):  # noqa: S104 - a bind-all address, not a destination
        host = "127.0.0.1"
    try:
        response = httpx.post(
            f"http://{host}:{settings.paid_media_api_port}/jobs/{name}",
            headers={"Authorization": f"Bearer {next(iter(tokens))}"},
            timeout=900,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(f"could not reach `serve`: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise click.ClickException(f"`serve` refused the job: HTTP {response.status_code}")
    result: dict[str, Any] = response.json()
    return result


def _state_runtime(settings: Settings) -> Any:
    from paid_media_agent.runtime.self_hosted import build_self_hosted_runtime

    return build_self_hosted_runtime(settings, project_root=project_root())


def _echo_sync(summary: dict[str, Any], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(summary, indent=2, default=str))
        return
    click.echo(
        f"{summary['source']} {summary['start']} to {summary['end']}: {summary['rows']} "
        f"entity-days, {summary['settings']} campaign settings, {len(summary['reads'])} reads."
    )
    if summary["unavailable"]:
        click.echo("  unavailable: " + "; ".join(summary["unavailable"]))


@main.command()
@click.option(
    "--days", type=int, default=None, help="Trailing days (default PAID_MEDIA_SYNC_DAYS)."
)
@click.option("--end", "end_date", default=None, help="Last day (YYYY-MM-DD). Default: yesterday.")
@click.option("--alias", "aliases", multiple=True, help="Account alias. Repeatable; default all.")
@click.option("--json", "as_json", is_flag=True)
def sync(days: int | None, end_date: str | None, aliases: tuple[str, ...], as_json: bool) -> None:
    """Pull recent performance and campaign settings into the history store.

    When `serve` holds the state file, this asks it to run its sync job instead.
    """
    from paid_media_agent.analytics.sync import run_sync
    from paid_media_agent.store import StoreBusy

    settings = Settings()
    _configure_logging(settings)
    end = _day(end_date, date.today() - timedelta(days=1))
    assert end is not None  # noqa: S101 - a default is supplied
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        runtime = None
    if runtime is None:
        if days or end_date or aliases:
            raise click.ClickException(
                "`serve` holds the state file; run `sync` without options to use its job"
            )
        run = _server_job(settings, "sync")
        if run["status"] != "ok":
            raise click.ClickException(f"sync failed in `serve`: {run['detail']}")
        _echo_sync(run["detail"], as_json)
        return
    try:
        result = asyncio.run(
            run_sync(
                accounts=runtime.profile.accounts,
                catalog=runtime.catalog,
                dispatcher=runtime.components.read_dispatcher,
                end=end,
                days=days or settings.paid_media_sync_days,
                aliases=aliases or None,
            )
        )
    finally:
        runtime.store.close()
    _echo_sync(result.summary(), as_json)
    if not result.reads:
        sys.exit(1)


@main.command()
@click.option("--start", "start_date", required=True, help="First day (YYYY-MM-DD).")
@click.option("--end", "end_date", default=None, help="Last day (YYYY-MM-DD). Default: yesterday.")
@click.option("--alias", "aliases", multiple=True, help="Account alias. Repeatable; default all.")
@click.option("--json", "as_json", is_flag=True)
def backfill(
    start_date: str, end_date: str | None, aliases: tuple[str, ...], as_json: bool
) -> None:
    """Pull an older date range into the history store, 28 days per read."""
    from paid_media_agent.analytics.sync import run_backfill
    from paid_media_agent.store import StoreBusy

    settings = Settings()
    _configure_logging(settings)
    start = _day(start_date)
    end = _day(end_date, date.today() - timedelta(days=1))
    assert start is not None and end is not None  # noqa: S101 - required or defaulted
    try:
        runtime = _state_runtime(settings)
    except StoreBusy as exc:
        raise click.ClickException(f"{exc}. Stop `serve` to backfill.") from None
    try:
        result = asyncio.run(
            run_backfill(
                accounts=runtime.profile.accounts,
                catalog=runtime.catalog,
                dispatcher=runtime.components.read_dispatcher,
                start=start,
                end=end,
                aliases=aliases or None,
            )
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        runtime.store.close()
    _echo_sync(result.summary(), as_json)
    if not result.reads:
        sys.exit(1)


@main.command()
@click.option("--scenario", default="baseline", show_default=True, help="Scenario name.")
@click.option("--days", type=click.IntRange(30, 1000), default=180, show_default=True)
@click.option("--campaigns", type=click.IntRange(1, 50), default=5, show_default=True)
@click.option("--seed", type=int, default=7, show_default=True)
@click.option("--start", "start_date", default=None, help="First day. Default: --days ago.")
@click.option("--force", is_flag=True, help="Replace an existing scenario file.")
@click.option("--json", "as_json", is_flag=True)
def simulate(
    scenario: str,
    days: int,
    campaigns: int,
    seed: int,
    start_date: str | None,
    force: bool,
    as_json: bool,
) -> None:
    """Simulate campaigns with known response curves into their own history file."""
    from paid_media_agent.runtime.self_hosted import state_path
    from paid_media_agent.sim.scenario import run_scenario, scenario_path
    from paid_media_agent.sim.simulator import ScenarioParams
    from paid_media_agent.store import Store

    settings = Settings()
    start = _day(start_date, date.today() - timedelta(days=days))
    assert start is not None  # noqa: S101 - a default is supplied
    try:
        path = scenario_path(state_path(settings, project_root()).parent, scenario)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--scenario") from None
    if path.exists():
        if not force:
            raise click.ClickException(f"{path} exists; pass --force to replace it")
        for stale in (path, path.with_name(path.name + ".wal")):
            stale.unlink(missing_ok=True)
    params = ScenarioParams(
        scenario_id=scenario, seed=seed, n_campaigns=campaigns, days=days, start=start
    )
    store = Store(path)
    try:
        run = run_scenario(store, params)
    finally:
        store.close()
    truth = [
        {
            "entity_ref": c.entity_ref,
            "kappa1": round(c.kappa1, 4),
            "kappa2": round(c.kappa2, 4),
            "base_budget": c.base_budget,
            "starts_on": (start + timedelta(days=c.start_index)).isoformat(),
        }
        for c in run.campaigns
    ]
    if as_json:
        click.echo(
            json.dumps(
                {
                    "path": str(path),
                    "params": params.as_json(),
                    "campaigns": truth,
                    "pulls": run.pulls,
                    "snapshot_rows": run.snapshot_rows,
                    "truth_rows": run.truth_rows,
                    "shocks": run.shocks,
                    "external_changes": run.external_changes,
                },
                indent=2,
            )
        )
        return
    click.echo(f"Scenario {scenario}: {days} days from {start}, seed {seed} -> {path}")
    for c in truth:
        click.echo(
            f"  {c['entity_ref']}  kappa1 {c['kappa1']:>8}  kappa2 {c['kappa2']:<7} "
            f"base budget {c['base_budget']:>8}  from {c['starts_on']}"
        )
    click.echo(
        f"  {run.pulls} pulls, {run.snapshot_rows} snapshot rows, {run.truth_rows} true "
        f"campaign-days, {run.shocks} labelled shocks, {run.external_changes} budget changes."
    )
    click.echo(f"Inspect it with: paid-media-agent history --scenario {scenario} --view daily")


_HISTORY_COLUMNS: dict[str, tuple[str, ...]] = {
    "daily": (
        "day",
        "account_alias",
        "entity_ref",
        "spend",
        "conversions",
        "conversions_matured",
        "daily_budget",
        "pacing_ratio",
        "age_days",
    ),
    "settings": ("valid_from", "account_alias", "entity_ref", "status", "daily_budget"),
    "changes": (
        "occurred_at",
        "source",
        "status",
        "account_alias",
        "entity_ref",
        "field",
        "before_value",
        "after_value",
    ),
    "lag": ("account_alias", "platform", "age_days", "entity_days", "completeness"),
}


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:.3f}" if abs(value) < 10 else f"{value:.2f}"
    return str(value)[:26]


@main.command()
@click.option(
    "--view",
    type=click.Choice(["coverage", "daily", "settings", "changes", "lag"]),
    default="coverage",
    show_default=True,
)
@click.option("--scenario", default=None, help="Read a simulated scenario instead of the state.")
@click.option("--alias", default=None, help="Account alias.")
@click.option("--entity", default=None, help="Campaign or entity id.")
@click.option("--start", "start_date", default=None, help="First day (YYYY-MM-DD).")
@click.option("--end", "end_date", default=None, help="Last day (YYYY-MM-DD).")
@click.option("--limit", type=click.IntRange(1, 500), default=30, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def history(
    view: str,
    scenario: str | None,
    alias: str | None,
    entity: str | None,
    start_date: str | None,
    end_date: str | None,
    limit: int,
    as_json: bool,
) -> None:
    """Show stored history: coverage, daily panel, settings versions, changes, or lag."""
    from paid_media_agent.analytics.history import query_history
    from paid_media_agent.runtime.self_hosted import state_path
    from paid_media_agent.sim.scenario import scenario_path
    from paid_media_agent.store import Store, StoreBusy

    settings = Settings()
    path = state_path(settings, project_root())
    if scenario is not None:
        path = scenario_path(path.parent, scenario)
        if not path.exists():
            raise click.ClickException(f"no scenario file {path}; run `simulate` first")
    try:
        store = Store(path)
    except StoreBusy as exc:
        raise click.ClickException(
            f"{exc}. Ask the agent instead; its query_history tool reads the same views."
        ) from None
    try:
        rows, truncated = query_history(
            store,
            view,  # type: ignore[arg-type]
            account_alias=alias,
            entity_ref=entity,
            start=_day(start_date),
            end=_day(end_date),
            limit=limit,
        )
    finally:
        store.close()
    if as_json:
        click.echo(json.dumps({"rows": rows, "truncated": truncated}, indent=2))
        return
    if not rows:
        click.echo(
            "No history yet. Run `paid-media-agent sync`, or `simulate` for sample data."
            if view == "coverage"
            else f"No {view} rows match."
        )
        return
    columns = _HISTORY_COLUMNS.get(view) or tuple(rows[0])
    table = [[_cell(row.get(c)) for c in columns] for row in rows]
    widths = [max(len(c), *(len(r[i]) for r in table)) for i, c in enumerate(columns)]
    click.echo("  ".join(c.ljust(w) for c, w in zip(columns, widths, strict=True)))
    for line in table:
        click.echo("  ".join(v.ljust(w) for v, w in zip(line, widths, strict=True)))
    if truncated:
        click.echo("... more rows; raise --limit or narrow with --alias/--entity/--start/--end.")


@main.command()
@click.option("--alias", default=None, help="Account alias; default every account.")
@click.option("--days", "window_days", type=click.IntRange(1, 28), default=7, show_default=True)
@click.option("--as-of", "as_of", default=None, help="Check as of this date. Default: today.")
@click.option("--scenario", default=None, help="Check a simulated scenario instead of the state.")
@click.option(
    "--predictor",
    type=click.Choice(["none", "local", "tabpfn"]),
    default=None,
    help="Override PAID_MEDIA_PREDICTOR for this run.",
)
@click.option(
    "--band",
    type=click.FloatRange(0.5, 0.99),
    default=None,
    help="Expected-range coverage (default PAID_MEDIA_ANOMALY_BAND, 0.95).",
)
@click.option("--json", "as_json", is_flag=True)
def anomalies(
    alias: str | None,
    window_days: int,
    as_of: str | None,
    scenario: str | None,
    predictor: str | None,
    band: float | None,
    as_json: bool,
) -> None:
    """Flag recent campaign-days whose spend or conversions fall outside their expected range."""
    from paid_media_agent.analytics.anomalies import check_anomalies
    from paid_media_agent.predict.factory import build_predictor
    from paid_media_agent.runtime.self_hosted import state_path
    from paid_media_agent.sim.scenario import scenario_path
    from paid_media_agent.store import Store, StoreBusy

    settings = Settings()
    _configure_logging(settings)
    if predictor is not None:
        settings = settings.model_copy(update={"paid_media_predictor": predictor})
    path = state_path(settings, project_root())
    if scenario is not None:
        path = scenario_path(path.parent, scenario)
        if not path.exists():
            raise click.ClickException(f"no scenario file {path}; run `simulate` first")
    day = _day(as_of, date.today())
    assert day is not None  # noqa: S101 - a default is supplied
    try:
        store = Store(path)
    except StoreBusy as exc:
        raise click.ClickException(
            f"{exc}. Run it inside `serve`: POST /jobs/anomalies, or ask the agent."
        ) from None
    try:
        report = asyncio.run(
            check_anomalies(
                store,
                build_predictor(settings, store),
                as_of=day,
                window_days=window_days,
                account_alias=alias,
                band=band or settings.paid_media_anomaly_band,
            )
        )
    finally:
        store.close()
    result = report.as_json()
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    click.echo(f"Checked as of {result['as_of']} with {result['predictor']}:")
    for metric, method in result["methods"].items():
        click.echo(f"  {metric:<11} {result['windows'][metric]} by {method}")
    for flag in result["flags"]:
        expected = "-" if flag["expected"] is None else f"{flag['expected']}"
        click.echo(
            f"  {flag['day']}  {flag['account_alias']:<14} {flag['entity_ref']:<10} "
            f"{flag['metric']:<11} {flag['direction']:<4} observed {flag['observed']} vs "
            f"expected {expected} [{flag['lo']}, {flag['hi']}]"
        )
    if not result["flags"]:
        click.echo("  No flags.")
    for note in result["notes"]:
        click.echo(f"  note: {note}")


# ---------------------------------------------------------------- budget bandit


@main.group()
def bandit() -> None:
    """Budget allocation across campaigns (CBS-style bandit), on simulated accounts for now."""


_PREDICTOR = click.option(
    "--predictor",
    type=click.Choice(["none", "local", "tabpfn"]),
    default=None,
    help="Global model: tabpfn uses TabPFN (billed tokens); otherwise a local pooled regression.",
)


def _bandit_config(pseudo_samples: int, policy: str = "thompson") -> Any:
    from paid_media_agent.bandit.recommend import BanditConfig

    return BanditConfig(policy=policy, pseudo_samples=pseudo_samples)  # type: ignore[arg-type]


@bandit.command("simulate")
@click.option("--scenario", default="bandit", show_default=True, help="Scenario id for the file.")
@click.option("--campaigns", type=click.IntRange(2, 30), default=5, show_default=True)
@click.option("--warmup", type=click.IntRange(28, 365), default=42, show_default=True)
@click.option("--days", type=click.IntRange(7, 365), default=60, show_default=True)
@click.option("--seed", type=int, default=7, show_default=True)
@click.option(
    "--policy", type=click.Choice(["thompson", "greedy"]), default="thompson", show_default=True
)
@click.option("--pseudo-samples", type=click.IntRange(0, 4096), default=512, show_default=True)
@_PREDICTOR
@click.option("--start", "start_date", default=None, help="First simulated day (YYYY-MM-DD).")
@click.option("--force", is_flag=True, help="Replace an existing scenario file.")
@click.option("--json", "as_json", is_flag=True)
def bandit_simulate(
    scenario: str,
    campaigns: int,
    warmup: int,
    days: int,
    seed: int,
    policy: str,
    pseudo_samples: int,
    predictor: str | None,
    start_date: str | None,
    force: bool,
    as_json: bool,
) -> None:
    """Let the bandit run a simulated account after a warm-up, and compare it with the truth.

    The account runs on an operator's budget schedule for --warmup days, then the bandit sets
    budgets every seven days. Every decision is logged in the scenario file (bandit_runs,
    bandit_decisions). Static budgets and an oracle that knows the true curves run on the same
    account in memory, for comparison.
    """
    from dataclasses import replace

    from paid_media_agent.bandit.evaluate import run_closed_loop
    from paid_media_agent.predict.factory import build_predictor
    from paid_media_agent.runtime.self_hosted import state_path
    from paid_media_agent.sim.scenario import scenario_path
    from paid_media_agent.sim.simulator import ScenarioParams
    from paid_media_agent.store import Store

    settings = Settings()
    _configure_logging(settings)
    if predictor is not None:
        settings = settings.model_copy(update={"paid_media_predictor": predictor})
    start = _day(start_date, date.today() - timedelta(days=warmup + days))
    assert start is not None  # noqa: S101 - a default is supplied
    try:
        path = scenario_path(state_path(settings, project_root()).parent, scenario)
    except ValueError as exc:
        raise click.BadParameter(str(exc), param_hint="--scenario") from None
    if path.exists():
        if not force:
            raise click.ClickException(f"{path} exists; pass --force to replace it")
        for stale in (path, path.with_name(path.name + ".wal")):
            stale.unlink(missing_ok=True)
    params = ScenarioParams(
        scenario_id=scenario, seed=seed, n_campaigns=campaigns, start=start, cold_starts=0
    )
    config = _bandit_config(pseudo_samples, policy)
    store = Store(path)
    try:
        result = asyncio.run(
            run_closed_loop(
                params,
                policy,  # type: ignore[arg-type]
                warmup_days=warmup,
                days=days,
                config=config,
                predictor=build_predictor(settings, store),
                store=store,
            )
        )
        decisions = store.fetch_dicts(
            """
            SELECT r.decision_day, d.entity_ref, d.current_budget, d.final_budget,
                d.post_mean, d.post_cov, d.constrained_by, t.kappa2 AS true_kappa2
            FROM bandit_decisions d
            JOIN bandit_runs r USING (run_id)
            LEFT JOIN (SELECT DISTINCT entity_ref, kappa2 FROM sim_truth) t USING (entity_ref)
            ORDER BY r.decision_day, d.entity_ref
            """
        )
        prior = store.fetch("SELECT DISTINCT prior_source FROM bandit_runs")
    finally:
        store.close()
    baselines = {
        name: asyncio.run(
            run_closed_loop(
                replace(params, scenario_id=f"{scenario}-{name}"),
                name,  # type: ignore[arg-type]
                warmup_days=warmup,
                days=days,
                config=config,
            )
        ).expected_conversions
        for name in ("static", "oracle")
    }
    rows = []
    for d in decisions:
        mean = json.loads(d["post_mean"]) if d["post_mean"] else None
        cov = json.loads(d["post_cov"]) if d["post_cov"] else None
        rows.append(
            {
                "decision_day": d["decision_day"].isoformat(),
                "entity_ref": d["entity_ref"],
                "budget": d["current_budget"],
                "new_budget": None if d["final_budget"] is None else round(d["final_budget"], 2),
                "kappa2": None if mean is None else round(mean[1], 3),
                "kappa2_sd": None if cov is None else round(cov[1][1] ** 0.5, 3),
                "true_kappa2": None if d["true_kappa2"] is None else round(d["true_kappa2"], 3),
                "constrained_by": d["constrained_by"],
            }
        )
    summary = {
        "path": str(path),
        "policy": policy,
        "global_model": sorted(p for (p,) in prior),
        "decisions": result.decisions,
        "expected_conversions": round(result.expected_conversions, 1),
        "static": round(baselines["static"], 1),
        "oracle": round(baselines["oracle"], 1),
        "regret": round(baselines["oracle"] - result.expected_conversions, 1),
        "violations": result.violations,
    }
    if as_json:
        click.echo(json.dumps({**summary, "rows": rows}, indent=2))
        return
    click.echo(
        f"Scenario {scenario}: {warmup} warm-up days, then {policy} for {days} days -> {path}"
    )
    for row in rows:
        moved = (
            "-"
            if row["new_budget"] is None or not row["budget"]
            else f"{row['new_budget'] / row['budget'] - 1:+.0%}"
        )
        kappa = "-" if row["kappa2"] is None else f"{row['kappa2']:.2f}±{row['kappa2_sd']:.2f}"
        click.echo(
            f"  {row['decision_day']}  {row['entity_ref']:<8} {row['budget']:>9.2f} -> "
            f"{row['new_budget'] or 0:>9.2f} {moved:>5}  kappa2 {kappa:<10} "
            f"true {row['true_kappa2']}  {','.join(row['constrained_by'])}"
        )
    click.echo(
        f"Expected conversions over {days} days: {summary['expected_conversions']} "
        f"(static {summary['static']}, oracle {summary['oracle']}; regret {summary['regret']}). "
        f"Constraint violations: {len(result.violations)}."
    )


@bandit.command("evaluate")
@click.option("--seeds", type=click.IntRange(1, 50), default=3, show_default=True)
@click.option("--campaigns", type=click.IntRange(2, 30), default=5, show_default=True)
@click.option("--warmup", type=click.IntRange(28, 365), default=42, show_default=True)
@click.option("--days", type=click.IntRange(7, 365), default=60, show_default=True)
@click.option("--pseudo-samples", type=click.IntRange(0, 4096), default=512, show_default=True)
@_PREDICTOR
@click.option("--json", "as_json", is_flag=True)
def bandit_evaluate(
    seeds: int,
    campaigns: int,
    warmup: int,
    days: int,
    pseudo_samples: int,
    predictor: str | None,
    as_json: bool,
) -> None:
    """Regret of every policy, and payout error of every model, on simulated accounts."""
    import numpy as np

    from paid_media_agent.bandit.evaluate import (
        POLICIES,
        compare_policies,
        kappa_contraction,
        payout_error,
    )
    from paid_media_agent.predict.factory import build_predictor
    from paid_media_agent.sim.simulator import ScenarioParams
    from paid_media_agent.store import Store

    settings = Settings()
    _configure_logging(settings)
    if predictor is not None:
        settings = settings.model_copy(update={"paid_media_predictor": predictor})
    ledger = Store()  # the token guard's ledger for this evaluation
    global_model = build_predictor(settings, ledger)
    config = _bandit_config(pseudo_samples)
    start = date(2026, 1, 5)

    async def run() -> dict[str, Any]:
        regret: dict[str, list[float]] = {p: [] for p in POLICIES if p != "oracle"}
        contraction: list[dict[str, float]] = []
        violations = 0
        payout: list[dict[str, Any]] = []
        for seed in range(1, seeds + 1):
            params = ScenarioParams(
                scenario_id=f"eval-{seed}",
                seed=seed,
                n_campaigns=campaigns,
                start=start,
                cold_starts=0,
                shock_rate=0.0,
            )
            results = await compare_policies(
                params, warmup_days=warmup, days=days, config=config, predictor=global_model
            )
            oracle = results["oracle"].expected_conversions
            for policy, result in results.items():
                violations += len(result.violations)
                if policy != "oracle":
                    regret[policy].append(oracle - result.expected_conversions)
            contraction.append(kappa_contraction(results["thompson"]))
            payout.append(
                await payout_error(
                    ScenarioParams(
                        scenario_id=f"payout-{seed}",
                        seed=seed,
                        n_campaigns=campaigns,
                        days=warmup + days,
                        start=start,
                        cold_starts=1,
                    ),
                    config=config,
                    predictor=global_model,
                )
            )
        return {
            "regret": regret,
            "contraction": contraction,
            "violations": violations,
            "payout": payout,
        }

    out = asyncio.run(run())
    tokens = ledger.fetch(
        "SELECT coalesce(sum(tokens_estimated), 0) FROM predictor_calls WHERE status = 'ok'"
    )[0][0]
    ledger.close()
    if as_json:
        click.echo(json.dumps({**out, "tabpfn_tokens": tokens}, indent=2, default=float))
        return
    click.echo(
        f"Closed loop: {seeds} seed(s), {campaigns} campaigns, {warmup} warm-up days, "
        f"{days} bandit days. Regret = oracle's expected conversions minus the policy's."
    )
    click.echo(f"  {'policy':<10} {'mean':>8} {'sd':>7}  per seed")
    for policy, values in out["regret"].items():
        click.echo(
            f"  {policy:<10} {float(np.mean(values)):>8.1f} {float(np.std(values)):>7.1f}  "
            + " ".join(f"{v:.1f}" for v in values)
        )
    keys = ("error_first", "error_last", "sd_first", "sd_last")
    means = {k: float(np.mean([c[k] for c in out["contraction"] if k in c])) for k in keys}
    click.echo(
        f"  thompson kappa2: |error| {means['error_first']:.3f} -> {means['error_last']:.3f}, "
        f"posterior sd {means['sd_first']:.3f} -> {means['sd_last']:.3f} (first -> last decision)"
    )
    click.echo(f"  constraint violations: {out['violations']}")
    model_names: tuple[str, ...] = ("local", "global", "cbs")
    if not any(p[m][g]["n"] for p in out["payout"] for m in p for g in p[m]):
        click.echo("Payout error: the history is too short for a forecast cutoff (needs 42 days).")
        model_names = ()
    else:
        click.echo("Payout error, conversions per day (next 14 days at the spend that happened):")
        click.echo(
            f"  {'model':<8} {'group':<11} {'n':>6} {'bias':>7} {'mae':>7} {'rmse':>7} {'cov80':>6}"
        )
    for model in model_names:
        for group in ("all", "cold_start"):
            rows = [p[model][group] for p in out["payout"] if p[model][group]["n"]]
            if not rows:
                continue
            n = sum(r["n"] for r in rows)

            def pooled(key: str, rows: list[dict[str, Any]] = rows, n: int = n) -> str:
                """Day-weighted across seeds; RMSE pools the squared errors."""
                if key == "rmse":
                    return f"{(sum(r['rmse'] ** 2 * r['n'] for r in rows) / n) ** 0.5:.3f}"
                values = [r[key] * r["n"] for r in rows if r[key] is not None]
                return f"{sum(values) / n:.3f}" if values else "-"

            click.echo(
                f"  {model:<8} {group:<11} {n:>6} {pooled('bias'):>7} {pooled('mae'):>7} "
                f"{pooled('rmse'):>7} {pooled('coverage80'):>6}"
            )
    if tokens:
        click.echo(f"TabPFN tokens used: {tokens}")


# ---------------------------------------------------------------- self-hosted runtime


@main.command()
@click.option("--host", default=None, help="Bind address (default PAID_MEDIA_API_HOST).")
@click.option("--port", type=int, default=None, help="Port (default PAID_MEDIA_API_PORT).")
def serve(host: str | None, port: int | None) -> None:
    """Serve the self-hosted API, and Slack in Socket Mode when it is configured.

    One process owns the DuckDB state file, so the API and the Slack adapter share it here.
    """
    settings = Settings()
    if host is not None:
        settings = settings.model_copy(update={"paid_media_api_host": host})
    if port is not None:
        settings = settings.model_copy(update={"paid_media_api_port": port})
    _configure_logging(settings)
    import uvicorn

    from paid_media_agent.runtime.self_hosted import build_self_hosted_runtime
    from paid_media_agent.scheduler import Scheduler, build_jobs
    from paid_media_agent.store import StoreBusy
    from paid_media_agent.surfaces.api.app import create_app
    from paid_media_agent.surfaces.slack.socket_mode import connect_socket_mode, socket_mode_ready

    try:
        runtime = build_self_hosted_runtime(settings, project_root=project_root())
    except StoreBusy as exc:
        click.echo(f"FAIL {exc}", err=True)
        sys.exit(1)

    scheduler = Scheduler(runtime.store, build_jobs(runtime))

    async def _serve() -> None:
        socket = (
            await connect_socket_mode(settings, runtime) if socket_mode_ready(settings) else None
        )
        jobs = asyncio.create_task(scheduler.run_forever()) if settings.scheduled_jobs() else None
        try:
            config = uvicorn.Config(
                create_app(runtime, scheduler=scheduler),
                host=settings.paid_media_api_host,
                port=settings.paid_media_api_port,
            )
            await uvicorn.Server(config).serve()
        finally:
            if jobs is not None:
                jobs.cancel()
            if socket is not None:
                await socket.close_async()

    try:
        asyncio.run(_serve())
    finally:
        runtime.store.close()


@main.command()
@click.pass_context
def slack(ctx: click.Context) -> None:
    """Alias of `serve`: the Slack adapter runs inside the API process."""
    ctx.invoke(serve)


if __name__ == "__main__":
    main()
