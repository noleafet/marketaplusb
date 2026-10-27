from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.jobs.models import JobRun
from apps.jobs.runner import enqueue_job, run_next_job
from apps.jobs.schedules import ensure_schedules

TOKEN = "test-engine-token"


@override_settings(ENGINE_API_TOKEN=TOKEN, ENGINE_ALLOW_PUBLIC_READS=True)
class JobApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_mutation_requires_engine_token(self):
        response = self.client.post(
            "/api/jobs/runs/run/", {"job_type": "coins_refresh"}, format="json"
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(JobRun.objects.count(), 0)

    def test_mutation_succeeds_with_engine_token(self):
        response = self.client.post(
            "/api/jobs/runs/run/",
            {"job_type": "coins_refresh", "params": {"per_page": 100}},
            format="json",
            HTTP_X_ENGINE_TOKEN=TOKEN,
        )
        self.assertEqual(response.status_code, 202)
        job = JobRun.objects.get()
        self.assertEqual(job.status, JobRun.Status.QUEUED)
        self.assertEqual(job.params, {"per_page": 100})
        self.assertEqual(response.data["id"], str(job.id))

    def test_unknown_job_type_is_rejected(self):
        response = self.client.post(
            "/api/jobs/runs/run/",
            {"job_type": "not_a_job"},
            format="json",
            HTTP_X_ENGINE_TOKEN=TOKEN,
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(JobRun.objects.count(), 0)

    def test_public_reads_follow_setting(self):
        response = self.client.get("/api/jobs/runs/")
        self.assertEqual(response.status_code, 200)
        with override_settings(ENGINE_ALLOW_PUBLIC_READS=False):
            blocked = self.client.get("/api/jobs/runs/")
        self.assertEqual(blocked.status_code, 403)

    def test_token_authorizes_reads_when_public_reads_disabled(self):
        with override_settings(ENGINE_ALLOW_PUBLIC_READS=False):
            response = self.client.get("/api/jobs/runs/", HTTP_X_ENGINE_TOKEN=TOKEN)
        self.assertEqual(response.status_code, 200)

    def test_only_queued_jobs_can_be_cancelled(self):
        queued = enqueue_job("coins_refresh")
        response = self.client.post(
            f"/api/jobs/runs/{queued.id}/cancel/", HTTP_X_ENGINE_TOKEN=TOKEN
        )
        self.assertEqual(response.status_code, 200)
        queued.refresh_from_db()
        self.assertEqual(queued.status, JobRun.Status.CANCELLED)

        finished = JobRun.objects.create(
            job_type="coins_refresh", status=JobRun.Status.SUCCEEDED
        )
        response = self.client.post(
            f"/api/jobs/runs/{finished.id}/cancel/", HTTP_X_ENGINE_TOKEN=TOKEN
        )
        self.assertEqual(response.status_code, 409)
        finished.refresh_from_db()
        self.assertEqual(finished.status, JobRun.Status.SUCCEEDED)


class ScheduleTests(TestCase):
    def test_default_schedules_capture_requested_cadence(self):
        schedules = {schedule.slug: schedule for schedule in ensure_schedules()}

        self.assertEqual(schedules["coins-refresh"].kind, "monthly")
        self.assertEqual(schedules["coins-refresh"].next_run_at.day, 1)
        self.assertEqual(schedules["coins-refresh"].next_run_at.hour, 0)
        self.assertEqual(schedules["daily-analysis"].kind, "daily")
        self.assertEqual(schedules["daily-analysis"].params["offset_minutes"], 10)
        self.assertEqual(
            schedules["paper-tick"].interval_seconds,
            int(settings.PAPER["TICK_MINUTES"]) * 60,
        )
        self.assertEqual(
            schedules["paper-evaluate"].interval_seconds,
            int(settings.PAPER["EVAL_INTERVAL_HOURS"]) * 3600,
        )


class WorkerTests(TestCase):
    @patch("apps.jobs.runner.run_handler", return_value={"processed": 1})
    def test_worker_claims_and_executes_queued_job(self, run_handler):
        queued = JobRun.objects.create(job_type="coins_refresh")

        result = run_next_job("test-worker")

        self.assertIsNotNone(result)
        self.assertEqual(result.id, queued.id)
        queued.refresh_from_db()
        self.assertEqual(queued.status, JobRun.Status.SUCCEEDED)
        self.assertEqual(queued.result, {"processed": 1})
        self.assertEqual(queued.worker, "test-worker")
        self.assertIsNotNone(queued.started_at)
        self.assertIsNotNone(queued.finished_at)
        self.assertIsNotNone(queued.duration_seconds)
        run_handler.assert_called_once()
        self.assertEqual(run_handler.call_args.args[0], "coins_refresh")

    @patch("apps.jobs.runner.run_handler", side_effect=RuntimeError("boom"))
    def test_worker_records_failure_without_raising(self, _run_handler):
        queued = JobRun.objects.create(job_type="coins_refresh")

        result = run_next_job("test-worker")

        self.assertIsNotNone(result)
        queued.refresh_from_db()
        self.assertEqual(queued.status, JobRun.Status.FAILED)
        self.assertIn("boom", queued.error)
        self.assertIsNotNone(queued.finished_at)

    @patch("apps.jobs.runner.run_handler")
    def test_worker_returns_none_when_queue_is_empty(self, _run_handler):
        self.assertIsNone(run_next_job("test-worker"))
