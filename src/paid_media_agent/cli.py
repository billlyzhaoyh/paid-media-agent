"""Command-line entry: setup console, demo, doctor, tests, accounts, reports, history, bandit,
and serve.

Every console action has a subcommand with `--json`, so coding agents and humans share one path.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections import defaultdict
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import click

from paid_media_agent.admin import actions
from paid_media_agent.analytics.history import HISTORY_VIEWS
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


def _visual_demo(settings: Settings, *, open_browser: bool, record: bool, as_json: bool) -> None:
    from paid_media_agent.runtime.self_hosted import state_path
    from paid_media_agent.testing.demo_visual import demo_store, export_recorded, run_visual_demo

    state_dir = state_path(settings, project_root()).parent
    try:
        result = asyncio.run(
            run_visual_demo(settings, state_dir=state_dir, open_browser=open_browser)
        )
        if record:
            store = demo_store(state_dir)
            try:
                result["recorded_calls"] = export_recorded(store)
            finally:
                store.close()
    except Exception as exc:
        raise click.ClickException(f"demo failed: {sanitize_exception(exc)}") from None
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    click.echo(f"Report: {result['html']}" + ("" if open_browser else " (not opened)"))
    pdf = str(result["pdf"])
    click.echo(
        "PDF: rendered beside it"
        if pdf == "rendered"
        else "PDF: not rendered on this machine (the Docker image includes the PDF libraries)"
    )
    for name in ("ranges", "budgets"):
        panel = result[name]
        if panel:
            click.echo(f"{name.capitalize()}: {panel['label']} ({panel['source']})")
    click.echo(f"TabPFN tokens billed in this state file: {result['tabpfn_tokens_billed']:,}")
    click.echo(result["receipt_message"])


@main.command()
@click.option(
    "--with-proposal",
    is_flag=True,
    help="Also run the governed fake-write flow after the analysis.",
)
@click.option(
    "--visual",
    is_flag=True,
    help="Render the report for a simulated account, with expected ranges, budget curves, and "
    "an approved change, and open it in the browser.",
)
@click.option("--no-open", is_flag=True, help="With --visual: write the report, do not open it.")
@click.option(
    "--record-tabpfn",
    is_flag=True,
    hidden=True,
    help="With --visual: save this run's TabPFN answers as the recorded set shipped for replay.",
)
@click.option(
    "--json", "as_json", is_flag=True, help="Print the final message and tool audit as JSON."
)
def demo(
    with_proposal: bool, visual: bool, no_open: bool, record_tabpfn: bool, as_json: bool
) -> None:
    """Run the fixture-backed demo through the real agent loop with no network or secrets."""
    settings = Settings(paid_media_model="scripted:demo", paid_media_allow_self_approval=True)
    _configure_logging(settings)
    if visual:
        _visual_demo(settings, open_browser=not no_open, record=record_tabpfn, as_json=as_json)
        return
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
@click.option(
    "--live",
    is_flag=True,
    help="Also read each account through its live tools and check the data path end to end.",
)
@click.option("--alias", "aliases", multiple=True, help="With --live: account alias. Repeatable.")
def doctor(as_json: bool, live: bool, aliases: tuple[str, ...]) -> None:
    """Diagnose configuration without printing secret values."""
    from paid_media_agent.doctor import format_checks, run_doctor, usage_check

    root = project_root()
    if as_json and not live:
        _emit(actions.status(root), True)
        return
    settings = actions.load_settings(root)
    checks = run_doctor(settings, project_root=root)
    checks.append(usage_check(settings, project_root=root))
    if live:
        checks += _live_checks(settings, aliases)
    if as_json:
        click.echo(json.dumps({"checks": [c.__dict__ for c in checks]}, indent=2))
    else:
        click.echo(format_checks(checks))
    if any(not c.ok for c in checks):
        sys.exit(1)


def _live_checks(settings: Settings, aliases: tuple[str, ...]) -> list[Any]:
    from paid_media_agent.analytics.live_check import run_live_checks
    from paid_media_agent.runtime.self_hosted import build_self_hosted_runtime
    from paid_media_agent.store import Store, StoreBusy

    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        # `serve` holds the state file; check against a throwaway store instead.
        runtime = build_self_hosted_runtime(settings, project_root=project_root(), store=Store())
    try:
        return asyncio.run(run_live_checks(runtime, aliases=aliases))
    finally:
        runtime.store.close()


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
@click.option(
    "--insights",
    is_flag=True,
    help="Add the expected-range and budget-curve panels from stored history, for the first "
    "account in scope, with the configured predictor (the local model by default).",
)
@click.option("--json", "as_json", is_flag=True)
def report(
    cadence: str,
    end_date: str | None,
    aliases: tuple[str, ...],
    no_render: bool,
    insights: bool,
    as_json: bool,
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

        async def panels() -> Any:
            from paid_media_agent.predict.factory import build_predictor
            from paid_media_agent.reports.insights import build_insights

            registry = runtime.profile.accounts
            alias = (aliases or registry.aliases())[0]
            binding = registry.resolve(alias)
            return await build_insights(
                runtime.profile.store,
                build_predictor(settings, runtime.profile.store),
                account_alias=alias,
                as_of=end + timedelta(days=1),
                window_days=7 if cadence == "weekly" else 14,
                currency=binding.currency if binding else None,
            )

        return await run_cadence_report(
            cadence=cadence,  # type: ignore[arg-type]
            end=end,
            accounts=runtime.profile.accounts,
            catalog=runtime.catalog,
            dispatcher=runtime.components.read_dispatcher,
            artifacts=runtime.profile.artifacts,
            aliases=aliases or None,
            render=not no_render,
            insights=panels if insights else None,
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


def _server_request(
    settings: Settings, method: str, path: str, body: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Call the `serve` process that holds the state file, as the first API token's caller."""
    import httpx

    logging.getLogger("httpx").setLevel(logging.WARNING)
    tokens = settings.api_token_map()
    if not tokens:
        raise click.ClickException(
            "this needs the running `serve` API and PAID_MEDIA_API_TOKENS is not set. "
            "Configure an API token (or stop `serve` where the command can run on its own)."
        )
    host = settings.paid_media_api_host
    if host in ("0.0.0.0", "::", ""):  # noqa: S104 - a bind-all address, not a destination
        host = "127.0.0.1"
    try:
        response = httpx.request(
            method,
            f"http://{host}:{settings.paid_media_api_port}{path}",
            headers={"Authorization": f"Bearer {next(iter(tokens))}"},
            json=body,
            timeout=900,
        )
    except httpx.HTTPError as exc:
        raise click.ClickException(
            f"could not reach `serve` ({type(exc).__name__}); is it running?"
        ) from None
    if response.status_code != 200:
        detail = ""
        try:
            detail = str(response.json().get("detail", ""))[:300]
        except ValueError:
            pass
        raise click.ClickException(
            f"`serve` refused: HTTP {response.status_code}" + (f" ({detail})" if detail else "")
        )
    result: dict[str, Any] = response.json()
    return result


def _server_job(settings: Settings, name: str) -> dict[str, Any]:
    """Run a job inside the `serve` process that holds the state file, over its API."""
    return _server_request(settings, "POST", f"/jobs/{name}")


def _state_runtime(settings: Settings) -> Any:
    from paid_media_agent.runtime.self_hosted import build_self_hosted_runtime

    return build_self_hosted_runtime(settings, project_root=project_root())


def _echo_sync(summary: dict[str, Any], as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(summary, indent=2, default=str))
        return
    click.echo(
        f"{summary['source']} {summary['start']} to {summary['end']}: {summary['rows']} "
        f"entity-days, {summary['settings']} campaign settings, {len(summary['reads'])} reads"
        f" ({summary.get('calls', len(summary['reads']))} provider calls)."
    )
    for alias, contract in (summary.get("contracts") or {}).items():
        if not contract.startswith("rows "):
            click.echo(f"  {alias}: read contract {contract}")
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
    end = _day(end_date)  # None: each account's own yesterday
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
                max_calls=settings.paid_media_sync_max_calls,
            )
        )
    finally:
        runtime.store.close()
    _echo_sync(result.summary(), as_json)
    if not result.reads:
        sys.exit(1)


@main.command()
@click.option("--out", "out_dir", default=None, help="Backup folder (default: state/backups).")
def backup(out_dir: str | None) -> None:
    """Export the state file as Parquet; works while `serve` runs (through its API)."""
    from paid_media_agent.store import StoreBusy
    from paid_media_agent.store.backup import backup_state, backups_root

    settings = Settings()
    _configure_logging(settings)
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        runtime = None
    if runtime is None:
        if out_dir:
            raise click.ClickException(
                "`serve` holds the state file; run `backup` without --out to use its job"
            )
        run = _server_job(settings, "backup")
        if run["status"] != "ok":
            raise click.ClickException(f"backup failed in `serve`: {run['detail']}")
        click.echo(f"Backed up to {run['detail']['backup']}.")
        return
    try:
        root = Path(out_dir) if out_dir else backups_root(runtime.store)
        target, removed = backup_state(runtime.store, root)
    finally:
        runtime.store.close()
    click.echo(f"Backed up to {target}." + (f" Removed {len(removed)} older." if removed else ""))


@main.command()
@click.argument("backup_dir", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option(
    "--to", "target", required=True, type=click.Path(path_type=Path), help="New state file."
)
def restore(backup_dir: Path, target: Path) -> None:
    """Build a new state file from a backup. Never overwrites; point PAID_MEDIA_STATE_PATH at it."""
    from paid_media_agent.store.backup import restore_backup

    try:
        restored = restore_backup(backup_dir, target)
    except (FileExistsError, FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from None
    click.echo(f"Restored into {restored}. Set PAID_MEDIA_STATE_PATH={restored} to use it.")


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
                max_calls=settings.paid_media_sync_max_calls,
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
    "outcomes": (
        "decision_day",
        "account_alias",
        "entity_ref",
        "outcome",
        "current_budget",
        "recommended_budget",
        "budget_in_force",
        "days_matured",
        "conversions_matured",
        "expected_conversions_window",
    ),
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
    type=click.Choice(list(HISTORY_VIEWS)),
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
    """Show stored history: coverage, daily panel, settings, changes, lag, or bandit outcomes."""
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
    """Evaluate the budget bandit on simulated accounts (`allocate` runs it on real ones)."""


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


@bandit.command("whatif-eval")
@click.option(
    "--scenario",
    type=click.Choice(["default", "constrained"]),
    default="default",
    show_default=True,
)
@click.option("--seeds", type=click.IntRange(1, 50), default=5, show_default=True)
@click.option("--campaigns", type=click.IntRange(2, 30), default=6, show_default=True)
@click.option("--cutoff", type=click.IntRange(35, 365), default=90, show_default=True)
@click.option("--vectors", type=click.IntRange(1, 200), default=12, show_default=True)
@click.option(
    "--spend-model",
    type=click.Choice(["ceiling", "linear"]),
    default="ceiling",
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True)
def bandit_whatif_eval(
    scenario: str,
    seeds: int,
    campaigns: int,
    cutoff: int,
    vectors: int,
    spend_model: str,
    as_json: bool,
) -> None:
    """How close what-if forecasts come to the simulated truth, on random budget scenarios."""
    from paid_media_agent.bandit.evaluate import whatif_calibration
    from paid_media_agent.bandit.recommend import BanditConfig
    from paid_media_agent.sim.simulator import ScenarioParams

    preset: dict[str, Any] = (
        {"ceiling_share": 0.5, "target_share": 0.5, "emit_signals": True}
        if scenario == "constrained"
        else {}
    )

    async def run() -> list[dict[str, Any]]:
        rows = []
        for seed in range(1, seeds + 1):
            params = ScenarioParams(
                scenario_id=f"whatif-{seed}",
                seed=seed,
                n_campaigns=campaigns,
                days=cutoff + 7,
                start=date(2026, 1, 5),
                **preset,
            )
            cal = await whatif_calibration(
                params,
                cutoff=cutoff,
                vectors=vectors,
                seed=seed,
                config=BanditConfig(spend_model=spend_model),  # type: ignore[arg-type]
            )
            rows.append({"seed": seed, **cal.summary()})
        return rows

    rows = asyncio.run(run())
    keys = (
        "bias",
        "mae",
        "change_mae",
        "direction",
        "coverage80",
        "change_coverage80",
        "spend_mae",
    )
    means = {
        k: round(sum(float(r[k]) for r in rows if r.get(k) is not None) / len(rows), 3)
        for k in keys
    }
    if as_json:
        click.echo(json.dumps({"seeds": rows, "mean": means}, indent=2))
        return
    click.echo(f"what-if forecasts against the truth, {scenario} scenario, {spend_model} spend:")
    click.echo("seed  " + "  ".join(f"{k:>17}" for k in keys))
    for r in rows:
        click.echo(f"{r['seed']:>4}  " + "  ".join(f"{str(r.get(k)):>17}" for k in keys))
    click.echo("mean  " + "  ".join(f"{means[k]:>17}" for k in keys))
    click.echo(
        "mae: account conversions; change_mae: error in the change, as a share of today's "
        "conversions; direction: share of material changes called the right way."
    )


@bandit.command("evaluate")
@click.option("--seeds", type=click.IntRange(1, 50), default=3, show_default=True)
@click.option("--campaigns", type=click.IntRange(2, 30), default=5, show_default=True)
@click.option("--warmup", type=click.IntRange(28, 365), default=42, show_default=True)
@click.option("--days", type=click.IntRange(7, 365), default=60, show_default=True)
@click.option("--pseudo-samples", type=click.IntRange(0, 4096), default=512, show_default=True)
@click.option(
    "--scenario",
    type=click.Choice(["default", "constrained"]),
    default="default",
    show_default=True,
    help="constrained: half the campaigns stop at a spend ceiling (demand or a bid target).",
)
@click.option(
    "--signals",
    is_flag=True,
    help="With --scenario constrained: also report Google-style impression-share signals.",
)
@click.option(
    "--spend-model",
    type=click.Choice(["ceiling", "linear", "both"]),
    default="ceiling",
    show_default=True,
    help="How the bandit models spend; both compares them on the same accounts.",
)
@_PREDICTOR
@click.option("--json", "as_json", is_flag=True)
def bandit_evaluate(
    seeds: int,
    campaigns: int,
    warmup: int,
    days: int,
    pseudo_samples: int,
    scenario: str,
    signals: bool,
    spend_model: str,
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

    models = ("linear", "ceiling") if spend_model == "both" else (spend_model,)
    preset: dict[str, Any] = (
        {"ceiling_share": 0.5, "target_share": 0.5, "emit_signals": signals}
        if scenario == "constrained"
        else {}
    )

    async def run() -> dict[str, Any]:
        regret: dict[str, list[float]] = defaultdict(list)
        unspendable: dict[str, list[float]] = defaultdict(list)
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
                **preset,
            )
            for i, model in enumerate(models):
                # Baselines don't depend on the spend model; run them once.
                policies = POLICIES if i == 0 else ("oracle", "greedy", "thompson")
                results = await compare_policies(
                    params,
                    warmup_days=warmup,
                    days=days,
                    config=replace(config, spend_model=model),
                    predictor=global_model,
                    policies=policies,
                )
                oracle = results["oracle"].expected_conversions
                for policy, result in results.items():
                    violations += len(result.violations)
                    if policy == "oracle":
                        continue
                    key = (
                        policy
                        if len(models) == 1 or policy in ("static", "cpa_rule")
                        else f"{policy}/{model}"
                    )
                    regret[key].append(oracle - result.expected_conversions)
                    unspendable[key].append(result.unspendable)
                if model == models[-1]:
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
            "scenario": scenario,
            "spend_models": list(models),
            "regret": dict(regret),
            "unspendable": dict(unspendable),
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
    if scenario == "constrained":
        click.echo(
            "Scenario: half the campaigns stop at a spend ceiling (demand or a bid target)"
            + (", with impression-share signals." if signals else ", from spend history only.")
            + " Unspendable = budget they could not spend, summed over the bandit days."
        )
    click.echo(f"  {'policy':<18} {'mean':>8} {'sd':>7} {'unspendable':>12}  per seed")
    for policy, values in out["regret"].items():
        wasted = float(np.mean(out["unspendable"].get(policy, [0.0])))
        click.echo(
            f"  {policy:<18} {float(np.mean(values)):>8.1f} {float(np.std(values)):>7.1f} "
            f"{wasted:>12,.0f}  " + " ".join(f"{v:.1f}" for v in values)
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


# ---------------------------------------------------------------- budget recommendations


@main.command()
@click.option("--alias", default=None, help="Account alias; default every configured account.")
@click.option(
    "--total",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="Total daily budget to split (default: the campaigns' current total).",
)
@click.option(
    "--policy",
    type=click.Choice(["thompson", "greedy"]),
    default=None,
    help="Override PAID_MEDIA_BANDIT_POLICY.",
)
@_PREDICTOR
@click.option(
    "--propose",
    is_flag=True,
    help="Turn the moves into proposals awaiting approval (nothing is applied).",
)
@click.option("--json", "as_json", is_flag=True)
def allocate(
    alias: str | None,
    total: float | None,
    policy: str | None,
    predictor: str | None,
    propose: bool,
    as_json: bool,
) -> None:
    """Recommend how to split each account's daily budget across its campaigns."""
    from paid_media_agent.bandit.live import allocate_accounts, live_config
    from paid_media_agent.predict.factory import build_predictor
    from paid_media_agent.store import StoreBusy
    from paid_media_agent.tools.bandit import budget_reading

    settings = Settings()
    _configure_logging(settings)
    if predictor is not None:
        settings = settings.model_copy(update={"paid_media_predictor": predictor})
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        raise click.ClickException(
            "`serve` holds the state file. Ask the agent (recommend_budgets), or run the job for "
            "every account with POST /jobs/allocate (it proposes only when "
            "PAID_MEDIA_BANDIT_PROPOSE=true)."
        ) from None
    try:
        aliases = runtime.profile.accounts.aliases()
        if alias is not None:
            if runtime.profile.accounts.resolve(alias) is None:
                raise click.BadParameter(f"unknown account alias {alias}", param_hint="--alias")
            aliases = (alias,)
        runs = asyncio.run(
            allocate_accounts(
                runtime.store,
                build_predictor(settings, runtime.store),
                aliases=aliases,
                accounts=runtime.profile.accounts,
                config=live_config(policy or settings.paid_media_bandit_policy),  # type: ignore[arg-type]
                total_budget=total,
                service=runtime.components.proposal_service,
                propose=propose,
                min_change=settings.paid_media_bandit_min_change,
            )
        )
    finally:
        runtime.store.close()
    if as_json:
        click.echo(json.dumps(runs, indent=2, default=str))
        return
    for run in runs:
        if "error" in run:
            click.echo(f"{run['account_alias']}: {run['error']}")
            continue
        currency = run["currency"]
        unit = f" {currency}" if currency else ""
        # What the budgets add up to; the aim can be larger than step limits and ceilings allow.
        placed = sum(float(d["final_budget"] or 0) for d in run["decisions"] if d["eligible"])
        aimed = float(run["total_budget"])
        aim = "" if abs(aimed - placed) <= 0.005 * max(aimed, 1.0) else f" (aimed for {aimed:.2f})"
        click.echo(
            f"{run['account_alias']} on {run['decision_day']}: budgets total {placed:.2f}{unit}"
            + f" a day{aim}, {run['policy']}, global model {run['prior_source']}"
            + (", LAST GOOD CURVES (data checks failed)" if run["fallback_used"] else "")
        )
        if run.get("expected_cpa") is not None:
            goal_cpa = (run.get("goals") or {}).get("target_cpa")
            click.echo(
                f"  expected CPA {run['expected_cpa']:.2f}"
                + (f" against a {goal_cpa:.2f} target" if goal_cpa else "")
                + (
                    (
                        " (total cut to meet it)"
                        if run.get("target_cpa_reached")
                        else " (total cut as far as the step limits allow; not reached)"
                    )
                    if run.get("capped_by_target_cpa")
                    else ""
                )
            )
        for row in run["decisions"]:
            click.echo(f"  {budget_reading(row, currency)}")
        for note in run["notes"]:
            click.echo(f"  note: {note}")
        proposed = run.get("proposals")
        if proposed:
            for ref, pid in proposed["proposals"].items():
                click.echo(f"  proposed {ref}: {pid} (awaiting approval)")
            for ref, why in proposed["skipped"].items():
                click.echo(f"  not proposed {ref}: {why}")
            if proposed["superseded"]:
                click.echo(f"  superseded {len(proposed['superseded'])} older proposal(s)")
    if propose:
        click.echo("Review with `paid-media-agent proposals list`; approve through the API.")


@main.group()
def proposals() -> None:
    """Proposals awaiting approval, including the budget bandit's."""


def _proposal_line(p: dict[str, Any]) -> str:
    before = ", ".join(f"{f['field']}={f['value']}" for f in p["before"]) or "-"
    after = ", ".join(f"{f['field']}={f['value']}" for f in p["after"])
    flags = f" [{', '.join(p['risk_flags'])}]" if p.get("risk_flags") else ""
    return (
        f"{p['proposal_id']}  {p['account_ref']} {p['target_ref']}: {before} -> {after}"
        f"{flags}\n    by {p['requester_ref']}: {p['reason'][:160]}"
    )


@proposals.command("list")
@click.option("--limit", type=click.IntRange(1, 200), default=50, show_default=True)
@click.option("--json", "as_json", is_flag=True)
def proposals_list(limit: int, as_json: bool) -> None:
    """Proposals awaiting a decision, oldest first."""
    from paid_media_agent.domain.presentation import ProposalView
    from paid_media_agent.runtime.self_hosted import state_path
    from paid_media_agent.store import Store, StoreBusy

    settings = Settings()
    try:
        store = Store(state_path(settings, project_root()))
    except StoreBusy:
        listed = _server_request(settings, "GET", f"/proposals?limit={limit}")["proposals"]
    else:
        try:
            records = store.repositories.proposals.list_awaiting(limit)
            listed = [ProposalView.from_record(r).model_dump(mode="json") for r in records]
        finally:
            store.close()
    if as_json:
        click.echo(json.dumps({"proposals": listed}, indent=2))
        return
    if not listed:
        click.echo("No proposals awaiting approval.")
    for p in listed:
        click.echo(_proposal_line(p))


@proposals.command("approve")
@click.argument("proposal_id")
@click.option("--json", "as_json", is_flag=True)
def proposals_approve(proposal_id: str, as_json: bool) -> None:
    """Approve a proposal through the running API; the change is applied once and read back."""
    settings = Settings()
    outcome = _server_request(settings, "POST", f"/proposals/{proposal_id}/approve")
    if as_json:
        click.echo(json.dumps(outcome, indent=2))
        return
    receipt = outcome.get("receipt") or {}
    click.echo(outcome.get("text") or "Approved.")
    if receipt:
        click.echo(f"  receipt: {receipt.get('status')} ({receipt.get('reason', '')})")


@proposals.command("reject")
@click.argument("proposal_id")
@click.option("--message", default="", help="Why, for the change log.")
def proposals_reject(proposal_id: str, message: str) -> None:
    """Reject a proposal through the running API."""
    settings = Settings()
    outcome = _server_request(
        settings, "POST", f"/proposals/{proposal_id}/reject", {"message": message}
    )
    click.echo(outcome.get("text") or "Rejected.")


# ---------------------------------------------------------------- goals and pacing


def _goal_line(goal: dict[str, Any] | None) -> str:
    if not goal:
        return "no goals set"
    parts = [
        f"{label} {goal[key]:,.2f}"
        for key, label in (
            ("target_cpa", "target CPA"),
            ("target_roas", "target ROAS"),
            ("monthly_budget", "monthly budget"),
        )
        if goal.get(key) is not None
    ]
    return (", ".join(parts) or "all goals cleared") + f" (from {goal['effective_from']})"


@main.group()
def goals() -> None:
    """Each account's target CPA or ROAS and monthly budget, versioned by date."""


@goals.command("set")
@click.option("--alias", required=True, help="Account alias.")
@click.option("--target-cpa", type=click.FloatRange(min=0, min_open=True), default=None)
@click.option("--target-roas", type=click.FloatRange(min=0, min_open=True), default=None)
@click.option(
    "--monthly-budget",
    type=click.FloatRange(min=0, min_open=True),
    default=None,
    help="In the account's currency, per calendar month.",
)
@click.option(
    "--clear",
    multiple=True,
    type=click.Choice(["target_cpa", "target_roas", "monthly_budget"]),
    help="Remove a goal. Repeatable.",
)
@click.option("--from", "from_date", default=None, help="First day it applies. Default: today.")
@click.option("--notes", default=None, help="Why, for the record.")
def goals_set(
    alias: str,
    target_cpa: float | None,
    target_roas: float | None,
    monthly_budget: float | None,
    clear: tuple[str, ...],
    from_date: str | None,
    notes: str | None,
) -> None:
    """Set goals directly. Unchanged goals carry over; the agent can only propose changes."""
    from paid_media_agent.analytics.goals import GoalError, update_goals
    from paid_media_agent.store import StoreBusy

    settings = Settings()
    body: dict[str, Any] = {
        "account_alias": alias,
        "target_cpa": target_cpa,
        "target_roas": target_roas,
        "monthly_budget": monthly_budget,
        "clear": list(clear),
        "effective_from": from_date,
        "notes": notes,
    }
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        goal = _server_request(settings, "POST", "/goals", body)["goal"]
    else:
        try:
            goal = update_goals(
                runtime.store,
                runtime.profile.accounts,
                alias,
                values={k: body[k] for k in ("target_cpa", "target_roas", "monthly_budget")},
                clear=clear,
                effective_from=_day(from_date),
                source="cli",
                **({"notes": notes} if notes is not None else {}),
            ).as_json()
        except GoalError as exc:
            raise click.ClickException(str(exc)) from None
        finally:
            runtime.store.close()
    click.echo(f"{alias}: {_goal_line(goal)}.")


@goals.command("show")
@click.option("--alias", default=None, help="Account alias; default every account.")
@click.option("--json", "as_json", is_flag=True)
def goals_show(alias: str | None, as_json: bool) -> None:
    """The goals in force today, per account, and their history."""
    from paid_media_agent.analytics.goals import GoalStore, account_today
    from paid_media_agent.store import StoreBusy

    settings = Settings()
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        query = f"?alias={alias}" if alias else ""
        listed = _server_request(settings, "GET", f"/goals{query}")["goals"]
    else:
        try:
            accounts = runtime.profile.accounts
            if alias and accounts.resolve(alias) is None:
                raise click.BadParameter(f"unknown account alias {alias}", param_hint="--alias")
            store = GoalStore(runtime.store)
            listed = []
            for a in [alias] if alias else list(accounts.aliases()):
                current = store.current(a, account_today(accounts, a))
                listed.append(
                    {
                        "account_alias": a,
                        "current": current.as_json() if current else None,
                        "history": [g.as_json() for g in store.history(a)],
                    }
                )
        finally:
            runtime.store.close()
    if as_json:
        click.echo(json.dumps({"goals": listed}, indent=2, default=str))
        return
    for entry in listed:
        click.echo(f"{entry['account_alias']}: {_goal_line(entry['current'])}")


@main.command()
@click.option("--alias", default=None, help="Account alias; default every account.")
@click.option("--json", "as_json", is_flag=True)
def pacing(alias: str | None, as_json: bool) -> None:
    """Month-to-date spend against the monthly budget, and CPA/ROAS against targets."""
    from paid_media_agent.analytics.pacing import account_pacing
    from paid_media_agent.store import StoreBusy

    settings = Settings()
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        query = f"?alias={alias}" if alias else ""
        reports = _server_request(settings, "GET", f"/pacing{query}")["accounts"]
    else:
        try:
            accounts = runtime.profile.accounts
            if alias and accounts.resolve(alias) is None:
                raise click.BadParameter(f"unknown account alias {alias}", param_hint="--alias")
            reports = [
                account_pacing(runtime.store, accounts, a).as_json()
                for a in ([alias] if alias else accounts.aliases())
            ]
        finally:
            runtime.store.close()
    if as_json:
        click.echo(json.dumps({"accounts": reports}, indent=2, default=str))
        return
    for report in reports:
        click.echo(f"{report['account_alias']}: {report['reading']}")
        for note in report["notes"]:
            click.echo(f"  note: {note}")


def _span(value: str | None, name: str) -> tuple[date | None, date | None]:
    if value is None:
        return None, None
    start, sep, end = value.partition(":")
    if not sep:
        raise click.BadParameter("use START:END (YYYY-MM-DD:YYYY-MM-DD)", param_hint=name)
    return _day(start), _day(end)


@main.command()
@click.option("--alias", "aliases", multiple=True, help="Account alias. Repeatable; default all.")
@click.option(
    "--metric", type=click.Choice(["cpa", "conversions", "roas"]), default="cpa", show_default=True
)
@click.option("--current", default=None, help="START:END; default the newest 7 days of data.")
@click.option("--previous", default=None, help="START:END; default the days just before.")
@_PREDICTOR
@click.option("--json", "as_json", is_flag=True)
def explain(
    aliases: tuple[str, ...],
    metric: str,
    current: str | None,
    previous: str | None,
    predictor: str | None,
    as_json: bool,
) -> None:
    """Why CPA, conversions or ROAS changed: spend mix, funnel rates, diminishing returns."""
    from paid_media_agent.analytics.drivers import explain_accounts
    from paid_media_agent.bandit.live import live_config
    from paid_media_agent.predict.factory import build_predictor
    from paid_media_agent.store import StoreBusy

    settings = Settings()
    if predictor is not None:
        settings = settings.model_copy(update={"paid_media_predictor": predictor})
    current_start, current_end = _span(current, "--current")
    previous_start, previous_end = _span(previous, "--previous")
    windows = {
        "current_start": current_start,
        "current_end": current_end,
        "previous_start": previous_start,
        "previous_end": previous_end,
    }
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        query = "&".join(
            [f"alias={a}" for a in aliases]
            + [f"metric={metric}"]
            + [f"{k}={v.isoformat()}" for k, v in windows.items() if v is not None]
        )
        reports = _server_request(settings, "GET", f"/explain?{query}")["reports"]
    else:
        try:
            accounts = runtime.profile.accounts
            for alias in aliases:
                if accounts.resolve(alias) is None:
                    raise click.BadParameter(f"unknown account alias {alias}", param_hint="--alias")
            found = asyncio.run(
                explain_accounts(
                    runtime.store,
                    accounts,
                    list(aliases) or None,
                    metric=metric,  # type: ignore[arg-type]
                    predictor=build_predictor(settings, runtime.store),
                    config=live_config(settings.paid_media_bandit_policy),
                    **windows,
                )
            )
            reports = [r.as_json() for r in found]
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        finally:
            runtime.store.close()
    if as_json:
        click.echo(json.dumps({"reports": reports}, indent=2, default=str))
        return
    for report in reports:
        click.echo(f"{', '.join(report['accounts'])}: {report['reading']}")
        for driver in report["drivers"]:
            click.echo(f"  {driver['reading']}")
        for note in report["notes"]:
            click.echo(f"  note: {note}")


def _budget_value(value: str, name: str) -> tuple[float | None, float | None]:
    """'+20%' or '-30%' is a relative change; a plain number is an amount."""
    text = value.strip()
    try:
        if text.endswith("%"):
            return None, float(text[:-1]) / 100
        return float(text), None
    except ValueError:
        raise click.BadParameter(
            f"{value} is not an amount or a percentage", param_hint=name
        ) from None


@main.command()
@click.option("--alias", required=True, help="Account alias.")
@click.option(
    "--set",
    "sets",
    multiple=True,
    help="CAMPAIGN=150 (new daily budget) or CAMPAIGN=+20% (relative). Repeatable.",
)
@click.option("--total", default=None, help="New account total: 1500 a day, or +10%.")
@click.option("--split", type=click.Choice(["proportional", "best"]), default="proportional")
@click.option("--days", "horizon", type=click.IntRange(1, 92), default=7, show_default=True)
@_PREDICTOR
@click.option("--json", "as_json", is_flag=True)
def whatif(
    alias: str,
    sets: tuple[str, ...],
    total: str | None,
    split: str,
    horizon: int,
    predictor: str | None,
    as_json: bool,
) -> None:
    """Forecast spend, conversions and CPA for different daily budgets; nothing changes."""
    from paid_media_agent.bandit.live import live_config
    from paid_media_agent.bandit.whatif import what_if_account
    from paid_media_agent.predict.factory import build_predictor
    from paid_media_agent.store import StoreBusy
    from paid_media_agent.tools.whatif import CampaignBudgetChange, WhatIfBudgetsArgs

    settings = Settings()
    if predictor is not None:
        settings = settings.model_copy(update={"paid_media_predictor": predictor})
    changes = []
    for item in sets:
        campaign, sep, value = item.partition("=")
        if not sep or not campaign:
            raise click.BadParameter("use CAMPAIGN=AMOUNT or CAMPAIGN=+N%", param_hint="--set")
        budget, change = _budget_value(value, "--set")
        changes.append(
            CampaignBudgetChange(campaign=campaign.strip(), budget=budget, change=change)
        )
    total_budget, total_change = _budget_value(total, "--total") if total else (None, None)
    try:
        args = WhatIfBudgetsArgs(
            account_alias=alias,
            changes=changes or None,
            total_daily_budget=total_budget,
            total_change=total_change,
            split=split,
            horizon_days=horizon,
        )
    except ValueError as exc:
        raise click.ClickException(sanitize_exception(exc)) from None
    try:
        runtime = _state_runtime(settings)
    except StoreBusy:
        result = _server_request(settings, "POST", "/what-if", args.model_dump(mode="json"))
    else:
        try:
            if runtime.profile.accounts.resolve(alias) is None:
                raise click.BadParameter(f"unknown account alias {alias}", param_hint="--alias")
            report = asyncio.run(
                what_if_account(
                    runtime.store,
                    runtime.profile.accounts,
                    build_predictor(settings, runtime.store),
                    alias,
                    args.scenario(),
                    config=live_config(settings.paid_media_bandit_policy),
                )
            )
            result = report.as_json()
        except ValueError as exc:
            raise click.ClickException(str(exc)) from None
        finally:
            runtime.store.close()
    if as_json:
        click.echo(json.dumps(result, indent=2, default=str))
        return
    click.echo(f"{alias}: {result['reading']}")
    for row in result["campaigns"]:
        if abs((row["budget"] or 0) - (row["budget_now"] or 0)) < 0.01 and not row["notes"]:
            continue
        moved: dict[str, float] = row["conversions_change"] or {}
        click.echo(
            f"  {row['campaign']}: budget {row['budget_now']:.2f} -> {row['budget']:.2f}, "
            f"conversions {moved.get('mean', 0):+.2f} a day"
            + ("".join(f"; {n}" for n in row["notes"]))
        )
    for note in result["notes"]:
        click.echo(f"  note: {note}")


@main.command()
@click.option("--days", type=click.IntRange(1, 365), default=7, show_default=True)
@click.option(
    "--by",
    type=click.Choice(["model", "day", "thread", "purpose"]),
    default="model",
    show_default=True,
)
@click.option("--json", "as_json", is_flag=True)
def usage(days: int, by: str, as_json: bool) -> None:
    """Model calls: tokens, cache hits, reported cost, failures, and latency."""
    from datetime import UTC, datetime

    from paid_media_agent.harness.usage import usage_summary
    from paid_media_agent.runtime.self_hosted import state_path
    from paid_media_agent.store import Store, StoreBusy

    settings = Settings()
    since = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=days)
    try:
        store = Store(state_path(settings, project_root()))
    except StoreBusy:
        raise click.ClickException(
            "`serve` holds the state file; stop it or read usage there later"
        ) from None
    try:
        summary = usage_summary(store, since=since, by=by)  # type: ignore[arg-type]
    finally:
        store.close()
    if as_json:
        click.echo(json.dumps(summary, indent=2, default=str))
        return
    click.echo(f"Model calls in the last {days} days, by {by}:")
    for row in [*summary["rows"], summary["totals"]]:
        cost = "not reported" if row["cost_usd"] is None else f"${row['cost_usd']:.4f}"
        if row["cost_usd"] is not None and row["costed_calls"] < row["calls"]:
            cost += f" ({row['costed_calls']} of {row['calls']} calls reported cost)"
        hit = "n/a" if row["cache_hit_rate"] is None else f"{row['cache_hit_rate']:.0%}"
        click.echo(
            f"  {row['key']}: {row['calls']} calls ({row['failed']} failed), "
            f"{row['input_tokens']:,} in ({hit} cached), {row['output_tokens']:,} out, {cost}, "
            f"latency p50 {row['p50_latency_ms']} ms / p95 {row['p95_latency_ms']} ms"
        )


@main.group("eval")
def eval_group() -> None:
    """The question eval on sample accounts: checks and a judge model, stored with a baseline."""


def _eval_store(path: str | None) -> Any:
    from paid_media_agent.evals.store import DEFAULT_PATH, EvalStore
    from paid_media_agent.store import StoreBusy

    target = Path(path) if path else project_root() / DEFAULT_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        return EvalStore(target)
    except StoreBusy:
        raise click.ClickException(
            f"{target} is in use (an eval run holds it); try again when it finishes"
        ) from None


def _eval_model(spec: str, settings: Settings, *, rpm: int) -> Any:
    from paid_media_agent.config import ModelConfig
    from paid_media_agent.evals.runner import Throttled
    from paid_media_agent.harness.models import cache_session, resolve_model

    same = spec == settings.paid_media_model
    model = resolve_model(
        ModelConfig.parse(spec, base_url=settings.paid_media_model_base_url if same else None),
        api_key_env=settings.paid_media_model_api_key_env if same else None,
        timeout_seconds=settings.paid_media_model_timeout_seconds,
        zero_data_retention=settings.paid_media_model_zero_data_retention,
        prompt_cache=settings.paid_media_prompt_cache,
        # One session per model and day: every question of a run shares the cached prefix.
        session_id=cache_session(f"eval:{spec}:{date.today().isoformat()}"),
    )
    return Throttled(model, rpm)


def _echo_eval(run: dict[str, Any], results: list[dict[str, Any]], against: Any) -> None:
    from paid_media_agent.evals.report import base_id, by_question, compare, totals, why

    t = totals(results)
    aborted = (run.get("totals") or {}).get("aborted")
    if aborted:
        click.echo(f"RUN INCOMPLETE: {aborted}; graded questions below are the ones before it.")
    click.echo(
        f"run {str(run['run_id'])[:8]} · {run['model']} · judge {run['judge_model'] or 'none'} · "
        f"anchor {run['anchor']} · git {run['git_sha'] or '?'}"
    )
    repeated = any("#" in r["question_id"] for r in results)
    if repeated:
        for question, (passed, attempts) in sorted(by_question(results).items()):
            failures = [
                r for r in results if base_id(r["question_id"]) == question and not r["passed"]
            ]
            line = f"  {passed}/{attempts} {question:22}"
            if failures:
                line += f"  {why(failures[0])}"
            click.echo(line)
    for r in [] if repeated else results:
        mark = "PASS" if r["passed"] else "FAIL"
        cost = f"${r['cost_usd']:.4f}" if r.get("cost_usd") is not None else "cost n/a"
        line = f"  {mark} {r['question_id']:22} {r.get('seconds') or 0:6.1f}s {cost}"
        if r.get("input_tokens"):
            # Calls and how much of their input came from, or went into, the prompt cache.
            line += (
                f" {r.get('model_calls') or 0}c {(r.get('cached_tokens') or 0) / r['input_tokens']:.0%}"
                f"/{(r.get('cache_write_tokens') or 0) / r['input_tokens']:.0%}w"
            )
        if not r["passed"]:
            line += f"  {why(r)}"
        click.echo(line)
    cost = "n/a" if t["cost_usd"] is None else f"${t['cost_usd']:.2f}"
    judge_cost = "" if t["judge_cost_usd"] is None else f" + judge ${t['judge_cost_usd']:.2f}"
    hit = "n/a" if t["cache_hit_rate"] is None else f"{t['cache_hit_rate']:.0%}"
    if t.get("cache_write_rate") is not None:
        hit += f" ({t['cache_write_rate']:.0%} written)"
    if repeated:
        click.echo(
            f"questions passed on a majority of attempts: {t['questions_passed']}/{t['questions']}"
        )
    click.echo(
        f"passed {t['passed']}/{t['attempts']} attempts ({(t['pass_rate'] or 0):.0%}); judge passed "
        f"{t['judge_passed']}/{t['judged']}"
        + (f" ({t['judge_skipped']} not judged: a check failed)" if t.get("judge_skipped") else "")
        + (f"; {t['repaired']} answers repaired once" if t.get("repaired") else "")
        + f"; cost {cost}{judge_cost}; {t['model_calls']} model "
        f"calls, {hit} of input from cache; p50 {t['p50_seconds']}s, max {t['max_seconds']}s"
    )
    if t["mean_scores"]:
        click.echo(
            "  mean judge scores: " + ", ".join(f"{k} {v}" for k, v in t["mean_scores"].items())
        )
    if against is not None:
        base_run, base_results = against
        diff = compare(results, base_results, current_run=run, against_run=base_run)
        for warning in diff["warnings"]:
            click.echo(f"warning: not like for like, {warning}")
        click.echo(
            f"against {str(base_run['run_id'])[:8]} ({base_run['model']}), "
            f"{diff['questions']} shared questions: pass rate {diff['pass_rate'][0]} -> "
            f"{diff['pass_rate'][1]}; regressions {diff['regressions'] or 'none'}; "
            f"fixes {diff['fixes'] or 'none'}; cost delta {diff['cost_delta_usd']}; "
            f"p50 seconds delta {diff['p50_seconds_delta']}"
        )


@eval_group.command("run")
@click.option(
    "--model", "model_spec", default=None, help="Model under test (default PAID_MEDIA_MODEL)."
)
@click.option(
    "--judge", "judge_spec", default=None, help="Judge model (default PAID_MEDIA_EVAL_JUDGE_MODEL)."
)
@click.option("--no-judge", is_flag=True, help="Deterministic checks only.")
@click.option(
    "--judge-all",
    is_flag=True,
    help="Judge every answer; by default an answer that failed a check is not judged.",
)
@click.option("--ids", default=None, help="Comma-separated question ids or prefixes (q01,q16).")
@click.option(
    "--repeat",
    type=click.IntRange(1, 10),
    default=1,
    show_default=True,
    help="Ask each question this many times; a question passes on a majority of attempts.",
)
@click.option(
    "--rpm",
    type=click.IntRange(0, 600),
    default=15,
    show_default=True,
    help="Model requests per minute, for rate-limited keys (0: no limit).",
)
@click.option("--store", "store_path", default=None, help="Eval results file.")
@click.option("--json", "as_json", is_flag=True)
def eval_run(
    model_spec: str | None,
    judge_spec: str | None,
    no_judge: bool,
    judge_all: bool,
    ids: str | None,
    repeat: int,
    rpm: int,
    store_path: str | None,
    as_json: bool,
) -> None:
    """Ask every question on synced sample accounts and grade the answers (bills the model)."""
    from paid_media_agent.evals.suite import run_suite

    root = project_root()
    settings = actions.load_settings(root)
    _configure_logging(settings)
    spec = model_spec or settings.paid_media_model
    judge = None if no_judge else (judge_spec or settings.paid_media_eval_judge_model)
    model = _eval_model(spec, settings, rpm=rpm)
    judge_model = _eval_model(judge, settings, rpm=rpm) if judge else None
    store = _eval_store(store_path)

    def progress(row: dict[str, Any]) -> None:
        if not as_json:
            mark = "PASS" if row["passed"] else "FAIL"
            click.echo(f"  {mark} {row['question_id']} ({row['seconds']}s)", err=True)

    try:
        run_id, results = asyncio.run(
            run_suite(
                settings,
                project_root=root,
                model=model,
                model_spec=spec,
                judge_model=judge_model,
                judge_spec=judge,
                store=store,
                ids=ids.split(",") if ids else None,
                on_result=progress,
                repeat=repeat,
                judge_all=judge_all,
            )
        )
        run = store.run(run_id)
        base_id = store.resolve("baseline")
        against = (
            (store.run(base_id), store.results(base_id))
            if base_id is not None and base_id != run_id
            else None
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        store.close()
    if as_json:
        click.echo(json.dumps({"run": run, "results": results}, indent=2, default=str))
        return
    _echo_eval(run, results, against)


@eval_group.command("report")
@click.argument("run_ref", required=False)
@click.option(
    "--against", default="baseline", show_default=True, help="Run id, prefix, or baseline."
)
@click.option("--store", "store_path", default=None)
@click.option("--json", "as_json", is_flag=True)
def eval_report(run_ref: str | None, against: str, store_path: str | None, as_json: bool) -> None:
    """Show a stored run (default the latest) and what changed against another run."""
    from paid_media_agent.evals.report import compare, totals

    store = _eval_store(store_path)
    try:
        run_id = store.resolve(run_ref)
        if run_id is None:
            raise click.ClickException("no eval run found; run `paid-media-agent eval run` first")
        run, results = store.run(run_id), store.results(run_id)
        other = store.resolve(against)
        base = (store.run(other), store.results(other)) if other and other != run_id else None
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        store.close()
    if as_json:
        body = {"run": run, "totals": totals(results), "results": results}
        if base is not None:
            body["against"] = {
                "run_id": base[0]["run_id"],
                **compare(results, base[1], current_run=run, against_run=base[0]),
            }
        click.echo(json.dumps(body, indent=2, default=str))
        return
    _echo_eval(run, results, base)


@eval_group.command("regrade")
@click.argument("run_ref")
@click.option("--store", "store_path", default=None)
def eval_regrade(run_ref: str, store_path: str | None) -> None:
    """Re-check a stored run's answers with the current checks (no model is called)."""
    from paid_media_agent.evals.suite import regrade

    store = _eval_store(store_path)
    try:
        run_id = store.resolve(run_ref)
        if run_id is None:
            raise click.ClickException(f"no eval run {run_ref}")
        new_id = regrade(store, run_id, project_root=project_root())
        run, results = store.run(new_id), store.results(new_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        store.close()
    click.echo(f"regraded {str(run_id)[:8]} as {str(new_id)[:8]}")
    _echo_eval(run, results, None)


@eval_group.command("baseline")
@click.argument("run_ref")
@click.option("--store", "store_path", default=None)
def eval_baseline(run_ref: str, store_path: str | None) -> None:
    """Mark a run as the baseline later runs are compared with."""
    store = _eval_store(store_path)
    try:
        run_id = store.resolve(run_ref)
        if run_id is None:
            raise click.ClickException(f"no eval run {run_ref}")
        store.set_baseline(run_id)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from None
    finally:
        store.close()
    click.echo(f"baseline is now {run_id}")


@main.group()
def context() -> None:
    """The company-context runtime skill: business facts the agent reads."""


@context.command("init")
def context_init() -> None:
    """Create workspace/skills/company-context/SKILL.md from the template; never overwrites."""
    from paid_media_agent.admin.company_context import init_company_context

    settings = Settings()
    workspace = settings.paid_media_workspace_root
    if not workspace.is_absolute():
        workspace = project_root() / workspace
    path, created = init_company_context(workspace)
    if created:
        click.echo(
            f"Created {path}. Replace the guidance with your facts; set numbers with `goals set`."
        )
    else:
        click.echo(f"{path} already exists; left unchanged.")


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
