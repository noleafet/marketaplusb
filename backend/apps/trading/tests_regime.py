"""BTC market regime protection."""

import math
from datetime import datetime, timedelta, timezone as dt_timezone

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


T0 = datetime(2026, 10, 1, 12, 0, tzinfo=dt_timezone.utc)
NEUTRAL_OI = (100.0, 100.1, 100.2, 100.3, 100.4, 100.5)


def regime_config(**overrides):
    from django.conf import settings

    from apps.trading.regime import RegimeConfig

    values = {
        "trailing_stop_pct": 0.08,
        "trailing_window_days": 7,
        "oi_flush_pct": 0.05,
        "oi_near_high_pct": 0.03,
        "funding_z": 2.0,
        "min_bearish_hours": 4.0,
        "risk": settings.RISK,
    }
    values.update(overrides)
    return RegimeConfig(**values)


def reading(**overrides):
    from apps.trading.regime import RegimeReadings

    values = {
        "time": T0,
        "price": 100.0,
        "bar_high": 100.0,
        "rsi": 50.0,
        "rsi_prev": 49.0,
        "macd_hist": (0.1, 0.2, 0.3, 0.4),
        "vwap": 99.0,
        "window_high": 101.0,
        "high_24h": 101.0,
        "close_1h": 100.0,
        "support": 90.0,
        "atr": 2.0,
        "momentum": 0.01,
        "trend_up": True,
        "funding_z": 0.0,
        "oi_values": NEUTRAL_OI,
    }
    values.update(overrides)
    if "price" in overrides and "bar_high" not in overrides:
        values["bar_high"] = values["price"]
    return RegimeReadings(**values)


class RegimeEvaluatorTests(TestCase):
    def evaluate(self, phase="normal", entered_at=None, guard_state=None, config=None, **fields):
        from apps.trading.regime import evaluate_regime

        return evaluate_regime(
            phase, entered_at, guard_state or {}, reading(**fields), config or regime_config()
        )

    def test_neutral_stays_normal(self):
        decision = self.evaluate()
        self.assertEqual(decision.phase, "normal")
        self.assertEqual(decision.reasons, [])
        self.assertFalse(decision.changed)

    def test_trailing_stop_threshold(self):
        self.assertEqual(
            self.evaluate(window_high=100.0, high_24h=100.0, price=92.01).phase, "normal"
        )
        decision = self.evaluate(window_high=100.0, high_24h=100.0, price=92.0)
        self.assertEqual(decision.phase, "bearish")
        self.assertIn("trailing_stop", decision.reasons)
        self.assertTrue(decision.changed)
        self.assertEqual(decision.entered_at, T0)

    def test_oi_flush_near_high_is_bearish(self):
        falling = (100.0, 99.0, 98.0, 97.0, 96.0, 94.9)
        decision = self.evaluate(oi_values=falling, price=99.0, high_24h=100.0)
        self.assertEqual(decision.phase, "bearish")
        self.assertIn("oi_flush", decision.reasons)
        self.assertEqual(
            self.evaluate(oi_values=falling, price=96.0, high_24h=100.0).phase, "normal"
        )

    def test_support_breakdown(self):
        decision = self.evaluate(close_1h=89.4)
        self.assertEqual(decision.phase, "bearish")
        self.assertIn("support_breakdown", decision.reasons)
        self.assertEqual(self.evaluate(close_1h=89.6).phase, "normal")

    def test_funding_crowding_is_warning_only(self):
        decision = self.evaluate(funding_z=2.0, momentum=-0.01)
        self.assertEqual(decision.phase, "warning")
        self.assertEqual(decision.reasons, ["funding_crowded"])
        self.assertEqual(
            self.evaluate(funding_z=2.0, momentum=0.01, trend_up=True).phase, "normal"
        )
        self.assertEqual(self.evaluate(funding_z=None, momentum=-0.01).phase, "normal")

    def test_guard_armed_is_warning(self):
        decision = self.evaluate(rsi=80.0)
        self.assertEqual(decision.phase, "warning")
        self.assertIn("guard_armed", decision.reasons)
        self.assertEqual(decision.guard_state["phase"], "armed")

    def test_guard_exit_escalates_to_bearish(self):
        armed = {
            "phase": "armed",
            "armed_at": (T0 - timedelta(hours=1)).isoformat(),
            "peak_high": 100.0,
            "peak_at": (T0 - timedelta(hours=1)).isoformat(),
        }
        decision = self.evaluate(
            phase="warning", entered_at=T0 - timedelta(hours=1), guard_state=armed, price=93.9
        )
        self.assertEqual(decision.phase, "bearish")
        self.assertIn("guard_exit", decision.reasons)
        self.assertEqual(decision.guard_state["phase"], "paused")

    def test_normal_jumps_straight_to_bearish(self):
        decision = self.evaluate(phase="normal", close_1h=80.0)
        self.assertEqual(decision.phase, "bearish")
        self.assertTrue(decision.changed)

    def test_warning_clears_when_signals_clear(self):
        decision = self.evaluate(phase="warning", entered_at=T0 - timedelta(hours=2))
        self.assertEqual(decision.phase, "normal")
        self.assertTrue(decision.changed)
        self.assertEqual(decision.entered_at, T0)

    def test_recovery_requires_every_check_and_min_hours(self):
        early = self.evaluate(phase="bearish", entered_at=T0 - timedelta(hours=3, minutes=59))
        self.assertEqual(early.phase, "bearish")
        self.assertFalse(early.changed)
        self.assertEqual(early.entered_at, T0 - timedelta(hours=3, minutes=59))

        entered = T0 - timedelta(hours=4)
        recovered = self.evaluate(phase="bearish", entered_at=entered)
        self.assertEqual(recovered.phase, "normal")
        self.assertTrue(recovered.changed)

        failing = {
            "rsi": {"rsi": 30.0, "rsi_prev": 29.0},
            "vwap": {"vwap": 101.0},
            "support": {"close_1h": 89.4},
            "trailing": {"price": 92.0, "window_high": 100.0, "high_24h": 100.0},
            "oi": {"oi_values": (100.0, 100.1, 100.2, 100.3, 100.4, 100.3)},
        }
        for name, fields in failing.items():
            with self.subTest(check=name):
                decision = self.evaluate(phase="bearish", entered_at=entered, **fields)
                self.assertEqual(decision.phase, "bearish")
                self.assertFalse(decision.changed)

    def test_recovery_lands_in_warning_if_warning_signal_holds(self):
        decision = self.evaluate(
            phase="bearish",
            entered_at=T0 - timedelta(hours=5),
            funding_z=2.5,
            momentum=-0.02,
        )
        self.assertEqual(decision.phase, "warning")
        self.assertEqual(decision.reasons, ["funding_crowded"])

    def test_bearish_metrics_include_recovery_checklist(self):
        decision = self.evaluate(phase="bearish", entered_at=T0 - timedelta(hours=1))
        checks = decision.metrics["recovery"]
        self.assertFalse(checks["min_duration"])
        self.assertTrue(checks["above_support"])


def synthetic_bars(start, count, step_minutes, base=100.0):
    bars = []
    for i in range(count):
        moment = start + timedelta(minutes=step_minutes * i)
        mid = base * (1.0 + 0.05 * math.sin(i / 37.0) + 0.02 * math.sin(i / 5.0))
        bars.append(
            {
                "time": moment,
                "open": mid,
                "high": mid * 1.004,
                "low": mid * 0.996,
                "close": mid * (1.0 + 0.001 * math.sin(i / 3.0)),
                "volume": 10.0 + (i % 7),
            }
        )
    return bars


class RegimeReadingSeriesTests(TestCase):
    def setUp(self):
        self.start = datetime(2026, 8, 1, tzinfo=dt_timezone.utc)
        self.bars_1h = synthetic_bars(self.start, 50 * 24, 60)
        self.bars_15m = synthetic_bars(self.start, 50 * 96, 15)
        self.funding = [
            {"time": self.start + timedelta(hours=8 * i), "rate": 0.0001 * (1 + math.sin(i))}
            for i in range(150)
        ]
        oi_start = self.start + timedelta(days=47)
        self.oi = [
            {"time": oi_start + timedelta(hours=i), "open_interest": 1000.0 + i}
            for i in range(3 * 24)
        ]

    def series(self, bars_15m, bars_1h, funding, oi):
        from apps.trading.regime import reading_series

        return reading_series(bars_15m, bars_1h, funding, oi, regime_config())

    def test_reading_series_starts_after_warm_up(self):
        readings = self.series(self.bars_15m, self.bars_1h, self.funding, self.oi)
        self.assertTrue(readings)
        self.assertGreaterEqual(readings[0].time - self.start, timedelta(days=45))
        self.assertEqual(readings[-1].time, self.bars_15m[-1]["time"] + timedelta(minutes=15))

    def test_reading_series_is_causal(self):
        full = self.series(self.bars_15m, self.bars_1h, self.funding, self.oi)
        k = len(self.bars_15m) - 200
        cutoff = self.bars_15m[k]["time"] + timedelta(minutes=15)
        truncated = self.series(
            self.bars_15m[: k + 1],
            [bar for bar in self.bars_1h if bar["time"] + timedelta(hours=1) <= cutoff],
            [row for row in self.funding if row["time"] <= cutoff],
            [row for row in self.oi if row["time"] <= cutoff],
        )
        self.assertEqual(truncated[-1].time, cutoff)
        by_time = {item.time: item for item in full}
        for item in truncated:
            self.assertEqual(item, by_time[item.time])

    def test_reading_series_oi_absent_before_coverage(self):
        readings = self.series(self.bars_15m, self.bars_1h, self.funding, self.oi)
        first_oi = self.oi[0]["time"]
        before = [item for item in readings if item.time < first_oi + timedelta(hours=1)]
        last_oi = self.oi[-1]["time"]
        after = [
            item for item in readings if first_oi + timedelta(hours=6) <= item.time <= last_oi
        ]
        stale = [item for item in readings if item.time > last_oi + timedelta(hours=5)]
        self.assertTrue(before)
        self.assertTrue(all(item.oi_values == () for item in before))
        self.assertTrue(after)
        self.assertTrue(all(len(item.oi_values) == 6 for item in after))
        self.assertTrue(all(item.oi_values == () for item in stale))
        self.assertTrue(all(item.funding_z is not None for item in readings))

    def test_reading_series_short_data_is_empty(self):
        self.assertEqual(self.series(self.bars_15m[:50], self.bars_1h[:100], [], []), [])
