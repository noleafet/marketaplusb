"""BTC market regime protection."""

import math
from datetime import datetime, timedelta, timezone as dt_timezone

from unittest.mock import MagicMock, patch

from django.conf import settings
from django.db import IntegrityError, transaction
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.common.binance import BinanceError
from apps.engine.versioning import strategy_snapshot
from apps.trading.models import (
    GridEvent,
    LiveTradingState,
    MarketRegimeState,
    OpenInterestPoint,
    PaperSession,
    StrategySettings,
)

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

    def test_momentum_decay_guard_exit_is_not_bearish(self):
        armed = {
            "phase": "armed",
            "armed_at": (T0 - timedelta(hours=1)).isoformat(),
            "peak_high": 100.0,
            "peak_at": (T0 - timedelta(hours=1)).isoformat(),
        }
        # 1% off the peak, MACD histogram shrinking, close below VWAP.
        decision = self.evaluate(
            phase="warning",
            entered_at=T0 - timedelta(hours=1),
            guard_state=armed,
            price=99.0,
            macd_hist=(0.4, 0.3, 0.2, 0.1),
            vwap=99.5,
        )
        self.assertEqual(decision.guard_state["phase"], "paused")
        self.assertEqual(decision.guard_state["pause_reason"], "momentum_decay")
        self.assertNotIn("guard_exit", decision.reasons)
        self.assertEqual(decision.phase, "normal")

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


BEARISH_FIELDS = {"close_1h": 80.0}
WARNING_FIELDS = {"funding_z": 3.0, "momentum": -0.02}


class RegimeControllerTests(TestCase):
    """``refresh_regime`` and its effects, with BTC readings stubbed."""

    def setUp(self):
        from apps.trading.tests import make_coin, make_session

        self.make_session = make_session
        self.eth = make_coin("ETH", "ETHUSDT")
        self.sol = make_coin("SOL", "SOLUSDT")
        self.now = T0
        price = patch("apps.trading.paper._fetch_price", return_value=100.0)
        price.start()
        self.addCleanup(price.stop)

    def holding(self, coin, **overrides):
        values = {"position_qty": 2.0, "avg_entry": 95.0, "mark_price": 100.0}
        values.update(overrides)
        return self.make_session(coin, **values)

    def refresh(self, now=None, client=None, **fields):
        from apps.trading.regime import refresh_regime

        with patch(
            "apps.trading.regime.fetch_live_inputs", return_value=([], [], [], [])
        ), patch("apps.trading.regime.reading_series", return_value=[reading(**fields)]):
            return refresh_regime(now or self.now, client=client or object())

    def set_phase(self, phase, entered_at=None, reasons=None):
        state = MarketRegimeState.load()
        state.phase = phase
        state.entered_at = entered_at
        state.reasons = reasons or []
        state.save()
        return state

    def test_fetch_failure_keeps_phase(self):
        from apps.trading.regime import refresh_regime

        self.set_phase("bearish", T0 - timedelta(hours=1), ["trailing_stop"])
        with patch("apps.trading.regime.fetch_live_inputs", side_effect=BinanceError("down")):
            state = refresh_regime(self.now, client=object())
        self.assertEqual(state.phase, "bearish")
        self.assertIn("down", state.last_error)
        self.assertEqual(MarketRegimeState.load().phase, "bearish")

    def test_short_data_keeps_phase(self):
        from apps.trading.regime import refresh_regime

        self.set_phase("warning", T0 - timedelta(hours=1), ["guard_armed"])
        with patch(
            "apps.trading.regime.fetch_live_inputs", return_value=([], [], [], [])
        ), patch("apps.trading.regime.reading_series", return_value=[]):
            state = refresh_regime(self.now, client=object())
        self.assertEqual(state.phase, "warning")
        self.assertTrue(state.last_error)

    def _tick(self, fields):
        from apps.trading.paper import tick_paper_sessions

        captured = {}

        def fake_tick(
            session, client, now, settings_row=None, portfolio_block="", derisk=False
        ):
            captured[session.coin.symbol] = (portfolio_block, derisk)
            return {"ok": True, "fills": 0}

        with patch("apps.trading.paper._tick_session", side_effect=fake_tick), patch(
            "apps.trading.regime.fetch_live_inputs", return_value=([], [], [], [])
        ), patch("apps.trading.regime.reading_series", return_value=[reading(**fields)]):
            summary = tick_paper_sessions()
        return captured, summary

    def test_warning_blocks_buys_and_derisks_without_selling(self):
        first = self.holding(self.eth)
        second = self.holding(self.sol)
        for mode in ("normal", "hybrid"):
            with self.subTest(mode=mode):
                trading = LiveTradingState.load()
                trading.mode = mode
                trading.save()
                MarketRegimeState.objects.all().delete()
                captured, summary = self._tick(WARNING_FIELDS)
                expected = ("btc_regime_warning", True)
                self.assertEqual(captured, {"ETH": expected, "SOL": expected})
                self.assertEqual(summary["regime"]["phase"], "warning")
                self.assertEqual(summary["buy_block"], "btc_regime_warning")
                for session in (first, second):
                    session.refresh_from_db()
                    self.assertEqual(session.status, PaperSession.Status.ACTIVE)
                    self.assertAlmostEqual(session.position_qty, 2.0)

    def test_bearish_blocks_buys_and_derisks_without_selling_or_pausing(self):
        sessions = [self.holding(self.eth), self.holding(self.sol)]
        captured, summary = self._tick(BEARISH_FIELDS)
        self.assertEqual(
            summary["regime"], {"phase": "bearish", "reasons": ["support_breakdown"]}
        )
        self.assertEqual(
            captured,
            {"ETH": ("btc_regime_bearish", True), "SOL": ("btc_regime_bearish", True)},
        )
        for session in sessions:
            session.refresh_from_db()
            self.assertEqual(session.status, PaperSession.Status.ACTIVE)
            self.assertAlmostEqual(session.position_qty, 2.0)
            self.assertFalse(session.fills.exists())
            self.assertNotIn("pause_source", session.risk_state)
        self.assertFalse(GridEvent.objects.filter(type=GridEvent.Type.PAUSE).exists())
        regime_event = GridEvent.objects.get(type=GridEvent.Type.REGIME)
        self.assertEqual(regime_event.symbol, "BTCUSDT")
        self.assertEqual(regime_event.before, {"phase": "normal"})
        self.assertEqual(regime_event.after["phase"], "bearish")

        # A second tick inside the same bar records no new transition.
        self.refresh(now=T0 + timedelta(minutes=5), **BEARISH_FIELDS)
        self.assertEqual(GridEvent.objects.filter(type=GridEvent.Type.REGIME).count(), 1)

    def test_normal_neither_blocks_nor_derisks(self):
        self.holding(self.eth)
        captured, summary = self._tick({})
        self.assertEqual(captured, {"ETH": ("", False)})
        self.assertEqual(summary["regime"]["phase"], "normal")

    def test_recovery_after_long_outage_in_one_call(self):
        self.set_phase("bearish", T0 - timedelta(hours=10), ["trailing_stop"])
        state = self.refresh(now=T0 + timedelta(hours=10))
        self.assertEqual(state.phase, "normal")

    def test_sessions_paused_by_the_former_rule_are_released(self):
        from apps.trading.tests import make_coin

        # The regime used to flatten and pause; any session still tagged from
        # then is resumed whatever the phase, so none stays paused forever.
        tagged = self.holding(
            self.eth,
            status=PaperSession.Status.PAUSED,
            position_qty=0.0,
            risk_state={"pause_source": "btc_regime"},
        )
        manual = self.holding(self.sol, status=PaperSession.Status.PAUSED)
        guarded = self.holding(
            make_coin("ADA", "ADAUSDT"),
            status=PaperSession.Status.PAUSED,
            risk_state={"phase": "paused", "pause_source": "btc_regime"},
        )
        self.refresh(**BEARISH_FIELDS)
        for session in (tagged, manual, guarded):
            session.refresh_from_db()
        self.assertEqual(tagged.status, PaperSession.Status.ACTIVE)
        self.assertNotIn("pause_source", tagged.risk_state)
        self.assertTrue(
            GridEvent.objects.filter(
                type=GridEvent.Type.RESUME, reason="btc_regime", session=tagged
            ).exists()
        )
        self.assertEqual(manual.status, PaperSession.Status.PAUSED)
        # Paused by its own coin guard too: that guard's recovery decides.
        self.assertEqual(guarded.status, PaperSession.Status.PAUSED)
        self.assertNotIn("pause_source", guarded.risk_state)

    def test_disabling_sets_normal(self):
        self.refresh(**BEARISH_FIELDS)
        row = StrategySettings.load()
        row.btc_regime_enabled = False
        row.save()
        from apps.trading.regime import refresh_regime

        with patch("apps.trading.regime.fetch_live_inputs") as fetch:
            state = refresh_regime(T0 + timedelta(minutes=5), client=object())
        fetch.assert_not_called()
        self.assertEqual(state.phase, "normal")
        self.assertEqual(
            GridEvent.objects.filter(type=GridEvent.Type.REGIME).latest("time").after["phase"],
            "normal",
        )

    def test_risk_guard_skips_regime_paused_sessions(self):
        from apps.trading.paper import run_risk_guard

        self.holding(
            self.eth,
            status=PaperSession.Status.PAUSED,
            risk_state={"phase": "paused", "pause_source": "btc_regime"},
        )
        with patch("apps.trading.paper.risk.fetch_snapshot") as fetch:
            summary = run_risk_guard()
        fetch.assert_not_called()
        self.assertEqual(summary["sessions"], 0)

    def test_manual_resume_clears_tag(self):
        from apps.trading.paper import resume_session

        session = self.holding(
            self.eth,
            status=PaperSession.Status.PAUSED,
            risk_state={"pause_source": "btc_regime", "last_metrics": {"rsi_15m": 50.0}},
        )
        resume_session(session)
        session.refresh_from_db()
        self.assertEqual(session.risk_state, {"last_metrics": {"rsi_15m": 50.0}})

    def test_older_reading_is_never_applied(self):
        self.refresh(**BEARISH_FIELDS)
        # An overlapping tick that fetched the previous bar must not move the
        # state machine backwards.
        state = self.refresh(time=T0 - timedelta(minutes=15))
        self.assertEqual(state.phase, "bearish")
        self.assertEqual(state.metrics["time"], T0.isoformat())
        self.assertEqual(GridEvent.objects.filter(type=GridEvent.Type.REGIME).count(), 1)

    def test_release_skips_sessions_no_longer_paused(self):
        stopped = self.holding(
            self.eth,
            status=PaperSession.Status.STOPPED,
            risk_state={"pause_source": "btc_regime"},
        )
        from apps.trading.regime import release_regime_pauses

        self.assertEqual(release_regime_pauses(T0), [])
        stopped.refresh_from_db()
        self.assertEqual(stopped.status, PaperSession.Status.STOPPED)

    def test_buy_block_reason_regime(self):
        from apps.trading.portfolio import buy_block_reason

        row = StrategySettings.load()
        self.assertEqual(buy_block_reason({}, row), "")
        self.assertEqual(buy_block_reason({}, row, "normal"), "")
        self.assertEqual(buy_block_reason({}, row, "warning"), "btc_regime_warning")
        self.assertEqual(buy_block_reason({}, row, "bearish"), "btc_regime_bearish")
        self.assertEqual(buy_block_reason({"halted": True}, row, "warning"), "daily_loss_halt")
        self.assertEqual(
            buy_block_reason({"exposure": 99.0}, row, "bearish"), "book_exposure_cap"
        )


class RegimeAnalysisTests(TestCase):
    """The daily analysis opens no paper sessions while BTC is bearish."""

    def setUp(self):
        from apps.engine.models import AnalysisRun
        from apps.trading.tests import make_coin

        self.analysis = AnalysisRun.objects.create(
            status=AnalysisRun.Status.RUNNING, scope=AnalysisRun.Scope.UNIVERSE, summary={}
        )
        self.coin = make_coin("ETH", "ETHUSDT")
        self.ranked = [
            {
                "coin": self.coin,
                "eligible": True,
                "suitability": "prime",
                "params": {"grid_levels": 10, "initial_inventory_pct": 0.5},
            }
        ]

    def set_phase(self, phase):
        state = MarketRegimeState.load()
        state.phase = phase
        state.reasons = ["trailing_stop"] if phase == "bearish" else ["guard_armed"]
        state.save()

    def bootstrap(self):
        from apps.engine.analysis import _bootstrap_paper_sessions

        _bootstrap_paper_sessions(self.analysis, self.ranked, StrategySettings.load())

    def test_no_new_sessions_while_bearish(self):
        from apps.engine.models import DecisionLog

        self.set_phase("bearish")
        self.bootstrap()
        self.assertFalse(PaperSession.objects.exists())
        self.assertTrue(self.analysis.summary["paper_blocked_by_regime"])
        self.assertEqual(self.analysis.summary["paper_sessions_created"], 0)
        self.assertTrue(
            DecisionLog.objects.filter(message__contains="BTC market regime is bearish").exists()
        )

    def test_sessions_open_when_regime_disabled_even_if_stored_bearish(self):
        self.set_phase("bearish")
        row = StrategySettings.load()
        row.btc_regime_enabled = False
        row.save()
        self.bootstrap()
        self.assertEqual(PaperSession.objects.count(), 1)
        self.assertNotIn("paper_blocked_by_regime", self.analysis.summary)

    def test_session_created_in_warning_cannot_buy(self):
        from apps.trading.paper import _tick_session

        self.set_phase("warning")
        self.bootstrap()
        session = PaperSession.objects.select_related("coin").get()
        self.assertGreater(session.params["initial_inventory_pct"], 0)

        now = datetime.now(dt_timezone.utc)
        hist = [
            {
                "time": now - timedelta(hours=200 - i),
                "open": 100.0,
                "high": 100.5,
                "low": 99.5,
                "close": 100.0,
            }
            for i in range(200)
        ]
        client = MagicMock()
        client.funding_rates.return_value = []
        with patch("apps.trading.paper._fetch_hist", return_value=hist), patch(
            "apps.trading.paper._fetch_price", return_value=100.0
        ), patch("apps.trading.paper.insert_equity_points", return_value=1), patch(
            "apps.trading.paper._apply_overbought_guard", return_value=None
        ):
            result = _tick_session(
                session, client, now, StrategySettings.load(), "btc_regime_warning"
            )
        self.assertTrue(result["ok"], result)
        session.refresh_from_db()
        self.assertAlmostEqual(session.position_qty, 0.0)
        self.assertFalse(session.fills.filter(side="buy").exists())
        self.assertFalse(session.grid_state.get("allow_buys", True))


def oi_rows(start, hours, base=1000.0):
    return [
        {"time": start + timedelta(hours=i), "open_interest": base + i, "open_interest_value": 0.0}
        for i in range(hours)
    ]


class OpenInterestRecorderTests(TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 30, 12, 0, tzinfo=dt_timezone.utc)
        self.client = MagicMock()

    def record(self):
        from apps.trading.regime import record_open_interest

        with patch("apps.trading.regime.timezone.now", return_value=self.now):
            return record_open_interest(client=self.client)

    def test_first_run_backfills_then_only_new_rows(self):
        start = self.now - timedelta(hours=10)
        self.client.open_interest_hist.return_value = oi_rows(start, 10)
        self.assertEqual(self.record(), 10)
        kwargs = self.client.open_interest_hist.call_args.kwargs
        self.assertEqual(kwargs["limit"], 500)
        self.assertIsNone(kwargs.get("start_ms"))

        # An hour later: two overlapping rows, two new ones, one in the future.
        self.now += timedelta(hours=1)
        self.client.open_interest_hist.return_value = oi_rows(start + timedelta(hours=8), 5)
        self.assertEqual(self.record(), 2)
        kwargs = self.client.open_interest_hist.call_args.kwargs
        self.assertEqual(
            kwargs["start_ms"], int((start + timedelta(hours=10)).timestamp() * 1000)
        )
        self.assertEqual(OpenInterestPoint.objects.filter(symbol="BTCUSDT").count(), 12)

    def test_resume_after_long_gap_stays_inside_binance_window(self):
        OpenInterestPoint.objects.create(
            symbol="BTCUSDT", time=self.now - timedelta(days=60), open_interest=1.0
        )
        self.client.open_interest_hist.return_value = []
        self.record()
        start_ms = self.client.open_interest_hist.call_args.kwargs["start_ms"]
        self.assertGreaterEqual(
            start_ms, int((self.now - timedelta(days=30)).timestamp() * 1000)
        )

    def test_schedule_exists_hourly(self):
        from apps.jobs.models import JobSchedule
        from apps.jobs.schedules import ensure_schedules

        ensure_schedules()
        schedule = JobSchedule.objects.get(slug="btc-open-interest")
        self.assertEqual(schedule.job_type, "btc_open_interest")
        self.assertEqual(schedule.interval_seconds, 3600)

    def test_duplicate_queued_job_refused(self):
        from apps.jobs.runner import DuplicateJobQueued, enqueue_job

        enqueue_job("btc_open_interest")
        with self.assertRaises(DuplicateJobQueued):
            enqueue_job("btc_open_interest")


REGIME_URL = "/api/strategy/regime/"
RECOVERY_CHECKS = [
    "guard_recovery",
    "above_support",
    "trailing_clear",
    "no_oi_flush",
    "min_duration",
]


@override_settings(ENGINE_API_TOKEN=TOKEN, ENGINE_ALLOW_PUBLIC_READS=True)
class RegimeApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_normal_shape(self):
        state = MarketRegimeState.load()
        state.metrics = {"price": 100.0, "drawdown_pct": 0.01}
        state.save()
        body = self.client.get(REGIME_URL).json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["phase"], "normal")
        self.assertEqual(body["reasons"], [])
        self.assertEqual(body["metrics"]["price"], 100.0)
        self.assertIsNone(body["recovery"])
        self.assertEqual(body["last_error"], "")
        self.assertEqual(body["transitions"], [])
        self.assertEqual(
            body["thresholds"],
            {
                "trailing_stop_pct": 0.08,
                "trailing_window_days": 7,
                "oi_flush_pct": 0.05,
                "oi_near_high_pct": 0.03,
                "funding_z": 2.0,
                "min_bearish_hours": 4.0,
                "rsi_overbought": settings.RISK["RSI_OVERBOUGHT"],
                "rsi_recovery": settings.RISK["RSI_RECOVERY"],
                "guard_trailing_pct": settings.RISK["TRAILING_STOP_PCT"],
                "support_buffer_atr": 0.25,
            },
        )

    def test_bearish_lists_recovery_checks_and_transitions(self):
        from apps.trading.regime import _record_transition, evaluate_regime

        decision = evaluate_regime(
            "bearish", T0 - timedelta(hours=1), {}, reading(close_1h=89.4), regime_config()
        )
        state = MarketRegimeState.load()
        state.phase = decision.phase
        state.entered_at = T0 - timedelta(hours=1)
        state.reasons = decision.reasons
        state.metrics = decision.metrics
        state.save()
        _record_transition("normal", "bearish", ["support_breakdown"], decision.metrics, T0)

        body = self.client.get(REGIME_URL).json()
        self.assertEqual(body["phase"], "bearish")
        self.assertEqual([item["check"] for item in body["recovery"]], RECOVERY_CHECKS)
        passed = {item["check"]: item["passed"] for item in body["recovery"]}
        self.assertEqual(
            passed,
            {
                "guard_recovery": True,
                "above_support": False,
                "trailing_clear": True,
                "no_oi_flush": True,
                "min_duration": False,
            },
        )
        self.assertTrue(all(isinstance(value, bool) for value in passed.values()))
        self.assertEqual(
            body["transitions"],
            [
                {
                    "time": T0.isoformat().replace("+00:00", "Z"),
                    "from": "normal",
                    "to": "bearish",
                    "reasons": ["support_breakdown"],
                }
            ],
        )

    def test_transitions_newest_first_and_capped(self):
        from apps.trading.regime import _record_transition

        for i in range(25):
            _record_transition("normal", "warning", ["guard_armed"], {}, T0 + timedelta(hours=i))
        transitions = self.client.get(REGIME_URL).json()["transitions"]
        self.assertEqual(len(transitions), 20)
        self.assertGreater(transitions[0]["time"], transitions[-1]["time"])

    def test_read_only(self):
        response = self.client.post(REGIME_URL, {}, format="json", HTTP_X_ENGINE_TOKEN=TOKEN)
        self.assertEqual(response.status_code, 405)

    def test_etl_events_serve_regime_events(self):
        from apps.etl_api.keys import create_client
        from apps.trading.regime import _record_transition

        _record_transition("normal", "bearish", ["trailing_stop"], {"price": 91.0}, T0)
        _, key = create_client("t")
        etl = APIClient()
        etl.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        response = etl.get("/api/v1/events/", {"type": "regime"})
        self.assertEqual(response.status_code, 200, response.content)
        data = response.json()["data"]
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["type"], "regime")
        self.assertEqual(data[0]["symbol"], "BTCUSDT")
        self.assertEqual(data[0]["after"]["phase"], "bearish")
