from datetime import datetime, timezone as dt_tz
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.etl_api.keys import create_client
from apps.etl_api.models import ApiClient, RateWindow

HEALTH = "/api/v1/meta/health/"


class AuthTests(TestCase):
    def setUp(self):
        self.client_row, self.key = create_client("etl-test")
        self.api = APIClient()

    def auth(self):
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {self.key}")

    def test_key_format_and_hash_at_rest(self):
        self.assertRegex(self.key, r"^mpb_[A-Za-z0-9_-]{40}$")
        self.assertEqual(self.client_row.key_prefix, self.key[:12])
        self.assertNotIn(self.key, str(ApiClient.objects.values().get()))

    def test_missing_key_401_problem_json(self):
        r = self.api.get(HEALTH)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r["Content-Type"], "application/problem+json")
        self.assertEqual(r.json()["type"], "https://marketaplusb/errors/unauthenticated")

    def test_unknown_and_malformed_key_401(self):
        self.api.credentials(HTTP_AUTHORIZATION="Bearer mpb_nope")
        self.assertEqual(self.api.get(HEALTH).status_code, 401)
        self.api.credentials(HTTP_AUTHORIZATION="Basic abc")
        self.assertEqual(self.api.get(HEALTH).status_code, 401)

    @override_settings(ENGINE_API_TOKEN="t", ENGINE_ALLOW_PUBLIC_READS=True)
    def test_engine_token_and_public_reads_do_not_apply(self):
        self.api.credentials(HTTP_X_ENGINE_TOKEN="t")
        self.assertEqual(self.api.get(HEALTH).status_code, 401)

    def test_revoked_key_403(self):
        self.client_row.revoked_at = timezone.now()
        self.client_row.save()
        self.auth()
        r = self.api.get(HEALTH)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["type"].rsplit("/", 1)[1], "revoked_key")

    def test_missing_read_scope_403(self):
        self.client_row.scopes = []
        self.client_row.save()
        self.auth()
        r = self.api.get(HEALTH)
        self.assertEqual(r.status_code, 403)
        self.assertEqual(r.json()["type"].rsplit("/", 1)[1], "insufficient_scope")

    def test_writes_405(self):
        self.auth()
        for m in ("post", "put", "patch", "delete"):
            r = getattr(self.api, m)(HEALTH)
            self.assertEqual(r.status_code, 405)
            self.assertTrue(r.json()["type"].endswith("method_not_allowed"))

    def test_health_ok_with_headers(self):
        self.auth()
        r = self.api.get(HEALTH)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r["X-RateLimit-Limit"], "120")
        self.assertIn("X-RateLimit-Remaining", r)
        self.assertIn("X-RateLimit-Reset", r)
        body = r.json()
        self.assertEqual(body["meta"]["api_version"], "1")
        self.assertTrue(body["meta"]["generated_at"].endswith("Z"))
        self.assertIn("current_strategy_version", body["data"])

    def test_last_used_at_written_once(self):
        self.auth()
        self.api.get(HEALTH)
        self.client_row.refresh_from_db()
        first = self.client_row.last_used_at
        self.assertIsNotNone(first)
        self.api.get(HEALTH)
        self.client_row.refresh_from_db()
        self.assertEqual(self.client_row.last_used_at, first)


    def test_bearer_edge_cases_401(self):
        for header in ("Bearer", "Bearer ", "Bearer    "):
            self.api.credentials(HTTP_AUTHORIZATION=header)
            r = self.api.get(HEALTH)
            self.assertEqual(r.status_code, 401, header)
            self.assertTrue(r.json()["type"].endswith("unauthenticated"))

    def test_valid_prefix_wrong_suffix_401(self):
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {self.key[:12]}WRONGSUFFIX")
        r = self.api.get(HEALTH)
        self.assertEqual(r.status_code, 401)
        self.assertTrue(r.json()["type"].endswith("unauthenticated"))

    def test_unknown_path_404_problem_json(self):
        self.auth()
        r = self.api.get("/api/v1/nope/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r["Content-Type"], "application/problem+json")
        self.assertTrue(r.json()["type"].endswith("not_found"))


FIXED_NOW = datetime(2026, 10, 30, 12, 0, 30, tzinfo=dt_tz.utc)
NEXT_MIN = int(datetime(2026, 10, 30, 12, 1, 0, tzinfo=dt_tz.utc).timestamp())


@mock.patch("apps.etl_api.throttling._now", return_value=FIXED_NOW)
class ThrottleTests(TestCase):
    def test_limit_exceeded_429_retry_after(self, _now):
        row, key = create_client("t", rate_limit_per_min=2)
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        api.get(HEALTH)
        api.get(HEALTH)
        r = api.get(HEALTH)
        self.assertEqual(r.status_code, 429)
        self.assertEqual(r["Retry-After"], "30")
        self.assertEqual(r["X-RateLimit-Reset"], str(NEXT_MIN))
        self.assertEqual(r.json()["type"].rsplit("/", 1)[1], "rate_limited")

    def test_throttle_counts_in_database(self, _now):
        row, key = create_client("t2")
        for _ in range(2):
            api = APIClient()
            api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
            self.assertEqual(api.get(HEALTH).status_code, 200)
        self.assertEqual(RateWindow.objects.get().count, 2)


class DashboardUnaffectedTests(TestCase):
    def test_dashboard_still_uses_engine_token(self):
        with override_settings(ENGINE_API_TOKEN="t", ENGINE_ALLOW_PUBLIC_READS=False):
            r = APIClient().get("/api/engine/analysis/")
            self.assertNotEqual(r.status_code, 200)
            self.assertNotIn("problem+json", r["Content-Type"])


class CommandTests(TestCase):
    def test_create_then_revoke(self):
        out = StringIO()
        call_command("etl_api_key", "create", "--name", "cmd", stdout=out)
        key = next(
            w for w in out.getvalue().split() if w.startswith("mpb_")
        )
        api = APIClient()
        api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        self.assertEqual(api.get(HEALTH).status_code, 200)
        listing = StringIO()
        call_command("etl_api_key", "list", stdout=listing)
        self.assertIn(key[:12], listing.getvalue())
        self.assertNotIn(key, listing.getvalue())
        call_command("etl_api_key", "revoke", key[:12], stdout=StringIO())
        r = api.get(HEALTH)
        self.assertEqual(r.status_code, 403)
        self.assertTrue(r.json()["type"].endswith("revoked_key"))
