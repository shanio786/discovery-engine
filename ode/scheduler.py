"""`ode scheduler`: the always-on engine.

One long-running process (a restart-always Docker service) that runs every
job on its own schedule, one at a time, forever:

    daily          at scheduler.daily_at         full cycle (discover, collect, score, briefs)
    reports        at scheduler.reports_at       reports/plan-latest.md, gems-latest.md/.csv
    plan_message   at scheduler.plan_message_at  Telegram: today's plan
    midday_wiki    at scheduler.midday_wiki_at   cheap Wikipedia refresh
    hourly_wiki    every N minutes               hourly dumps -> same-day spikes
    trends         every N minutes               Google Trends per country
    breakout       every N minutes               spec breakout alert (6h)

Last runs live in `scheduler_state`, so a restart never re-runs a job early.
A failing job is logged and retried at its next slot; the loop never dies.
Sources only refresh daily/hourly, so "busy" means: always ready, never
hammering an API for data that cannot have changed.
"""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta

from ode.config import Config, Secrets
from ode.db import Database
from ode.logging import get_logger

log = get_logger(__name__)

TICK_SECONDS = 30


@dataclass(frozen=True)
class Job:
    name: str
    every: timedelta | None = None    # interval job
    at: time | None = None            # daily job (UTC)


def parse_at(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m), tzinfo=UTC)


def jobs_from_config(config: Config) -> list[Job]:
    s = config.scheduler
    every = s.get("every") or {}
    return [
        Job("daily", at=parse_at(s.get("daily_at", "04:17"))),
        Job("reports", at=parse_at(s.get("reports_at", "06:37"))),
        Job("plan_message", at=parse_at(s.get("plan_message_at", "06:47"))),
        Job("midday_wiki", at=parse_at(s.get("midday_wiki_at", "16:17"))),
        Job("hourly_wiki", every=timedelta(minutes=int(every.get("hourly_wiki_minutes", 60)))),
        Job("trends", every=timedelta(minutes=int(every.get("trends_minutes", 60)))),
        Job("breakout", every=timedelta(minutes=int(every.get("breakout_minutes", 360)))),
    ]


def is_due(job: Job, last_run: datetime | None, now: datetime) -> bool:
    """Pure: should `job` run at `now` given its last run?"""
    if job.every is not None:
        return last_run is None or now - last_run >= job.every
    assert job.at is not None
    slot = datetime.combine(now.date(), job.at)
    if now < slot:
        slot -= timedelta(days=1)       # today's slot not reached: yesterday's counts
    # never run a daily job for a slot older than a day (no stampede after downtime)
    return (last_run is None or last_run < slot) and now - slot < timedelta(hours=20)


class Scheduler:
    def __init__(self, config: Config, secrets: Secrets) -> None:
        self.config = config
        self.secrets = secrets
        self.jobs = jobs_from_config(config)
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def _last_runs(self, db: Database) -> dict[str, datetime]:
        rows = await db.pool.fetch("SELECT job, last_run FROM scheduler_state")
        return {r["job"]: r["last_run"] for r in rows}

    async def _mark(self, db: Database, job: str, when: datetime) -> None:
        await db.pool.execute(
            "INSERT INTO scheduler_state (job, last_run) VALUES ($1, $2) "
            "ON CONFLICT (job) DO UPDATE SET last_run = EXCLUDED.last_run", job, when)

    def _runner(self, name: str) -> Callable[[Database], Awaitable[dict]]:
        from ode import pipeline
        from ode.realtime import runner

        async def with_client(fn, db: Database) -> dict:
            async with pipeline.http_client(self.config) as client:
                return await fn(self.config, self.secrets, db, client)

        table: dict[str, Callable[[Database], Awaitable[dict]]] = {
            "daily": lambda db: pipeline.daily(self.config, self.secrets),
            "midday_wiki": lambda db: pipeline.harvest(self.config, self.secrets,
                                                       sources=["wikipedia"]),
            "plan_message": lambda db: with_client(runner.plan_message_job, db),
            "reports": lambda db: with_client(runner.reports_job, db),
            "hourly_wiki": lambda db: with_client(runner.hourly_job, db),
            "trends": lambda db: with_client(runner.trends_job, db),
            "breakout": lambda db: with_client(runner.breakout_job, db),
        }
        return table[name]

    async def run_job(self, db: Database, job: Job) -> bool:
        """Run one job; a stop request cancels it. Returns False if cancelled
        (then it is NOT marked as run, so it runs again after the restart)."""
        started = datetime.now(UTC)
        log.info("scheduler.job_start", job=job.name)
        task = asyncio.create_task(self._runner(job.name)(db))
        stopper = asyncio.create_task(self._stop.wait())
        await asyncio.wait({task, stopper}, return_when=asyncio.FIRST_COMPLETED)
        stopper.cancel()
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass  # expected: we just cancelled it
            except Exception as exc:  # noqa: BLE001 - shutting down anyway
                log.debug("scheduler.cancel_error", job=job.name, error=str(exc)[:200])
            log.warning("scheduler.job_cancelled", job=job.name, reason="shutdown")
            return False
        try:
            result = task.result()
            log.info("scheduler.job_done", job=job.name,
                     seconds=round((datetime.now(UTC) - started).total_seconds()),
                     result=str(result)[:300])
        except Exception as exc:  # noqa: BLE001 - the loop must never die
            log.error("scheduler.job_failed", job=job.name, error=str(exc)[:300])
        await self._mark(db, job.name, started)
        return True

    async def run_forever(self) -> None:
        db = await Database.connect(self.secrets.database_url)
        try:
            await db.init_schema()
            log.info("scheduler.started", jobs=[j.name for j in self.jobs])
            while not self._stop.is_set():
                last = await self._last_runs(db)
                now = datetime.now(UTC)
                for job in self.jobs:
                    if self._stop.is_set():
                        break
                    if is_due(job, last.get(job.name), now):
                        await self.run_job(db, job)
                        now = datetime.now(UTC)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=TICK_SECONDS)
                except TimeoutError:
                    pass
        finally:
            await db.close()
            log.info("scheduler.stopped")


async def main(config: Config, secrets: Secrets) -> int:
    sched = Scheduler(config, secrets)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, sched.stop)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass
    await sched.run_forever()
    return 0
