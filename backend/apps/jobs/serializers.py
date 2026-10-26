from rest_framework import serializers

from apps.jobs.handlers import HANDLERS
from apps.jobs.models import JobRun, JobSchedule


class JobRunSerializer(serializers.ModelSerializer):
    schedule_slug = serializers.CharField(source="schedule.slug", read_only=True, default=None)

    class Meta:
        model = JobRun
        fields = [
            "id",
            "job_type",
            "trigger",
            "status",
            "schedule_slug",
            "params",
            "result",
            "error",
            "worker",
            "enqueued_at",
            "started_at",
            "finished_at",
            "duration_seconds",
        ]


class JobScheduleSerializer(serializers.ModelSerializer):
    last_job_id = serializers.UUIDField(read_only=True, allow_null=True)
    next_run_in_seconds = serializers.SerializerMethodField()

    class Meta:
        model = JobSchedule
        fields = [
            "slug",
            "name",
            "description",
            "job_type",
            "kind",
            "params",
            "enabled",
            "interval_seconds",
            "next_run_at",
            "last_enqueued_at",
            "last_job_id",
            "next_run_in_seconds",
        ]
        read_only_fields = ["slug", "job_type", "kind"]

    def get_next_run_in_seconds(self, obj: JobSchedule) -> float | None:
        if not obj.next_run_at:
            return None
        from django.utils import timezone

        return round((obj.next_run_at - timezone.now()).total_seconds(), 1)

    def validate_params(self, value):
        if not isinstance(value, dict):
            raise serializers.ValidationError("params must be an object")
        return value


class JobTriggerSerializer(serializers.Serializer):
    job_type = serializers.ChoiceField(choices=sorted(HANDLERS))
    params = serializers.JSONField(required=False, default=dict)

    def validate_params(self, value):
        if value in (None, ""):
            return {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("params must be an object")
        return value
