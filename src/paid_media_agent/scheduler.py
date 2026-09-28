"""Recurring jobs inside `serve`: the daily history sync and the deterministic reports.

`serve` owns the state file, so scheduled work runs in that process. A job is due once its hour
has come on one of its days and no scheduled run has started that day, so a server started late
still runs the day's job, and a failed run waits for its next slot instead of retrying in a loop.
Every run, scheduled or manual (`POST /jobs/{name}`), is recorded in `job_runs`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from paid_media_agent.redaction import sanitize_exception
from paid_media_agent.store.db import Store, utc_now

if TYPE_CHECKING:
    from paid_media_agent.runtime.self_hosted import SelfHostedRuntime

log = logging.getLogger(__name__)

JOB_NAMES = ("sync", "report_weekly", "report_monthly")
JobTrigger = Literal["scheduled", "manual"]


class UnknownJob(KeyError):
    pass


class JobBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class Job:
    name: str
    run: Callable[[], Awaitable[dict[str, Any]]]
    on_day: Callable[[date], bool] = lambda _day: True
    hour_utc: int = 6
    scheduled: bool = True
    """False keeps the job available to `POST /jobs/{name}` without running it on a schedule."""


@dataclass(frozen=True)
class JobRun:
    run_id: uuid.UUID
    job: str
    trigger: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    detail: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "job": self.job,
            "trigger": self.trigger,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "detail": self.detail,
        }


class Scheduler:
    def __init__(
        self,
        store: Store,
        jobs: Sequence[Job],
        *,
        clock: Callable[[], datetime] = utc_now,
        tick_seconds: float = 60.0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._jobs = {job.name: job for job in jobs}
        self._locks = {name: asyncio.Lock() for name in self._jobs}
        self._clock = clock
        self._tick = tick_seconds
        self._sleep = sleep

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._jobs)

    def _last_scheduled_start(self, name: str) -> datetime | None:
        rows = self._store.fetch(
            "SELECT max(started_at) FROM job_runs WHERE job = ? AND trigger = 'scheduled'", [name]
        )
        value: datetime | None = rows[0][0] if rows else None
        return value

    def due(self, now: datetime) -> list[Job]:
        due = []
        for job in self._jobs.values():
            if not job.scheduled or not job.on_day(now.date()) or now.hour < job.hour_utc:
                continue
            last = self._last_scheduled_start(job.name)
            if last is None or last.date() < now.date():
                due.append(job)
        return due

    async def tick(self) -> list[JobRun]:
        runs = []
        for job in self.due(self._clock()):
            if self._locks[job.name].locked():
                continue
            runs.append(await self._run(job, "scheduled"))
        return runs

    async def run_now(self, name: str) -> JobRun:
        job = self._jobs.get(name)
        if job is None:
            raise UnknownJob(name)
        if self._locks[name].locked():
            raise JobBusy(name)
        return await self._run(job, "manual")

    async def run_forever(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            await self._sleep(self._tick)

    def history(self, limit: int = 20) -> list[JobRun]:
        rows = self._store.fetch(
            "SELECT run_id, job, trigger, status, started_at, finished_at, detail FROM job_runs "
            "ORDER BY started_at DESC LIMIT ?",
            [limit],
        )
        return [
            JobRun(
                run_id=row[0],
                job=row[1],
                trigger=row[2],
                status=row[3],
                started_at=row[4],
                finished_at=row[5],
                detail=json.loads(row[6]) if row[6] else {},
            )
            for row in rows
        ]

    async def _run(self, job: Job, trigger: JobTrigger) -> JobRun:
        async with self._locks[job.name]:
            run_id = uuid.uuid4()
            started = self._clock()
            self._store.write(
                "INSERT INTO job_runs VALUES (?, ?, ?, 'running', ?, NULL, NULL)",
                [run_id, job.name, trigger, started],
            )
            try:
                detail = await job.run()
                status = "ok"
            except Exception as exc:
                log.warning("job %s failed", job.name, exc_info=True)
                detail, status = {"error": sanitize_exception(exc)}, "failed"
            finished = self._clock()
            self._store.write(
                "UPDATE job_runs SET status = ?, finished_at = ?, detail = ? WHERE run_id = ?",
                [status, finished, json.dumps(detail, default=str), run_id],
            )
            return JobRun(run_id, job.name, trigger, status, started, finished, detail)


def build_jobs(runtime: SelfHostedRuntime, *, clock: Callable[[], datetime] = utc_now) -> list[Job]:
    """Every job, bound to the server's accounts and read path; `PAID_MEDIA_JOBS` schedules them."""
    from paid_media_agent.analytics.sync import run_sync
    from paid_media_agent.reports.cadence import Cadence, run_cadence_report

    settings = runtime.settings
    profile = runtime.profile
    dispatcher = runtime.components.read_dispatcher

    def yesterday() -> date:
        return clock().date() - timedelta(days=1)

    async def sync() -> dict[str, Any]:
        run = await run_sync(
            accounts=profile.accounts,
            catalog=runtime.catalog,
            dispatcher=dispatcher,
            end=yesterday(),
            days=settings.paid_media_sync_days,
        )
        return run.summary()

    def report(cadence: Cadence) -> Callable[[], Awaitable[dict[str, Any]]]:
        async def _report() -> dict[str, Any]:
            run = await run_cadence_report(
                cadence=cadence,
                end=yesterday(),
                accounts=profile.accounts,
                catalog=runtime.catalog,
                dispatcher=dispatcher,
                artifacts=profile.artifacts,
            )
            listed = (run.report or {}).get("files")
            files = (
                [str(f.get("path")) for f in listed if isinstance(f, dict)]
                if isinstance(listed, list)
                else []
            )
            return {
                "analysis_artifact_id": run.analysis_artifact_id,
                "reconciled": run.reconciled,
                "unavailable": list(run.unavailable),
                "files": files,
            }

        return _report

    hour = settings.paid_media_job_hour_utc
    enabled = set(settings.scheduled_jobs())
    return [
        Job("sync", sync, hour_utc=hour, scheduled="sync" in enabled),
        Job(
            "report_weekly",
            report("weekly"),
            on_day=lambda d: d.weekday() == 0,
            hour_utc=hour,
            scheduled="report_weekly" in enabled,
        ),
        Job(
            "report_monthly",
            report("monthly"),
            on_day=lambda d: d.day == 1,
            hour_utc=hour,
            scheduled="report_monthly" in enabled,
        ),
    ]
