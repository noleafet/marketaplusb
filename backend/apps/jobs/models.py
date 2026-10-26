import uuid

from django.db import models
from django.utils import timezone


class JobSchedule(models.Model):
    class Kind(models.TextChoices):
        MONTHLY = "monthly", "Monthly"
        DAILY = "daily", "Daily"
        INTERVAL = "interval", "Interval"

    slug = models.SlugField(max_length=64, unique=True)
    name = models.CharField(max_length=128)
    description = models.CharField(max_length=255, blank=True, default="")
    job_type = models.CharField(max_length=48, db_index=True)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    params = models.JSONField(default=dict, blank=True)
    enabled = models.BooleanField(default=True)
    interval_seconds = models.PositiveIntegerField(null=True, blank=True)
    next_run_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)
    last_job = models.ForeignKey(
        "jobs.JobRun",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="schedules_triggered",
    )
    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["slug"]

    def __str__(self) -> str:
        return f"{self.slug} -> {self.job_type}"

    def compute_next_run(self, now=None):
        from apps.jobs.schedules import next_run_for

        return next_run_for(self, now=now)


class JobRun(models.Model):
    class Status(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    class Trigger(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        MANUAL = "manual", "Manual"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    job_type = models.CharField(max_length=48, db_index=True)
    trigger = models.CharField(max_length=16, choices=Trigger.choices, default=Trigger.SCHEDULED)
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.QUEUED, db_index=True
    )
    schedule = models.ForeignKey(
        JobSchedule, on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
    params = models.JSONField(default=dict, blank=True)
    result = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    worker = models.CharField(max_length=128, blank=True, default="")
    enqueued_at = models.DateTimeField(default=timezone.now, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-enqueued_at"]
        indexes = [
            models.Index(fields=["status", "enqueued_at"]),
            models.Index(fields=["job_type", "-enqueued_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.job_type} [{self.status}] {self.id}"
