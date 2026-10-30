"""BTC market regime protection."""

from datetime import datetime, timezone as dt_timezone

from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.engine.versioning import strategy_snapshot
from apps.trading.models import MarketRegimeState, OpenInterestPoint, StrategySettings

TOKEN = "test-engine-token"


@override_settings(ENGINE_API_TOKEN=TOKEN, ENGINE_ALLOW_PUBLIC_READS=True)
class RegimeModelTests(TestCase):
    def test_settings_defaults(self):
        row = StrategySettings.load()
        self.assertTrue(row.btc_regime_enabled)
        self.assertAlmostEqual(row.btc_trailing_stop_pct, 0.08)
        self.assertEqual(row.btc_trailing_window_days, 7)
        self.assertAlmostEqual(row.btc_oi_flush_pct, 0.05)
        self.assertAlmostEqual(row.btc_oi_near_high_pct, 0.03)
        self.assertAlmostEqual(row.btc_funding_z, 2.0)
        self.assertAlmostEqual(row.btc_min_bearish_hours, 4)

    def test_regime_state_singleton_defaults(self):
        first = MarketRegimeState.load()
        second = MarketRegimeState.load()
        self.assertEqual(first.pk, second.pk)
        self.assertEqual(first.phase, "normal")
        self.assertEqual(first.reasons, [])
        self.assertEqual(first.last_error, "")

    def test_open_interest_point_unique(self):
        moment = datetime(2026, 10, 1, tzinfo=dt_timezone.utc)
        OpenInterestPoint.objects.create(symbol="BTCUSDT", time=moment, open_interest=1.0)
        with self.assertRaises(IntegrityError), transaction.atomic():
            OpenInterestPoint.objects.create(symbol="BTCUSDT", time=moment, open_interest=2.0)

    def test_settings_api_validates_regime_fields(self):
        client = APIClient()
        bad = client.post(
            "/api/strategy/settings/",
            {"btc_trailing_stop_pct": 0.5, "btc_min_bearish_hours": 100},
            format="json",
            HTTP_X_ENGINE_TOKEN=TOKEN,
        )
        self.assertEqual(bad.status_code, 400)
        self.assertIn("btc_trailing_stop_pct", bad.json())
        self.assertIn("btc_min_bearish_hours", bad.json())

        good = client.post(
            "/api/strategy/settings/",
            {"btc_trailing_stop_pct": 0.1},
            format="json",
            HTTP_X_ENGINE_TOKEN=TOKEN,
        )
        self.assertEqual(good.status_code, 200, good.content)
        self.assertAlmostEqual(StrategySettings.load().btc_trailing_stop_pct, 0.1)

    def test_regime_fields_in_strategy_snapshot(self):
        snapshot = strategy_snapshot()
        for name in (
            "btc_regime_enabled",
            "btc_trailing_stop_pct",
            "btc_trailing_window_days",
            "btc_oi_flush_pct",
            "btc_oi_near_high_pct",
            "btc_funding_z",
            "btc_min_bearish_hours",
        ):
            self.assertIn(name, snapshot)
