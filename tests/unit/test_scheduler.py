"""Scheduled jobs on a fake clock, and running one on demand through the API."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from paid_media_agent.config import Settings
from paid_media_agent.scheduler import Job, JobBusy, Scheduler, UnknownJob, build_jobs
from paid_media_agent.store import Store
from paid_media_agent.testing.scripted_model import ScriptedChatModel


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def _scheduler(clock: Clock, jobs: list[Job]) -> Scheduler:
    return Scheduler(Store(), jobs, clock=clock)


async def test_a_daily_job_runs_once_its_hour_comes_and_once_per_day() -> None:
    ran: list[datetime] = []
    clock = Clock(datetime(2026, 3, 2, 5, 30))  # a Monday

    async def work() -> dict[str, Any]:
        ran.append(clock.now)
        return {"ok": True}

    scheduler = _scheduler(clock, [Job("sync", work, hour_utc=6)])
    assert await scheduler.tick() == []
    clock.now = datetime(2026, 3, 2, 6, 1)
    assert [r.status for r in await scheduler.tick()] == ["ok"]
    clock.now = datetime(2026, 3, 2, 23, 0)
    assert await scheduler.tick() == []
    clock.now = datetime(2026, 3, 3, 14, 0)  # the server was down at six; it still runs today
    await scheduler.tick()
    assert ran == [datetime(2026, 3, 2, 6, 1), datetime(2026, 3, 3, 14, 0)]


async def test_weekly_and_unscheduled_jobs_only_run_when_they_should() -> None:
    ran: list[str] = []

    def record(name: str) -> Any:
        async def work() -> dict[str, Any]:
            ran.append(name)
            return {}

        return work

    clock = Clock(datetime(2026, 3, 3, 7))  # a Tuesday
    scheduler = _scheduler(
        clock,
        [
            Job("weekly", record("weekly"), on_day=lambda d: d.weekday() == 0),
            Job("manual_only", record("manual_only"), scheduled=False),
        ],
    )
    await scheduler.tick()
    clock.now = datetime(2026, 3, 9, 7)  # Monday
    await scheduler.tick()
    assert ran == ["weekly"]
    await scheduler.run_now("manual_only")
    assert ran == ["weekly", "manual_only"]


async def test_a_failure_is_recorded_and_waits_for_the_next_day() -> None:
    calls = 0

    async def broken() -> dict[str, Any]:
        nonlocal calls
        calls += 1
        raise RuntimeError("provider down")

    clock = Clock(datetime(2026, 3, 2, 6))
    scheduler = _scheduler(clock, [Job("sync", broken)])
    (run,) = await scheduler.tick()
    assert run.status == "failed" and "provider down" in run.detail["error"]
    clock.now += timedelta(hours=1)
    assert await scheduler.tick() == [] and calls == 1
    assert [r.status for r in scheduler.history()] == ["failed"]


async def test_manual_runs_do_not_skip_the_schedule_and_never_overlap() -> None:
    gate = asyncio.Event()

    async def slow() -> dict[str, Any]:
        await gate.wait()
        return {}

    clock = Clock(datetime(2026, 3, 2, 7))
    scheduler = _scheduler(clock, [Job("sync", slow)])
    running = asyncio.create_task(scheduler.run_now("sync"))
    await asyncio.sleep(0)
    with pytest.raises(JobBusy):
        await scheduler.run_now("sync")
    assert await scheduler.tick() == [], "a busy job is skipped, not queued"
    gate.set()
    assert (await running).trigger == "manual"
    assert [r.trigger for r in await scheduler.tick()] == ["scheduled"]
    with pytest.raises(UnknownJob):
        await scheduler.run_now("nope")


def test_the_job_setting_is_validated() -> None:
    assert Settings(_env_file=None, paid_media_jobs=" sync , ").scheduled_jobs() == ("sync",)  # type: ignore[call-arg]
    assert Settings(_env_file=None, paid_media_jobs="").scheduled_jobs() == ()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="unknown PAID_MEDIA_JOBS"):
        Settings(_env_file=None, paid_media_jobs="sync,vacuum").scheduled_jobs()  # type: ignore[call-arg]


async def test_the_api_runs_the_real_sync_job_into_the_state_file(
    settings: Settings, project_root: Path, tmp_path: Path
) -> None:
    from paid_media_agent.runtime.self_hosted import build_self_hosted_runtime
    from paid_media_agent.surfaces.api.app import create_app

    configured = settings.model_copy(
        update={
            "paid_media_api_tokens": SecretStr("tok-ops:ops"),
            "paid_media_data_mode": "sample",
            "paid_media_jobs": "sync",
        }
    )
    model = ScriptedChatModel(steps=[])
    runtime = build_self_hosted_runtime(
        configured, project_root=project_root, model=model, store=Store(tmp_path / "state.duckdb")
    )
    clock = Clock(datetime(2026, 8, 29, 7))
    scheduler = Scheduler(runtime.store, build_jobs(runtime, clock=clock), clock=clock)
    auth = {"Authorization": "Bearer tok-ops"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(create_app(runtime, scheduler=scheduler)),
        base_url="http://test",
    ) as client:
        assert (await client.post("/jobs/sync")).status_code == 401
        assert (await client.post("/jobs/nope", headers=auth)).status_code == 404
        response = await client.post("/jobs/sync", headers=auth)
        listing = (await client.get("/jobs", headers=auth)).json()

    run = response.json()
    assert response.status_code == 200 and run["status"] == "ok", run
    assert run["detail"]["end"] == "2026-08-28" and run["detail"]["rows"] > 0
    assert listing["jobs"] == [
        "sync",
        "anomalies",
        "allocate",
        "report_weekly",
        "report_monthly",
        "backup",
    ]
    assert listing["recent"][0]["trigger"] == "manual"
    stored = runtime.store.fetch("SELECT count(DISTINCT source), min(source) FROM pulls")
    assert stored == [(1, "sync")]
    runtime.store.close()

    without = create_app(
        build_self_hosted_runtime(configured, project_root=project_root, model=model, store=Store())
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(without), base_url="http://test"
    ) as client:
        assert (await client.post("/jobs/sync", headers=auth)).status_code == 404
