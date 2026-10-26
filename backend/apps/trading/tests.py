from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings

from apps.coins.models import Coin
from apps.engine.grid import GridParams
from apps.trading.execution import LiveGateError, assert_live_allowed, set_kill_switch
from apps.trading.models import (
    LiveOrderAudit,
    LiveTradingState,
    PaperEvaluation,
    PaperSession,
)
from apps.trading.paper import (
    pause_session,
    resume_session,
    stop_session,
    tick_paper_sessions,
)

LIVE_BASE = {
    "ENABLED": False,
    "TESTNET": True,
    "API_KEY": "",
    "API_SECRET": "",
    "MAX_NOTIONAL_USD": 250.0,
    "MAX_DAILY_LOSS_USD": 50.0,
    "MAX_LEVERAGE": 2.0,
    "SYMBOL_ALLOWLIST": [],
}


def make_coin(symbol: str = "BTC", binance_symbol: str = "BTCUSDT") -> Coin:
    return Coin.objects.create(
        coingecko_id=symbol.lower(),
        symbol=symbol,
        name=symbol,
        binance_symbol=binance_symbol,
        analysis_eligible=bool(binance_symbol),
    )


def make_session(coin: Coin, **overrides) -> PaperSession:
    params = GridParams(grid_levels=10, range_lookback_days=14).to_dict()
    defaults = {
        "coin": coin,
        "params": params,
        "config": {
            "capital": 1000.0,
            "leverage": 2.0,
            "maker_fee_rate": 0.0002,
            "taker_fee_rate": 0.0005,
            "slippage_bps": 1.0,
            "interval": "4h",
        },
        "capital": 1000.0,
        "leverage": 2.0,
        "equity": 1000.0,
        "peak_equity": 1000.0,
    }
    defaults.update(overrides)
    return PaperSession.objects.create(**defaults)


class LiveSafetyTests(TestCase):
    @override_settings(LIVE=LIVE_BASE)
    def test_live_trading_denied_by_default_and_audited(self):
        with self.assertRaises(LiveGateError) as raised:
            assert_live_allowed("BTCUSDT", 100.0, 1.0)

        self.assertIn("LIVE_TRADING_ENABLED", str(raised.exception))
        audit = LiveOrderAudit.objects.get()
        self.assertFalse(audit.allowed)
        self.assertIn("LIVE_TRADING_ENABLED", audit.reason)
        self.assertFalse(settings.LIVE["ENABLED"])

    @override_settings(
        LIVE={**LIVE_BASE, "ENABLED": True, "API_KEY": "key", "API_SECRET": "secret"}
    )
    def test_kill_switch_blocks_even_when_environment_is_enabled(self):
        state = LiveTradingState.load()
        self.assertTrue(state.kill_switch)

        with self.assertRaises(LiveGateError) as raised:
            assert_live_allowed("BTCUSDT", 100.0, 1.0)
        self.assertIn("Kill switch", str(raised.exception))

        set_kill_switch(False)
        with self.assertRaises(LiveGateError) as raised:
            assert_live_allowed("BTCUSDT", 100.0, 1.0)
        self.assertIn("approval", str(raised.exception))
        self.assertEqual(LiveOrderAudit.objects.filter(allowed=False).count(), 2)


class PaperSessionLifecycleTests(TestCase):
    def test_pause_resume_stop_transitions(self):
        session = make_session(make_coin())

        pause_session(session)
        session.refresh_from_db()
        self.assertEqual(session.status, PaperSession.Status.PAUSED)

        resume_session(session)
        session.refresh_from_db()
        self.assertEqual(session.status, PaperSession.Status.ACTIVE)

        stop_session(session, "test_stop")
        session.refresh_from_db()
        self.assertEqual(session.status, PaperSession.Status.STOPPED)
        self.assertEqual(session.stop_reason, "test_stop")
        self.assertFalse(session.promotion_ready)


class PaperTickTests(TestCase):
    @override_settings(
        PAPER={
            **settings.PAPER,
            "EVAL_INTERVAL_HOURS": 0,
        }
    )
    @patch("apps.trading.paper.insert_equity_points", return_value=1)
    @patch("apps.trading.paper._fetch_hist")
    @patch("apps.trading.paper._fetch_price", return_value=100.0)
    @patch("apps.trading.paper.BinanceFuturesClient")
    def test_first_tick_builds_and_persists_grid(self, client_cls, fetch_price, fetch_hist, _pts):
        now = datetime.now(dt_timezone.utc)
        hist = []
        for index in range(30 * 6):
            price = 100.0 + 3.0 * ((index % 12) - 6) / 6.0
            hist.append(
                {
                    "time": now - timedelta(hours=4 * (30 * 6 - index)),
                    "open": price,
                    "high": price * 1.004,
                    "low": price * 0.996,
                    "close": price,
                }
            )
        fetch_hist.return_value = hist
        session = make_session(make_coin())

        summary = tick_paper_sessions(session_ids=[str(session.id)])

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["sessions"], 1)
        self.assertEqual(summary["fills"], 0)
        session.refresh_from_db()
        self.assertEqual(session.last_tick_at is not None, True)
        self.assertGreaterEqual(len(session.grid_state["levels"]), 3)
        self.assertEqual(session.grid_state["last_bar_time"] is not None, True)
        self.assertTrue(session.grid_state["orders"])
        self.assertAlmostEqual(session.equity, 1000.0, places=2)
        self.assertAlmostEqual(session.mark_price, 100.0, places=6)
        self.assertTrue(PaperEvaluation.objects.filter(session=session).exists())
        fetch_price.assert_called_once()
