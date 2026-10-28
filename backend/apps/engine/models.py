import uuid

from django.conf import settings
from django.db import models
from django.utils import timezone

from apps.coins.models import Coin


class AnalysisRun(models.Model):
    class Status(models.TextChoices):
        RUNNING = "running", "Running"
        SUCCEEDED = "succeeded", "Succeeded"
        FAILED = "failed", "Failed"

    class Trigger(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled"
        MANUAL = "manual", "Manual"
        API = "api", "API"

    class Scope(models.TextChoices):
        UNIVERSE = "universe", "Universe"
        SOLO = "solo", "Solo"

    class Mode(models.TextChoices):
        NORMAL = "normal", "Normal"
        HYBRID = "hybrid", "Hybrid"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.RUNNING)
    trigger = models.CharField(max_length=16, choices=Trigger.choices, default=Trigger.SCHEDULED)
    mode = models.CharField(
        max_length=8, choices=Mode.choices, default=Mode.NORMAL, db_index=True
    )
    scope = models.CharField(max_length=16, choices=Scope.choices, default=Scope.UNIVERSE)
    params = models.JSONField(default=dict, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    error = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    data_cutoff = models.DateTimeField(null=True, blank=True)
    duration_seconds = models.FloatField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self) -> str:
        return f"AnalysisRun {self.id} ({self.status})"


class CoinAnalysis(models.Model):
    class Suitability(models.TextChoices):
        PRIME = "prime", "Prime"
        VIABLE = "viable", "Viable"
        WATCH = "watch", "Watch"
        AVOID = "avoid", "Avoid"
        INELIGIBLE = "ineligible", "Ineligible"

    analysis = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, related_name="coin_results"
    )
    coin = models.ForeignKey(Coin, on_delete=models.PROTECT, related_name="analysis_results")
    rank = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    score = models.FloatField(default=0.0)
    suitability = models.CharField(
        max_length=16, choices=Suitability.choices, default=Suitability.INELIGIBLE
    )
    eligible = models.BooleanField(default=False)
    exclusion_reason = models.CharField(max_length=255, blank=True, default="")

    oos_metrics = models.JSONField(default=dict, blank=True)
    in_sample_metrics = models.JSONField(default=dict, blank=True)
    features = models.JSONField(default=dict, blank=True)
    component_scores = models.JSONField(default=dict, blank=True)
    params = models.JSONField(default=dict, blank=True)
    folds = models.JSONField(default=list, blank=True)
    chart = models.JSONField(default=dict, blank=True)
    trade_sample = models.JSONField(default=list, blank=True)
    diagnostics = models.JSONField(default=dict, blank=True)
    data_start = models.DateTimeField(null=True, blank=True)
    data_end = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["rank", "-score"]
        constraints = [
            models.UniqueConstraint(fields=["analysis", "coin"], name="unique_analysis_coin")
        ]
        indexes = [
            models.Index(fields=["analysis", "rank"]),
            models.Index(fields=["coin", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.coin.symbol} #{self.rank or '-'} score={self.score:.1f}"


class DecisionLog(models.Model):
    class Severity(models.TextChoices):
        INFO = "info", "Info"
        WARNING = "warning", "Warning"
        ERROR = "error", "Error"

    created_at = models.DateTimeField(default=timezone.now, db_index=True)
    category = models.CharField(max_length=64, db_index=True)
    severity = models.CharField(max_length=16, choices=Severity.choices, default=Severity.INFO)
    message = models.TextField()
    payload = models.JSONField(default=dict, blank=True)
    analysis = models.ForeignKey(
        AnalysisRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="decisions"
    )
    job_run_id = models.UUIDField(null=True, blank=True, db_index=True)
    coin = models.ForeignKey(Coin, on_delete=models.SET_NULL, null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"[{self.category}] {self.message[:80]}"


class Report(models.Model):
    class Kind(models.TextChoices):
        RANKING = "ranking", "Coin ranking"
        PERFORMANCE = "performance", "Performance"
        RISK = "risk", "Risk"
        METHODOLOGY = "methodology", "Methodology"

    analysis = models.ForeignKey(
        AnalysisRun, on_delete=models.CASCADE, null=True, blank=True, related_name="reports"
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    title = models.CharField(max_length=255)
    body_markdown = models.TextField()
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.title


def log_decision(
    category: str,
    message: str,
    *,
    severity: str = DecisionLog.Severity.INFO,
    payload: dict | None = None,
    analysis: AnalysisRun | None = None,
    job_run_id=None,
    coin: Coin | None = None,
) -> DecisionLog:
    return DecisionLog.objects.create(
        category=category,
        message=message,
        severity=severity,
        payload=payload or {},
        analysis=analysis,
        job_run_id=job_run_id,
        coin=coin,
    )
