from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import numpy as np
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.trading.execution import LiveGateError, assert_live_allowed
from apps.trading.hybrid import TrendSignal, dmi, ema_series
from apps.trading.hybrid import evaluate as evaluate_trend
from apps.trading.models import (
    LiveApproval,
    LiveOrderAudit,
    LiveTradingState,
    PaperFill,
)
from apps.trading.paper import tick_paper_sessions
from apps.trading.tests import (
    LIVE_BASE,
    make_adaptive_stack,
    make_coin,
    make_session,
)

TOKEN = "test-engine-token"


def make_klines(closes, *, step_hours: float = 1.0):
    start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)
    return [
        {
            "time": start + timedelta(hours=step_hours * index),
            "open": close,
            "high": close * 1.002,
            "low": close * 0.998,
            "close": close,
        }
        for index, close in enumerate(closes)
    ]


def halted_signal(**overrides) -> TrendSignal:
    values = {
        "ready": True,
        "ema_100": 90.0,
        "price_1h": 89.0,
        "adx": None,
        "plus_di": None,
        "minus_di": None,
        "above_streak": 0,
        "below_streak": 2,
        "halted": True,
        "allow_buys": False,
        "reason": "exit_confirmed",
    }
    values.update(overrides)
    return TrendSignal(**values)


class HybridTrendFilterTests(TestCase):
    """Closed-candle EMA(100)/ADX gates for hybrid mode."""

    def setUp(self):
        self.closes = list(np.linspace(100.0, 200.0, 240))
        self.klines_1h = make_klines(self.closes)
        self.ema = float(ema_series(np.array(self.closes), 100)[-1])

    def test_uptrend_allows_buys_when_15m_closes_are_above_ema(self):
        klines_15m = make_klines([self.ema + 5.0] * 8, step_hours=0.25)

        signal = evaluate_trend(self.klines_1h, klines_15m)

        self.assertTrue(signal.ready)
        self.assertGreater(signal.price_1h, signal.ema_100)
        self.assertFalse(signal.halted)
        self.assertTrue(signal.allow_buys)
        self.assertEqual(signal.reason, "ok")

    def test_two_closed_15m_candles_below_ema_halt_the_grid(self):
        klines_15m = make_klines(
            [self.ema + 5.0, self.ema - 5.0, self.ema - 6.0], step_hours=0.25
        )

        signal = evaluate_trend(self.klines_1h, klines_15m)

        self.assertTrue(signal.halted)
        self.assertFalse(signal.allow_buys)
        self.assertEqual(signal.below_streak, 2)
        self.assertEqual(signal.reason, "exit_confirmed")

    def test_halt_clears_only_after_two_confirmed_candles_back_above(self):
        self.assertTrue(
            evaluate_trend(
                self.klines_1h,
                make_klines([self.ema - 1.0, self.ema - 2.0], step_hours=0.25),
            ).halted
        )

        single = evaluate_trend(
            self.klines_1h,
            make_klines([self.ema - 1.0, self.ema + 2.0], step_hours=0.25),
            halted=True,
        )
        self.assertTrue(single.halted)

        resumed = evaluate_trend(
            self.klines_1h,
            make_klines([self.ema - 1.0, self.ema + 2.0, self.ema + 3.0], step_hours=0.25),
            halted=True,
        )
        self.assertFalse(resumed.halted)
        self.assertTrue(resumed.allow_buys)

    def test_adx_filter_blocks_weak_trends(self):
        closes = [100.0 if index % 2 == 0 else 102.0 for index in range(240)]
        klines_1h = make_klines(closes)
        ema = float(ema_series(np.array(closes), 100)[-1])
        klines_15m = make_klines([ema + 3.0] * 4, step_hours=0.25)

        gated = evaluate_trend(klines_1h, klines_15m, adx_enabled=True)
        self.assertIsNotNone(gated.adx)
        self.assertLess(gated.adx, 25)
        self.assertFalse(gated.allow_buys)
        self.assertEqual(gated.reason, "adx_weak")

        ungated = evaluate_trend(klines_1h, klines_15m, adx_enabled=False)
        self.assertTrue(ungated.allow_buys)

    def test_dmi_reports_directional_pressure(self):
        closes = np.linspace(100.0, 200.0, 240)
        result = dmi(closes * 1.002, closes * 0.998, closes, 14)

        self.assertIsNotNone(result)
        adx_value, plus_di, minus_di = result
        self.assertGreater(adx_value, 25)
        self.assertGreater(plus_di, minus_di)

    def test_insufficient_history_blocks_buys_without_halting(self):
        signal = evaluate_trend(self.klines_1h[:40], make_klines([100.0] * 4, step_hours=0.25))

        self.assertFalse(signal.ready)
        self.assertFalse(signal.allow_buys)
        self.assertFalse(signal.halted)
        self.assertEqual(signal.reason, "insufficient_history")


class HybridStrategyGateTests(TestCase):
    def test_trend_block_disarms_buy_orders(self):
        _, strategy = make_adaptive_stack()
        strategy._rebuild_orders(100.0)
        self.assertTrue(strategy.orders)
        now = datetime(2025, 6, 1, tzinfo=dt_timezone.utc)

        strategy.trend_blocked = True
        strategy._refresh_gates(100.0, now)
        strategy._rebuild_orders(100.0)

        self.assertFalse(any(order["side"] == "buy" for order in strategy.orders.values()))
        buy_rows = [item for item in strategy.level_plan if item["side"] == "buy"]
        self.assertTrue(buy_rows)
        self.assertTrue(all(item["reason"] == "risk_gate" for item in buy_rows))

    def test_halted_signal_flattens_inventory_and_persists(self):
        now = datetime(2025, 6, 1, tzinfo=dt_timezone.utc)
        adapter, strategy = make_adaptive_stack(
            {"allow_buys": False}, position_qty=5.0, avg_entry=98.0
        )

        event = strategy.apply_trend_signal(halted_signal(), 100.0, now)

        self.assertEqual(event, "halted")
        self.assertEqual(adapter.position_qty, 0.0)
        self.assertEqual(adapter.fills[-1].reason, "hybrid_trend_exit")
        self.assertTrue(strategy.hybrid_halted)
        self.assertTrue(strategy.trend_blocked)
        self.assertFalse(any(order["side"] == "buy" for order in strategy.orders.values()))
        state = strategy.to_state()
        self.assertTrue(state["hybrid_halted"])
        self.assertTrue(state["trend_blocked"])
        self.assertEqual(state["trend_reason"], "exit_confirmed")

    def test_clear_trend_filter_rearms_buys(self):
        _, strategy = make_adaptive_stack({"trend_blocked": True, "allow_buys": False})
        now = datetime(2025, 6, 1, tzinfo=dt_timezone.utc)

        strategy.clear_trend_filter()
        strategy._refresh_gates(100.0, now)

        self.assertFalse(strategy.trend_blocked)
        self.assertTrue(strategy.allow_buys)


@override_settings(ENGINE_API_TOKEN=TOKEN, ENGINE_ALLOW_PUBLIC_READS=True)
class TradingModeApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.headers = {"HTTP_X_ENGINE_TOKEN": TOKEN}

    def test_switch_to_hybrid_requires_confirmation(self):
        response = self.client.post(
            "/api/live/status/mode/", {"mode": "hybrid"}, format="json", **self.headers
        )

        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.data["requires_confirmation"])
        self.assertEqual(LiveTradingState.load().mode, LiveTradingState.Mode.NORMAL)

    def test_switch_to_hybrid_purges_open_orders_and_positions(self):
        session = make_session(
            make_coin(),
            position_qty=2.0,
            avg_entry=90.0,
            realized_pnl=25.0,
            fees_paid=1.0,
            grid_state={"orders": {"0": {"side": "buy", "qty": 1.0}}},
        )
        PaperFill.objects.create(
            session=session,
            time=datetime.now(dt_timezone.utc),
            side="buy",
            price=90.0,
            qty=2.0,
            notional=180.0,
            fee=0.1,
            reason="grid",
        )

        response = self.client.post(
            "/api/live/status/mode/",
            {"mode": "hybrid", "confirm": True},
            format="json",
            **self.headers,
        )

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["mode"], "hybrid")
        self.assertEqual(response.data["purge"], {"sessions": 1, "positions": 1})
        session.refresh_from_db()
        self.assertEqual(session.position_qty, 0.0)
        self.assertEqual(session.avg_entry, 0.0)
        self.assertEqual(session.grid_state, {})
        self.assertEqual(session.realized_pnl, 25.0)
        self.assertAlmostEqual(session.equity, 1024.0, places=6)
        self.assertEqual(PaperFill.objects.filter(session=session).count(), 1)

    def test_adx_toggle_persists_in_existing_limits(self):
        self.client.post(
            "/api/live/status/mode/",
            {"mode": "hybrid", "adx_enabled": True, "confirm": True},
            format="json",
            **self.headers,
        )

        response = self.client.get("/api/live/status/mode/")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["mode"], "hybrid")
        self.assertTrue(response.data["adx_enabled"])
        state = LiveTradingState.load()
        self.assertTrue(state.limits["hybrid_adx"])

    def test_switch_back_to_normal_keeps_paper_state(self):
        session = make_session(make_coin(), position_qty=1.0, avg_entry=95.0)
        self.client.post(
            "/api/live/status/mode/",
            {"mode": "hybrid", "confirm": True},
            format="json",
            **self.headers,
        )
        session.refresh_from_db()
        session.position_qty = 1.0
        session.avg_entry = 95.0
        session.grid_state = {"orders": {"0": {"side": "buy", "qty": 1.0}}}
        session.save()

        response = self.client.post(
            "/api/live/status/mode/", {"mode": "normal"}, format="json", **self.headers
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["purge"], {"sessions": 0, "positions": 0})
        session.refresh_from_db()
        self.assertEqual(session.position_qty, 1.0)
        self.assertTrue(session.grid_state)


class HybridLiveGateTests(TestCase):
    @override_settings(
        LIVE={**LIVE_BASE, "ENABLED": True, "API_KEY": "key", "API_SECRET": "secret"}
    )
    def test_hybrid_mode_blocks_new_live_orders(self):
        approval = LiveApproval.objects.create(
            status=LiveApproval.Status.APPROVED, risk_acknowledged=True
        )
        state = LiveTradingState.load()
        state.kill_switch = False
        state.active_approval = approval
        state.mode = LiveTradingState.Mode.HYBRID
        state.save()

        with self.assertRaises(LiveGateError) as raised:
            assert_live_allowed("BTCUSDT", 100.0, 1.0)

        self.assertIn("Hybrid mode", str(raised.exception))
        self.assertFalse(LiveOrderAudit.objects.get().allowed)


class HybridPaperTickTests(TestCase):
    @override_settings(PAPER={**settings.PAPER, "EVAL_INTERVAL_HOURS": 0})
    @patch("apps.trading.paper.insert_equity_points", return_value=1)
    @patch("apps.trading.paper._hybrid_confidence")
    @patch("apps.trading.paper._fetch_hist")
    @patch("apps.trading.paper._fetch_price", return_value=100.0)
    @patch("apps.trading.paper.BinanceFuturesClient")
    def test_hybrid_blocks_buys_on_confirmed_exit(
        self, client_cls, fetch_price, fetch_hist, confidence, _pts
    ):
        state = LiveTradingState.load()
        state.mode = LiveTradingState.Mode.HYBRID
        state.save()
        confidence.return_value = halted_signal()
        now = datetime.now(dt_timezone.utc)
        hist = [
            {
                "time": now - timedelta(hours=4 * (180 - index)),
                "open": 100.0,
                "high": 100.4,
                "low": 99.6,
                "close": 100.0,
            }
            for index in range(180)
        ]
        fetch_hist.return_value = hist
        session = make_session(make_coin())

        summary = tick_paper_sessions(session_ids=[str(session.id)])

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["results"][0]["mode"], "hybrid")
        self.assertTrue(summary["results"][0]["trend"]["halted"])
        session.refresh_from_db()
        self.assertTrue(session.grid_state["hybrid_halted"])
        self.assertTrue(session.grid_state["trend_blocked"])
        self.assertFalse(
            any(order["side"] == "buy" for order in session.grid_state["orders"].values())
        )
        self.assertFalse(PaperFill.objects.filter(session=session, side="buy").exists())
