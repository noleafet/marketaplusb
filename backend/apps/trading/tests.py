from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import numpy as np
from django.conf import settings
from django.test import TestCase, override_settings

from apps.coins.models import Coin
from apps.engine.grid import GridParams, SimulationConfig
from apps.engine.models import DecisionLog
from apps.trading.execution import (
    LiveGateError,
    PaperExecutionAdapter,
    assert_live_allowed,
    set_kill_switch,
)
from apps.trading.models import (
    LiveOrderAudit,
    LiveTradingState,
    PaperEvaluation,
    PaperSession,
)
from apps.trading.paper import (
    _apply_overbought_guard,
    pause_session,
    resume_session,
    run_risk_guard,
    set_bias,
    stop_session,
    tick_paper_sessions,
)
from apps.trading.risk import RiskDecision, RiskSnapshot, evaluate, macd_histogram, rsi
from apps.trading.strategy import IncrementalGridStrategy

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


RISK_CFG = {**settings.RISK}


def make_snapshot(**overrides) -> RiskSnapshot:
    now = datetime.now(dt_timezone.utc)
    defaults = {
        "price": 100.0,
        "bar_time": now,
        "bar_high": 100.0,
        "close": 100.0,
        "rsi": 60.0,
        "rsi_prev": 58.0,
        "macd_hist": [0.5, 0.4, 0.3],
        "vwap": 99.0,
        "oi_values": [100.0] * 5,
        "oi_available": True,
    }
    defaults.update(overrides)
    return RiskSnapshot(**defaults)


def make_trading_stack(session: PaperSession):
    config = SimulationConfig(capital=session.capital, leverage=session.leverage)
    params = GridParams.from_dict(session.params or {})
    adapter = PaperExecutionAdapter(session, config=config, params=params)
    strategy = IncrementalGridStrategy(params, config, adapter, {})
    return adapter, strategy


class RiskIndicatorTests(TestCase):
    def test_rsi_detects_overbought_and_oversold(self):
        self.assertGreater(rsi(np.linspace(100, 130, 80), 14), 75)
        self.assertLess(rsi(np.linspace(130, 100, 80), 14), 30)
        self.assertIsNone(rsi(np.linspace(100, 110, 10), 14))

    def test_macd_histogram_requires_enough_history(self):
        self.assertEqual(macd_histogram(np.linspace(100, 110, 20), 12, 26, 9).size, 0)
        self.assertGreater(macd_histogram(np.linspace(100, 120, 80), 12, 26, 9).size, 0)


class OverboughtStateMachineTests(TestCase):
    def setUp(self):
        self.now = datetime.now(dt_timezone.utc)

    def test_rsi_above_threshold_arms_and_tracks_peak(self):
        decision = evaluate(
            {},
            make_snapshot(rsi=80.0, price=100.0, bar_high=101.5),
            RISK_CFG,
            self.now,
        )
        self.assertEqual(decision.action, "armed")
        self.assertEqual(decision.reason, "rsi_overbought")
        self.assertEqual(decision.state["phase"], "armed")
        self.assertEqual(decision.state["peak_high"], 101.5)

    def test_armed_with_no_exhaustion_holds(self):
        state = {
            "phase": "armed",
            "armed_at": self.now.isoformat(),
            "peak_high": 100.0,
        }
        decision = evaluate(state, make_snapshot(price=100.0), RISK_CFG, self.now)
        self.assertEqual(decision.action, "hold")
        self.assertEqual(decision.state["phase"], "armed")

    def test_trailing_drawdown_pauses(self):
        state = {
            "phase": "armed",
            "armed_at": self.now.isoformat(),
            "peak_high": 100.0,
        }
        decision = evaluate(
            state, make_snapshot(price=93.0, bar_high=94.0), RISK_CFG, self.now
        )
        self.assertEqual(decision.action, "pause")
        self.assertEqual(decision.reason, "trailing_drawdown")
        self.assertEqual(decision.state["phase"], "paused")
        self.assertEqual(decision.state["pause_reason"], "trailing_drawdown")

    def test_momentum_decay_pauses_when_vwap_lost(self):
        state = {
            "phase": "armed",
            "armed_at": self.now.isoformat(),
            "peak_high": 100.0,
        }
        snapshot = make_snapshot(
            price=99.5,
            bar_high=99.6,
            close=99.0,
            vwap=100.0,
            macd_hist=[1.0, 0.6, 0.3],
        )
        decision = evaluate(state, snapshot, RISK_CFG, self.now)
        self.assertEqual(decision.action, "pause")
        self.assertEqual(decision.reason, "momentum_decay")

    def test_open_interest_flush_near_peak_pauses(self):
        state = {
            "phase": "armed",
            "armed_at": self.now.isoformat(),
            "peak_high": 100.0,
        }
        snapshot = make_snapshot(
            price=99.0,
            bar_high=99.2,
            close=99.0,
            vwap=98.0,
            macd_hist=[0.1, 0.2, 0.3],
            oi_values=[100.0, 100.0, 100.0, 100.0, 100.0, 93.0],
        )
        decision = evaluate(state, snapshot, RISK_CFG, self.now)
        self.assertEqual(decision.action, "pause")
        self.assertEqual(decision.reason, "oi_flush")

    def test_paused_resumes_when_all_recovery_rules_confirm(self):
        state = {
            "phase": "paused",
            "paused_at": (self.now - timedelta(hours=2)).isoformat(),
            "peak_high": 100.0,
        }
        snapshot = make_snapshot(
            price=101.0,
            close=101.0,
            vwap=100.0,
            rsi=40.0,
            rsi_prev=34.0,
            oi_values=[100.0, 100.0, 100.0, 100.0, 101.0],
        )
        decision = evaluate(state, snapshot, RISK_CFG, self.now)
        self.assertEqual(decision.action, "resume")
        self.assertEqual(decision.state["phase"], "idle")

    def test_paused_stays_while_vwap_not_reclaimed(self):
        state = {
            "phase": "paused",
            "paused_at": (self.now - timedelta(hours=2)).isoformat(),
            "peak_high": 100.0,
        }
        snapshot = make_snapshot(
            price=99.0,
            close=99.0,
            vwap=100.0,
            rsi=40.0,
            rsi_prev=34.0,
            oi_values=[100.0, 100.0, 100.0, 100.0, 101.0],
        )
        decision = evaluate(state, snapshot, RISK_CFG, self.now)
        self.assertEqual(decision.action, "hold")

    def test_paused_ignores_recovery_inside_cooldown(self):
        state = {
            "phase": "paused",
            "paused_at": (self.now - timedelta(minutes=5)).isoformat(),
            "peak_high": 100.0,
        }
        snapshot = make_snapshot(
            price=101.0,
            close=101.0,
            vwap=100.0,
            rsi=40.0,
            rsi_prev=34.0,
            oi_values=[100.0, 100.0, 100.0, 100.0, 101.0],
        )
        decision = evaluate(state, snapshot, RISK_CFG, self.now)
        self.assertEqual(decision.action, "hold")


class OverboughtGuardExecutionTests(TestCase):
    @patch("apps.trading.paper.send_webhook", return_value=True)
    @patch("apps.trading.paper.risk.fetch_snapshot")
    def test_pause_flattens_inventory_and_freezes_orders(self, fetch, _webhook):
        now = datetime.now(dt_timezone.utc)
        session = make_session(
            make_coin(),
            position_qty=2.0,
            avg_entry=100.0,
            equity=1000.0,
            risk_state={
                "phase": "armed",
                "armed_at": now.isoformat(),
                "peak_high": 100.0,
            },
        )
        adapter, strategy = make_trading_stack(session)
        strategy.orders = {1: {"side": "sell", "qty": 1.0}}
        fetch.return_value = make_snapshot(price=93.0, bar_high=94.0)

        result = _apply_overbought_guard(
            session, strategy, adapter, client=None, now=now, price=93.0
        )

        self.assertEqual(result["action"], "pause", result)
        self.assertEqual(session.status, PaperSession.Status.PAUSED)
        self.assertEqual(adapter.position_qty, 0.0)
        self.assertEqual(strategy.orders, {})
        self.assertTrue(session.risk_state["webhook_ok"])
        self.assertEqual(len(adapter.fills), 1)
        self.assertEqual(adapter.fills[0].reason, "overbought_trailing_drawdown")

    @patch("apps.trading.paper.risk.fetch_snapshot")
    def test_risk_job_resumes_paused_session_and_clears_stale_grid(self, fetch):
        now = datetime.now(dt_timezone.utc)
        session = make_session(
            make_coin(),
            status=PaperSession.Status.PAUSED,
            grid_state={"levels": [1.0, 2.0, 3.0], "orders": {"1": {"side": "sell", "qty": 1}}},
            risk_state={
                "phase": "paused",
                "paused_at": (now - timedelta(hours=2)).isoformat(),
                "peak_high": 100.0,
            },
        )
        fetch.return_value = make_snapshot(
            price=101.0,
            close=101.0,
            vwap=100.0,
            rsi=40.0,
            rsi_prev=34.0,
            oi_values=[100.0, 100.0, 100.0, 100.0, 101.0],
        )

        summary = run_risk_guard()

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["resumed"], 1)
        session.refresh_from_db()
        self.assertEqual(session.status, PaperSession.Status.ACTIVE)
        self.assertEqual(session.grid_state, {})
        self.assertEqual(session.risk_state["phase"], "idle")


class BiasLockTests(TestCase):
    def test_set_bias_clears_grid_state_and_logs(self):
        session = make_session(
            make_coin(),
            grid_state={"levels": [1.0, 2.0, 3.0], "orders": {"1": {"side": "buy", "qty": 1}}},
        )

        set_bias(session, "bearish")

        session.refresh_from_db()
        self.assertEqual(session.bias, PaperSession.Bias.BEARISH)
        self.assertEqual(session.grid_state, {})
        self.assertTrue(
            DecisionLog.objects.filter(category="paper", coin=session.coin).exists()
        )

    def test_set_bias_rejects_unknown_values(self):
        session = make_session(make_coin())

        with self.assertRaises(ValueError):
            set_bias(session, "neutral")

        session.refresh_from_db()
        self.assertEqual(session.bias, PaperSession.Bias.BULLISH)

    @override_settings(PAPER={**settings.PAPER, "EVAL_INTERVAL_HOURS": 0})
    @patch("apps.trading.paper.insert_equity_points", return_value=1)
    @patch("apps.trading.paper._fetch_hist")
    @patch("apps.trading.paper._fetch_price", return_value=100.0)
    @patch("apps.trading.paper.BinanceFuturesClient")
    def test_bearish_session_keeps_orders_clear_until_bullish(
        self, client_cls, fetch_price, fetch_hist, _pts
    ):
        now = datetime.now(dt_timezone.utc)
        hist = []
        for index in range(180):
            price = 100.0 + 3.0 * ((index % 12) - 6) / 6.0
            hist.append(
                {
                    "time": now - timedelta(hours=4 * (180 - index)),
                    "open": price,
                    "high": price * 1.004,
                    "low": price * 0.996,
                    "close": price,
                }
            )
        fetch_hist.return_value = hist
        session = make_session(make_coin(), bias=PaperSession.Bias.BEARISH)

        tick_paper_sessions(session_ids=[str(session.id)])

        session.refresh_from_db()
        self.assertEqual(session.grid_state["orders"], {})
        self.assertEqual(session.fills.count(), 0)
        self.assertAlmostEqual(session.mark_price, 100.0, places=6)

        set_bias(session, "bullish")
        tick_paper_sessions(session_ids=[str(session.id)])

        session.refresh_from_db()
        self.assertEqual(session.bias, PaperSession.Bias.BULLISH)
        self.assertTrue(session.grid_state["orders"])
