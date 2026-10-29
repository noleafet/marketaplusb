"""Only one grid suitability analysis may be queued or running at a time."""

from datetime import timedelta
from io import StringIO
from unittest.mock import patch

import psycopg
from django.core.management import CommandError, call_command
from django.db import IntegrityError, connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.engine.analysis import ANALYSIS_LOCK_KEY, AnalysisAlreadyRunning, run_daily_analysis
from apps.engine.models import DecisionLog
from apps.jobs.models import JobRun, JobSchedule
from apps.jobs.runner import (
    DuplicateJobQueued,
    JobAlreadyActive,
    enqueue_due_schedules,
    enqueue_job,
    reap_stale_jobs,
    run_next_job,
)
from apps.jobs.schedules import ensure_schedules

TOKEN = "test-engine-token"


class ExclusiveEnqueueTests(TestCase):
    def test_second_analysis_is_refused_while_one_is_queued(self):
        first = enqueue_job("daily_analysis")
        with self.assertRaises(JobAlreadyActive) as raised:
            enqueue_job("daily_analysis", params={"symbols": ["BTC"]})
        self.assertEqual(raised.exception.active.id, first.id)
        self.assertEqual(JobRun.objects.filter(job_type="daily_analysis").count(), 1)

    def test_second_analysis_is_refused_while_one_is_running(self):
        first = enqueue_job("daily_analysis")
        first.status = JobRun.Status.RUNNING
        first.save()
        with self.assertRaises(JobAlreadyActive):
            enqueue_job("daily_analysis")

    def test_a_finished_analysis_does_not_block_the_next(self):
        for status in (JobRun.Status.SUCCEEDED, JobRun.Status.FAILED, JobRun.Status.CANCELLED):
            job = enqueue_job("daily_analysis")
            job.status = status
            job.save()
        self.assertEqual(enqueue_job("daily_analysis").status, JobRun.Status.QUEUED)

    def test_other_job_types_are_not_exclusive(self):
        enqueue_job("daily_analysis")
        # Distinct params, so these are not duplicates of each other either.
        enqueue_job("paper_tick", params={"session_ids": ["a"]})
        enqueue_job("paper_tick", params={"session_ids": ["b"]})
        self.assertEqual(JobRun.objects.filter(job_type="paper_tick").count(), 2)

    def test_database_constraint_rejects_a_direct_second_insert(self):
        JobRun.objects.create(job_type="daily_analysis", status=JobRun.Status.RUNNING)
        with self.assertRaises(IntegrityError), transaction.atomic():
            JobRun.objects.create(job_type="daily_analysis", status=JobRun.Status.QUEUED)

    def test_race_past_the_pre_check_is_still_refused(self):
        first = enqueue_job("daily_analysis")
        # Simulate a concurrent request whose pre-check ran before ``first``
        # existed: only the database constraint stands in the way.
        with patch("apps.jobs.runner.active_exclusive_job", side_effect=[None, first]):
            with self.assertRaises(JobAlreadyActive) as raised:
                enqueue_job("daily_analysis")
        self.assertEqual(raised.exception.active.id, first.id)
        self.assertEqual(JobRun.objects.filter(job_type="daily_analysis").count(), 1)

    def test_worker_runs_the_single_queued_analysis(self):
        enqueue_job("daily_analysis")
        with patch("apps.jobs.runner.run_handler", return_value={"ok": True}) as handler:
            job = run_next_job("test-worker")
        handler.assert_called_once()
        self.assertEqual(job.status, JobRun.Status.SUCCEEDED)
        self.assertEqual(enqueue_job("daily_analysis").status, JobRun.Status.QUEUED)


@override_settings(ENGINE_API_TOKEN=TOKEN, ENGINE_ALLOW_PUBLIC_READS=True)
class ExclusiveApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.active = enqueue_job("daily_analysis")

    def post(self, url, body=None):
        return self.client.post(url, body or {}, format="json", HTTP_X_ENGINE_TOKEN=TOKEN)

    def assert_conflict(self, response):
        self.assertEqual(response.status_code, 409, response.content)
        body = response.json()
        self.assertEqual(body["active_job_id"], str(self.active.id))
        self.assertIn("wait for it to finish", body["detail"])
        self.assertEqual(JobRun.objects.filter(job_type="daily_analysis").count(), 1)

    def test_engine_trigger_returns_conflict(self):
        self.assert_conflict(self.post("/api/engine/analysis/trigger/"))

    def test_generic_job_trigger_returns_conflict(self):
        self.assert_conflict(self.post("/api/jobs/runs/run/", {"job_type": "daily_analysis"}))

    def test_schedule_run_now_returns_conflict(self):
        ensure_schedules()
        self.assert_conflict(self.post("/api/jobs/schedules/daily-analysis/run/"))

    def test_settings_rerun_saves_settings_and_reports_the_conflict(self):
        from apps.engine.models import AnalysisRun

        AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED,
            scope=AnalysisRun.Scope.UNIVERSE,
            params={"interval": "4h"},
        )
        response = self.post(
            "/api/strategy/settings/", {"signal_interval": "1h", "rerun_analysis": True}
        )
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["signal_interval"], "1h")
        self.assertIn("wait for it to finish", response.json()["job_conflict"])
        self.assertEqual(JobRun.objects.filter(job_type="daily_analysis").count(), 1)

    @patch("apps.coins.views.ensure_manual_coin")
    def test_adding_a_coin_keeps_the_coin_and_skips_its_analysis(self, ensure):
        from apps.coins.models import Coin

        ensure.return_value = Coin.objects.create(
            coingecko_id="tao", symbol="TAO", name="Bittensor", binance_symbol="TAOUSDT"
        )
        response = self.post("/api/coins/add/", {"symbol": "TAO"})
        self.assertEqual(response.status_code, 202, response.content)
        self.assertIsNone(response.json()["job"])
        self.assertIn("TAO was added", response.json()["detail"])
        self.assertEqual(JobRun.objects.filter(job_type="daily_analysis").count(), 1)


class ExclusiveSchedulerTests(TestCase):
    def test_due_schedule_is_skipped_while_an_analysis_is_active(self):
        ensure_schedules()
        schedule = JobSchedule.objects.get(slug="daily-analysis")
        now = timezone.now()
        schedule.next_run_at = now - timedelta(minutes=1)
        schedule.save()
        active = enqueue_job("daily_analysis")
        active.status = JobRun.Status.RUNNING
        active.save()
        JobSchedule.objects.exclude(slug="daily-analysis").update(enabled=False)

        enqueued = enqueue_due_schedules(now=now)

        self.assertEqual(enqueued, [])
        self.assertEqual(JobRun.objects.filter(job_type="daily_analysis").count(), 1)
        schedule.refresh_from_db()
        self.assertGreater(schedule.next_run_at, now)
        self.assertTrue(
            DecisionLog.objects.filter(message__contains="Scheduled daily_analysis skipped").exists()
        )


class ExclusiveCliTests(TestCase):
    def test_inline_runjob_refuses_while_an_analysis_is_queued(self):
        enqueue_job("daily_analysis")
        with self.assertRaises(CommandError) as raised:
            call_command("runjob", "daily_analysis", stdout=StringIO())
        self.assertIn("wait for it to finish", str(raised.exception))
        self.assertEqual(JobRun.objects.filter(job_type="daily_analysis").count(), 1)

    def test_enqueue_runjob_refuses_while_an_analysis_is_running(self):
        job = enqueue_job("daily_analysis")
        job.status = JobRun.Status.RUNNING
        job.save()
        with self.assertRaises(CommandError):
            call_command("runjob", "daily_analysis", "--enqueue", stdout=StringIO())


class StaleReaperTests(TestCase):
    def _running(self, job_type, minutes_ago):
        job = JobRun.objects.create(
            job_type=job_type,
            status=JobRun.Status.RUNNING,
            started_at=timezone.now() - timedelta(minutes=minutes_ago),
        )
        return job

    def test_a_long_analysis_is_not_reaped_like_a_short_job(self):
        analysis = self._running("daily_analysis", 90)
        tick = self._running("paper_tick", 90)
        reap_stale_jobs()
        analysis.refresh_from_db()
        tick.refresh_from_db()
        # Reaping a live analysis would free its slot for a second one.
        self.assertEqual(analysis.status, JobRun.Status.RUNNING)
        self.assertEqual(tick.status, JobRun.Status.FAILED)

    def test_an_abandoned_analysis_is_eventually_reaped(self):
        analysis = self._running("daily_analysis", 7 * 60)
        reap_stale_jobs()
        analysis.refresh_from_db()
        self.assertEqual(analysis.status, JobRun.Status.FAILED)
        self.assertEqual(enqueue_job("daily_analysis").status, JobRun.Status.QUEUED)


class AnalysisLockTests(TransactionTestCase):
    """The advisory lock also blocks runs started outside the job queue."""

    def test_analysis_refuses_to_start_while_another_session_holds_the_lock(self):
        db = connection.settings_dict
        other = psycopg.connect(
            host=db["HOST"] or "127.0.0.1",
            port=db["PORT"] or 5432,
            dbname=db["NAME"],
            user=db["USER"],
            password=db["PASSWORD"],
        )
        try:
            other.execute("SELECT pg_advisory_lock(%s)", [ANALYSIS_LOCK_KEY])
            with patch("apps.engine.analysis._run_daily_analysis") as inner:
                with self.assertRaises(AnalysisAlreadyRunning):
                    run_daily_analysis(trigger="manual")
            inner.assert_not_called()
        finally:
            # Unlock explicitly: the server frees a closed session's locks
            # asynchronously, which made the next assertion race the close.
            other.execute("SELECT pg_advisory_unlock(%s)", [ANALYSIS_LOCK_KEY])
            other.close()

        # Once the other session has released it the lock is free again.
        with patch("apps.engine.analysis._run_daily_analysis", return_value="ran") as inner:
            self.assertEqual(run_daily_analysis(trigger="manual"), "ran")
        inner.assert_called_once()


class DuplicateQueuedJobTests(TestCase):
    def test_identical_queued_job_is_refused(self):
        first = enqueue_job("paper_tick", params={"session_ids": ["a"]})
        with self.assertRaises(DuplicateJobQueued) as raised:
            enqueue_job("paper_tick", params={"session_ids": ["a"]})
        self.assertEqual(raised.exception.active.id, first.id)
        self.assertIn("already queued", raised.exception.message)
        self.assertEqual(JobRun.objects.filter(job_type="paper_tick").count(), 1)

    def test_empty_and_missing_params_are_the_same_job(self):
        enqueue_job("paper_evaluate")
        with self.assertRaises(DuplicateJobQueued):
            enqueue_job("paper_evaluate", params={})

    def test_different_params_or_types_are_not_duplicates(self):
        enqueue_job("paper_tick")
        enqueue_job("paper_tick", params={"session_ids": ["a"]})
        enqueue_job("paper_tick", params={"session_ids": ["b"]})
        enqueue_job("paper_evaluate")
        self.assertEqual(JobRun.objects.filter(status=JobRun.Status.QUEUED).count(), 4)

    def test_a_running_job_does_not_block_the_next_queued_one(self):
        running = enqueue_job("paper_tick")
        running.status = JobRun.Status.RUNNING
        running.save()
        self.assertEqual(enqueue_job("paper_tick").status, JobRun.Status.QUEUED)
        # ...but a second identical one behind it is still a duplicate.
        with self.assertRaises(DuplicateJobQueued):
            enqueue_job("paper_tick")

    def test_database_constraint_rejects_a_direct_duplicate(self):
        JobRun.objects.create(job_type="paper_tick", status=JobRun.Status.QUEUED, params={"x": 1})
        with self.assertRaises(IntegrityError), transaction.atomic():
            JobRun.objects.create(
                job_type="paper_tick", status=JobRun.Status.QUEUED, params={"x": 1}
            )

    def test_race_past_the_pre_check_is_still_refused(self):
        first = enqueue_job("paper_tick")
        with patch("apps.jobs.runner.queued_duplicate", side_effect=[None, first]):
            with self.assertRaises(DuplicateJobQueued):
                enqueue_job("paper_tick")
        self.assertEqual(JobRun.objects.filter(job_type="paper_tick").count(), 1)

    def test_scheduler_does_not_stack_ticks_while_no_worker_runs(self):
        ensure_schedules()
        JobSchedule.objects.exclude(slug="paper-tick").update(enabled=False)
        now = timezone.now()
        JobSchedule.objects.filter(slug="paper-tick").update(next_run_at=now - timedelta(minutes=1))
        self.assertEqual(len(enqueue_due_schedules(now=now)), 1)

        later = now + timedelta(minutes=10)
        JobSchedule.objects.filter(slug="paper-tick").update(next_run_at=later - timedelta(minutes=1))
        logs_before = DecisionLog.objects.count()
        self.assertEqual(enqueue_due_schedules(now=later), [])
        self.assertEqual(JobRun.objects.filter(job_type="paper_tick").count(), 1)
        # Routine skips are not written to the decision log.
        self.assertEqual(DecisionLog.objects.count(), logs_before)
        self.assertGreater(JobSchedule.objects.get(slug="paper-tick").next_run_at, later)

    def test_migration_cancels_existing_duplicates_keeping_the_oldest(self):
        from importlib import import_module

        from django.apps import apps as global_apps

        migration = import_module("apps.jobs.migrations.0003_no_duplicate_queued_job")
        with connection.cursor() as cursor:
            cursor.execute("ALTER TABLE jobs_jobrun DROP CONSTRAINT IF EXISTS no_duplicate_queued_job")
            cursor.execute("DROP INDEX IF EXISTS no_duplicate_queued_job")
        oldest = JobRun.objects.create(job_type="paper_tick", status=JobRun.Status.QUEUED)
        newer = JobRun.objects.create(
            job_type="paper_tick",
            status=JobRun.Status.QUEUED,
            enqueued_at=oldest.enqueued_at + timedelta(seconds=1),
        )
        migration.cancel_queued_duplicates(global_apps, None)
        oldest.refresh_from_db()
        newer.refresh_from_db()
        self.assertEqual(oldest.status, JobRun.Status.QUEUED)
        self.assertEqual(newer.status, JobRun.Status.CANCELLED)


@override_settings(ENGINE_API_TOKEN=TOKEN, ENGINE_ALLOW_PUBLIC_READS=True)
class DuplicateQueuedApiTests(TestCase):
    def test_paper_tick_endpoints_return_conflict_for_a_duplicate(self):
        client = APIClient()
        first = client.post(
            "/api/jobs/runs/run/", {"job_type": "paper_tick"}, format="json",
            HTTP_X_ENGINE_TOKEN=TOKEN,
        )
        self.assertEqual(first.status_code, 202)
        second = client.post(
            "/api/paper/sessions/tick/", {}, format="json", HTTP_X_ENGINE_TOKEN=TOKEN
        )
        self.assertEqual(second.status_code, 409, second.content)
        self.assertEqual(second.json()["active_job_id"], first.json()["id"])
        self.assertIn("already queued", second.json()["detail"])
