from django.db.models import Count
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.jobs.handlers import HANDLERS
from apps.jobs.models import JobRun, JobSchedule
from apps.jobs.runner import enqueue_job
from apps.jobs.schedules import ensure_schedules
from apps.jobs.serializers import (
    JobRunSerializer,
    JobScheduleSerializer,
    JobTriggerSerializer,
)

JOB_DESCRIPTIONS = {
    "coins_refresh": "Fetch the top 100 coins by market cap and remap Binance futures contracts.",
    "daily_analysis": "Sync data, run walk-forward backtests, score coins, write reports, bootstrap paper sessions.",
    "paper_tick": "Advance active paper sessions using live Binance public market data.",
    "paper_evaluate": "Check paper sessions against promotion thresholds.",
    "paper_risk": "Resume paper sessions paused by the overbought guard once recovery confirms.",
    "btc_open_interest": "Store BTC 1h open interest for backtesting the BTC market regime.",
}


class JobRunViewSet(viewsets.ReadOnlyModelViewSet):
    serializer_class = JobRunSerializer
    lookup_field = "id"

    def get_queryset(self):
        queryset = JobRun.objects.select_related("schedule")
        job_type = self.request.query_params.get("job_type")
        state = self.request.query_params.get("status")
        if job_type:
            queryset = queryset.filter(job_type=job_type)
        if state:
            queryset = queryset.filter(status=state)
        limit = self.request.query_params.get("limit")
        if limit:
            try:
                queryset = queryset[: max(1, min(500, int(limit)))]
            except ValueError:
                pass
        return queryset

    @action(detail=False, methods=["post"], url_path="run")
    def trigger(self, request):
        serializer = JobTriggerSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        job = enqueue_job(
            serializer.validated_data["job_type"],
            trigger=JobRun.Trigger.MANUAL,
            params=serializer.validated_data.get("params") or {},
        )
        return Response(JobRunSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def cancel(self, request, id=None):
        job = self.get_object()
        if job.status != JobRun.Status.QUEUED:
            return Response(
                {"detail": "Only queued jobs can be cancelled."},
                status=status.HTTP_409_CONFLICT,
            )
        job.status = JobRun.Status.CANCELLED
        job.finished_at = timezone.now()
        job.save(update_fields=["status", "finished_at"])
        return Response(JobRunSerializer(job).data)


class JobScheduleViewSet(viewsets.ModelViewSet):
    serializer_class = JobScheduleSerializer
    lookup_field = "slug"
    http_method_names = ["get", "patch", "post", "head", "options"]

    def get_queryset(self):
        ensure_schedules()
        return JobSchedule.objects.select_related("last_job")

    @action(detail=True, methods=["post"])
    def run(self, request, slug=None):
        schedule = self.get_object()
        params = request.data.get("params") or schedule.params or {}
        if not isinstance(params, dict):
            return Response(
                {"detail": "params must be an object"}, status=status.HTTP_400_BAD_REQUEST
            )
        job = enqueue_job(
            schedule.job_type,
            trigger=JobRun.Trigger.MANUAL,
            params=params,
            schedule=schedule,
        )
        return Response(JobRunSerializer(job).data, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def toggle(self, request, slug=None):
        schedule = self.get_object()
        schedule.enabled = not schedule.enabled
        if schedule.enabled and schedule.next_run_at is None:
            schedule.next_run_at = schedule.compute_next_run()
        schedule.save(update_fields=["enabled", "next_run_at", "updated_at"])
        return Response(JobScheduleSerializer(schedule).data)


class JobSummaryView(viewsets.ViewSet):
    def list(self, request):
        ensure_schedules()
        now = timezone.now()
        runs = JobRun.objects.all()
        counts = {
            row["status"]: row["total"] for row in runs.values("status").annotate(total=Count("id"))
        }
        recent = runs.select_related("schedule")[:10]
        schedules = JobSchedule.objects.select_related("last_job").order_by("next_run_at")
        return Response(
            {
                "now": now,
                "status_counts": counts,
                "queued": counts.get(JobRun.Status.QUEUED, 0),
                "running": counts.get(JobRun.Status.RUNNING, 0),
                "recent_runs": JobRunSerializer(recent, many=True).data,
                "schedules": JobScheduleSerializer(schedules, many=True).data,
                "job_types": [
                    {"job_type": job_type, "description": description}
                    for job_type, description in JOB_DESCRIPTIONS.items()
                    if job_type in HANDLERS
                ],
            }
        )
