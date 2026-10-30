from importlib import import_module

from django.apps import apps as django_apps
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.engine.models import AnalysisRun
from apps.engine.versioning import resolve_strategy_version
from apps.trading.models import StrategySettings


class FieldTests(TestCase):
    def test_defaults(self):
        self.assertTrue(StrategySettings.load().run_comparison_analysis)
        self.assertFalse(AnalysisRun.objects.create().drives_sessions)

    def test_comparison_toggle_does_not_change_strategy_version(self):
        a = resolve_strategy_version()
        row = StrategySettings.load()
        row.run_comparison_analysis = False
        row.save()
        self.assertEqual(resolve_strategy_version(row).id, a.id)

    def test_settings_api_exposes_and_updates_toggle(self):
        client = APIClient()
        client.credentials(HTTP_X_ENGINE_TOKEN="t")
        with override_settings(ENGINE_API_TOKEN="t"):
            got = client.get("/api/strategy/settings/")
            self.assertEqual(got.status_code, 200)
            self.assertIs(got.json()["run_comparison_analysis"], True)
            resp = client.post(
                "/api/strategy/settings/", {"run_comparison_analysis": False}, format="json"
            )
            self.assertEqual(resp.status_code, 200)
        self.assertFalse(StrategySettings.load().run_comparison_analysis)

    def test_backfill_marks_only_session_driving_runs(self):
        drove = AnalysisRun.objects.create(
            mode="normal", scope="universe", summary={"paper_sessions_created": 0})
        solo = AnalysisRun.objects.create(
            mode="normal", scope="solo", summary={"paper_sessions_created": 1})
        hyb = AnalysisRun.objects.create(mode="hybrid", scope="universe", summary={})
        old = AnalysisRun.objects.create(mode="normal", scope="universe", summary={})
        import_module(
            "apps.engine.migrations.0009_analysisrun_drives_sessions"
        ).backfill_drives_sessions(django_apps, None)
        for run in (drove, solo, hyb, old):
            run.refresh_from_db()
        self.assertTrue(drove.drives_sessions)
        self.assertFalse(solo.drives_sessions)
        self.assertFalse(hyb.drives_sessions)
        self.assertFalse(old.drives_sessions)
