"""DB-backed job queue: enqueue, claim, execute, and schedule dispatch."""

from __future__ import annotations

import logging
import os
import signal
import socket
import time
import uuid
from datetime import timedelta
from typing import Any

from django.db import IntegrityError, NotSupportedError, transaction
from django.utils import timezone
from rest_framework import status as http_status
from rest_framework.exceptions import APIException

from apps.engine.models import log_decision
from apps.jobs.handlers import HANDLERS, run_handler
from apps.jobs.models import ACTIVE_JOB_STATUSES, EXCLUSIVE_JOB_TYPES, JobRun, JobSchedule
from apps.jobs.schedules import ensure_schedules, next_run_for

logger = logging.getLogger("jobs")

# A running analysis is only presumed abandoned after this long; a 1h universe
# run plus the hybrid pass takes ~20 minutes, so the default 60 minutes could
# free the slot while one is still working.
STALE_AFTER_MINUTES = {"daily_analysis": 360}
DEFAULT_STALE_AFTER_MINUTES = 60


class JobAlreadyActive(APIException):
    """An exclusive job type already has a queued or running job.

    API views return it as HTTP 409 with the blocking job's id; the scheduler
    and the CLI catch it and skip.
    """

    status_code = http_status.HTTP_409_CONFLICT
    default_code = "job_already_active"

    def __init__(self, active: JobRun):
        self.active = active
        verb = "is running" if active.status == JobRun.Status.RUNNING else "is queued"
        message = (
            f"A {active.job_type.replace('_', ' ')} job {verb} "
            f"({str(active.id)[:8]}); wait for it to finish before starting another."
        )
        super().__init__(
            {
                "detail": message,
                "active_job_id": str(active.id),
                "active_job_status": active.status,
            }
        )
        self.message = message


def active_exclusive_job(job_type: str) -> JobRun | None:
    if job_type not in EXCLUSIVE_JOB_TYPES:
        return None
    return (
        JobRun.objects.filter(job_type=job_type, status__in=ACTIVE_JOB_STATUSES)
        .order_by("enqueued_at")
        .first()
    )


def _create_job(job_type: str, **fields: Any) -> JobRun:
    """Create a job row, refusing a second active job of an exclusive type.

    The pre-check gives a clear error in the common case; the partial unique
    index ``one_active_exclusive_job`` closes the race between two requests
    that both pass the pre-check.
    """

    active = active_exclusive_job(job_type)
    if active is not None:
        raise JobAlreadyActive(active)
    try:
        with transaction.atomic():
            return JobRun.objects.create(job_type=job_type, **fields)
    except IntegrityError:
        active = active_exclusive_job(job_type)
        if active is None:
            raise
        raise JobAlreadyActive(active) from None


def enqueue_job(
    job_type: str,
    *,
    trigger: str = JobRun.Trigger.MANUAL,
    params: dict[str, Any] | None = None,
    schedule: JobSchedule | None = None,
) -> JobRun:
    if job_type not in HANDLERS:
        raise ValueError(f"Unknown job type: {job_type}")
    job = _create_job(
        job_type,
        trigger=trigger,
        params=params or {},
        schedule=schedule,
        status=JobRun.Status.QUEUED,
    )
    log_decision(
        "jobs",
        f"Job {job.job_type} enqueued ({trigger}) as {job.id}.",
        job_run_id=job.id,
        payload={"job_id": str(job.id), "job_type": job.job_type, "params": job.params},
    )
    return job


def start_inline_job(
    job_type: str, *, params: dict[str, Any] | None = None, worker: str = "runjob"
) -> JobRun:
    """Create an already-running job for synchronous execution (``runjob``).

    Goes through the same exclusivity check as the queue, so running a job
    inline cannot overlap a queued or running one.
    """

    if job_type not in HANDLERS:
        raise ValueError(f"Unknown job type: {job_type}")
    return _create_job(
        job_type,
        trigger=JobRun.Trigger.MANUAL,
        status=JobRun.Status.RUNNING,
        params=params or {},
        started_at=timezone.now(),
        worker=worker,
    )


def enqueue_due_schedules(now=None) -> list[JobRun]:
    """Create queued jobs for every enabled schedule whose slot has arrived."""
    now = now or timezone.now()
    ensure_schedules()
    enqueued: list[JobRun] = []
    due_ids = list(
        JobSchedule.objects.filter(enabled=True, next_run_at__lte=now).values_list("id", flat=True)
    )
    for schedule_id in due_ids:
        with transaction.atomic():
            schedule = (
                JobSchedule.objects.select_for_update()
                .filter(id=schedule_id, enabled=True, next_run_at__lte=now)
                .first()
            )
            if schedule is None:
                continue
            try:
                job = enqueue_job(
                    schedule.job_type,
                    trigger=JobRun.Trigger.SCHEDULED,
                    params=schedule.params,
                    schedule=schedule,
                )
            except JobAlreadyActive as exc:
                # Skip this slot rather than queueing behind the active job;
                # the next slot is computed as usual.
                schedule.next_run_at = next_run_for(schedule, now)
                schedule.save(update_fields=["next_run_at"])
                logger.warning("Skipped schedule %s: %s", schedule.slug, exc.message)
                log_decision(
                    "jobs",
                    f"Scheduled {schedule.job_type} skipped: {exc.message}",
                    severity="warning",
                    job_run_id=exc.active.id,
                    payload={
                        "schedule": schedule.slug,
                        "active_job_id": str(exc.active.id),
                        "next_run_at": schedule.next_run_at.isoformat()
                        if schedule.next_run_at
                        else None,
                    },
                )
                continue
            schedule.last_enqueued_at = now
            schedule.last_job = job
            schedule.next_run_at = next_run_for(schedule, now)
            schedule.save(update_fields=["last_enqueued_at", "last_job", "next_run_at"])
            enqueued.append(job)
    if enqueued:
        logger.info("Enqueued %s scheduled job(s)", len(enqueued))
    return enqueued


def _claim_next(worker_id: str) -> JobRun | None:
    with transaction.atomic():
        queryset = JobRun.objects.select_for_update(skip_locked=True).filter(
            status=JobRun.Status.QUEUED
        )
        try:
            job = queryset.order_by("enqueued_at").first()
        except NotSupportedError:
            job = queryset.order_by("enqueued_at").first()
        if job is None:
            return None
        job.status = JobRun.Status.RUNNING
        job.started_at = timezone.now()
        job.worker = worker_id
        job.save(update_fields=["status", "started_at", "worker"])
        return job


def execute_job(job: JobRun) -> JobRun:
    started = time.monotonic()
    try:
        result = run_handler(job.job_type, job.params or {}, job)
        job.status = JobRun.Status.SUCCEEDED
        job.result = result or {}
    except Exception as exc:  # noqa: BLE001 - failures must be recorded, not crash the worker
        logger.exception("Job %s (%s) failed", job.id, job.job_type)
        job.status = JobRun.Status.FAILED
        job.error = str(exc)[:4000]
        log_decision(
            "jobs",
            f"Job {job.job_type} failed: {exc}",
            severity="error",
            job_run_id=job.id,
            payload={"job_id": str(job.id)},
        )
    job.finished_at = timezone.now()
    job.duration_seconds = round(time.monotonic() - started, 3)
    job.save(
        update_fields=["status", "result", "error", "finished_at", "duration_seconds"]
    )
    return job


def run_next_job(worker_id: str | None = None) -> JobRun | None:
    worker_id = worker_id or worker_name()
    job = _claim_next(worker_id)
    if job is None:
        return None
    return execute_job(job)


def reap_stale_jobs(max_age_minutes: int = DEFAULT_STALE_AFTER_MINUTES) -> int:
    """Fail running jobs whose worker is presumed gone.

    Each job type gets its own allowance (``STALE_AFTER_MINUTES``), so a long
    analysis is never reaped while still running, which would free its
    exclusive slot and let a second one start beside it.
    """

    now = timezone.now()
    count = 0
    running = JobRun.objects.filter(status=JobRun.Status.RUNNING)
    for job_type in set(running.values_list("job_type", flat=True)):
        limit = STALE_AFTER_MINUTES.get(job_type, max_age_minutes)
        stale = running.filter(job_type=job_type, started_at__lt=now - timedelta(minutes=limit))
        reaped = stale.update(
            status=JobRun.Status.FAILED,
            error=f"worker abandoned job (running > {limit}m)",
            finished_at=now,
        )
        count += reaped
    if count:
        logger.warning("Reaped %s stale job(s)", count)
    return count


def worker_name() -> str:
    host = socket.gethostname()
    pid = os.getpid()
    return f"{host}:{pid}:{uuid.uuid4().hex[:6]}"


def run_worker(
    *,
    poll_seconds: float = 2.0,
    max_jobs: int | None = None,
    reap_interval_seconds: float = 300.0,
) -> int:
    worker_id = worker_name()
    stop = {"flag": False}

    def _stop(signum, frame):  # noqa: ARG001
        stop["flag"] = True

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)
    processed = 0
    last_reap = 0.0
    logger.info("Worker %s started", worker_id)
    while not stop["flag"]:
        if time.monotonic() - last_reap >= reap_interval_seconds:
            reap_stale_jobs()
            last_reap = time.monotonic()
        job = run_next_job(worker_id)
        if job is not None:
            processed += 1
            logger.info("Job %s (%s) -> %s", job.id, job.job_type, job.status)
            if max_jobs and processed >= max_jobs:
                break
            continue
        time.sleep(poll_seconds)
    logger.info("Worker %s stopped after %s job(s)", worker_id, processed)
    return processed
