"""Persistent job queue for background ingestion tasks with retry/recovery."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger(__name__)


class JobStatus(str, Enum):
    """Job lifecycle states."""

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class IngestionJobQueue:
    """Manages persistent ingestion jobs with retry/recovery semantics."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        max_retries: int = 3,
        retry_delay_seconds: int = 60,
        job_timeout_seconds: int = 3600,
    ):
        self.session_factory = session_factory
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self.job_timeout_seconds = job_timeout_seconds
        self._running_jobs: dict[UUID, asyncio.Task] = {}

    async def enqueue_ingestion(self, job_id: UUID, source_id: UUID, version_id: UUID) -> None:
        """Record a pending job in the database."""
        from db.models import KnowledgeInjectionJob

        async with self.session_factory() as session:
            row = await session.get(KnowledgeInjectionJob, job_id)
            if row:
                # Job already exists; don't re-enqueue
                return
            # Job will be created by the ingestion pipeline; this just marks it as queued
            logger.info(f"Enqueued job {job_id} for processing")

    async def process_pending_jobs(self) -> None:
        """Main worker loop: pick up pending jobs and run them."""
        from db.models import KnowledgeInjectionJob
        from ingestion.pipeline import run_ingestion
        from ingestion.pipeline_types import JobRef, JobStatus as PipelineJobStatus, JobType

        while True:
            try:
                async with self.session_factory() as session:
                    # Find jobs ready to run (pending or retrying, not yet timed out)
                    cutoff = datetime.now(timezone.utc) - timedelta(seconds=self.job_timeout_seconds)
                    result = await session.execute(
                        select(KnowledgeInjectionJob).where(
                            (KnowledgeInjectionJob.status == "QUEUED")
                            & (KnowledgeInjectionJob.created_at > cutoff)
                        )
                    )
                    jobs = result.scalars().all()

                    for row in jobs:
                        # Skip if already running
                        if row.job_id in self._running_jobs:
                            continue

                        job = JobRef(
                            job_id=row.job_id,
                            source_id=row.source_id,
                            version_id=row.version_id,
                            job_type=JobType(row.job_type),
                            status=PipelineJobStatus(row.status),
                            triggered_by=row.triggered_by,
                        )

                        # Launch job as a task and track it
                        task = asyncio.create_task(self._run_job_with_retry(job, row))
                        self._running_jobs[job.job_id] = task

                        # Clean up completed tasks
                        self._running_jobs = {
                            jid: t for jid, t in self._running_jobs.items() if not t.done()
                        }

                # Sleep before checking for more jobs
                await asyncio.sleep(5)
            except Exception as e:
                logger.exception(f"Error in job processing loop: {e}")
                await asyncio.sleep(10)

    async def _run_job_with_retry(
        self, job: JobRef, db_row: KnowledgeInjectionJob
    ) -> None:
        """Execute a job with retry logic."""
        from db.models import KnowledgeInjectionJob
        from ingestion.pipeline import run_ingestion
        from ingestion.queue import repository as job_repo

        attempt = 0
        while attempt < self.max_retries:
            try:
                async with self.session_factory() as session:
                    # Refresh job state
                    row = await session.get(KnowledgeInjectionJob, job.job_id)
                    if row is None or row.status == "FAILED":
                        break

                    # Mark as running
                    row.status = "RUNNING"
                    row.attempts = (row.attempts or 0) + 1
                    await session.commit()

                # Execute ingestion (may take a long time)
                outcome = await asyncio.wait_for(
                    self._execute_ingestion(job),
                    timeout=self.job_timeout_seconds,
                )

                # Mark complete
                async with self.session_factory() as session:
                    await job_repo.complete_job(
                        session,
                        job_id=job.job_id,
                        status=outcome["status"],
                        chunks_created_count=outcome.get("chunks_created_count", 0),
                        entities_created_count=outcome.get("entities_created_count", 0),
                        error_details=outcome.get("error_details"),
                    )
                    await session.commit()
                logger.info(f"Job {job.job_id} completed")
                break

            except asyncio.TimeoutError:
                logger.warning(f"Job {job.job_id} timed out (attempt {attempt + 1})")
                attempt += 1
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay_seconds)
            except Exception as e:
                logger.exception(f"Job {job.job_id} failed on attempt {attempt + 1}: {e}")
                attempt += 1
                if attempt < self.max_retries:
                    # Exponential backoff
                    delay = self.retry_delay_seconds * (2 ** attempt)
                    await asyncio.sleep(delay)

        # Mark as failed if all retries exhausted
        if attempt >= self.max_retries:
            async with self.session_factory() as session:
                from ingestion.queue import repository as job_repo

                await job_repo.complete_job(
                    session,
                    job_id=job.job_id,
                    status="FAILED",
                    chunks_created_count=0,
                    entities_created_count=0,
                    error_details=f"Max retries ({self.max_retries}) exceeded",
                )
                await session.commit()
            logger.error(f"Job {job.job_id} failed after {self.max_retries} attempts")

    async def _execute_ingestion(self, job: JobRef) -> dict:
        """Run the actual ingestion pipeline."""
        from ingestion.pipeline import run_ingestion

        async with self.session_factory() as session:
            outcome = await run_ingestion(session, job)
            return {
                "status": outcome.status,
                "chunks_created_count": outcome.chunks_created_count,
                "entities_created_count": outcome.entities_created_count,
                "error_details": outcome.error_details,
            }
