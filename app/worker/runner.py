"""Runs jobs one at a time and records every run in `job_runs`."""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

from app.db.models import JobRun
from app.kickbase.errors import KickbaseUnavailable, ReadOnlyViolation
from app.worker.jobs import JobContext

logger = logging.getLogger(__name__)

Job = Callable[[JobContext], Awaitable[str]]
# Expected now and then; everything else is a bug or a changed API and gets a traceback.
TRANSIENT_ERRORS = (KickbaseUnavailable, OperationalError, InterfaceError)


class JobRunner:
    def __init__(self, ctx: JobContext) -> None:
        self.ctx = ctx
        self._lock = asyncio.Lock()  # one job at a time; they share the Kickbase client

    async def mark_interrupted_runs(self) -> int:
        """Runs still marked 'running' from a previous process were interrupted (one worker)."""
        async with self.ctx.sessions.begin() as session:
            result = await session.execute(
                update(JobRun)
                .where(JobRun.status == "running")
                .values(status="aborted", finished_at=self.ctx.clock(), detail="Worker beendet")
            )
            return result.rowcount or 0

    async def run(self, name: str, job: Job) -> bool:
        async with self._lock:
            ctx = self.ctx
            requests_before = ctx.client.request_count
            async with ctx.sessions.begin() as session:
                run = JobRun(job=name, started_at=ctx.clock(), status="running", requests=0)
                session.add(run)
                await session.flush()
                run_id = run.id
            logger.info("Job %s gestartet", name)
            status, detail = "failed", None
            try:
                detail = await job(ctx)
                status = "ok"
            except asyncio.CancelledError:
                status, detail = "cancelled", "abgebrochen (Worker beendet)"
                raise
            except ReadOnlyViolation as exc:
                detail = f"ReadOnlyViolation: {exc}"
                logger.critical(
                    "Job %s wollte schreibend auf Kickbase zugreifen", name, exc_info=True
                )
                raise
            except TRANSIENT_ERRORS as exc:
                detail = f"{type(exc).__name__}: {exc}"[:1000]
                logger.error("Job %s fehlgeschlagen: %s", name, detail)
            except Exception as exc:  # bugs or a changed API: keep the traceback
                detail = f"{type(exc).__name__}: {exc}"[:1000]
                logger.exception("Job %s fehlgeschlagen", name)
            finally:
                await self._finish(
                    run_id, status, detail, ctx.client.request_count - requests_before
                )
            if status == "ok":
                logger.info("Job %s fertig: %s", name, detail)
            return status == "ok"

    async def _finish(self, run_id: int, status: str, detail: str | None, requests: int) -> None:
        try:
            async with self.ctx.sessions.begin() as session:
                run = await session.get(JobRun, run_id)
                run.finished_at = self.ctx.clock()
                run.status = status
                run.requests = requests
                run.detail = detail
        except SQLAlchemyError:
            logger.exception("Status von Job-Lauf %s konnte nicht gespeichert werden", run_id)

    async def last_success(self, name: str) -> datetime | None:
        async with self.ctx.sessions() as session:
            return await session.scalar(
                select(JobRun.finished_at)
                .where(JobRun.job == name, JobRun.status == "ok")
                .order_by(JobRun.finished_at.desc())
                .limit(1)
            )

    async def succeeded_today(self, name: str) -> bool:
        last = await self.last_success(name)
        return last is not None and last.astimezone(self.ctx.tz).date() == self.ctx.today()
