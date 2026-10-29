from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

import numpy as np
from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.coins.models import Coin
from apps.engine.grid import GridParams, SimulationConfig
from apps.engine.models import DecisionLog
from apps.trading.execution import (
    Fill,
    LiveGateError,
    PaperExecutionAdapter,
    assert_live_allowed,
    set_kill_switch,
)
from apps.trading.hybrid import TrendSignal, dmi, ema_series
from apps.trading.hybrid import evaluate as evaluate_trend
from apps.trading.models import (
    LiveApproval,
    LiveOrderAudit,
    LiveTradingState,
    PaperEvaluation,
    PaperFill,
    PaperSession,
    StrategySettings,
)
from apps.trading.paper import (
    _apply_overbought_guard,
    paper_config_snapshot,
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
        self.assertTrue(session.grid_state["level_plan"])
        self.assertGreater(session.grid_state["atr"], 0.0)
        self.assertGreater(session.grid_state["epoch_stop"], 0.0)
        self.assertAlmostEqual(session.equity, 1000.0, places=2)
        self.assertAlmostEqual(session.mark_price, 100.0, places=6)
        self.assertTrue(PaperEvaluation.objects.filter(session=session).exists())
        self.assertTrue(
            DecisionLog.objects.filter(
                category="paper",
                coin=session.coin,
                message__contains="grid initialized",
            ).exists()
        )
        fetch_price.assert_called_once()


class PaperSignalIntervalTests(TestCase):
    @override_settings(PAPER={**settings.PAPER, "SIGNAL_INTERVAL": "1h", "TICK_MINUTES": 5})
    def test_new_sessions_use_completed_1h_signals_with_5m_monitoring(self):
        snapshot = paper_config_snapshot()

        self.assertEqual(snapshot["interval"], "1h")
        self.assertEqual(settings.PAPER["TICK_MINUTES"], 5)


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


class FakeAdapter:
    """Minimal in-memory execution adapter for strategy unit tests."""

    def __init__(
        self,
        *,
        capital: float = 1000.0,
        position_qty: float = 0.0,
        avg_entry: float = 0.0,
    ) -> None:
        self.capital = capital
        self.position_qty = position_qty
        self.avg_entry = avg_entry
        self.realized_pnl = 0.0
        self.fees_paid = 0.0
        self.funding_paid = 0.0
        self.mark_price = 0.0
        self.fills: list[Fill] = []

    def equity(self, price: float) -> float:
        unreal = (
            (price - self.avg_entry) * self.position_qty if self.position_qty > 0 else 0.0
        )
        return (
            self.capital
            + self.realized_pnl
            - self.fees_paid
            - self.funding_paid
            + unreal
        )

    def max_buy_qty(self, price: float) -> float:
        if price <= 0:
            return 0.0
        eq = self.equity(price)
        notional = self.position_qty * price
        budget = min(
            eq * 2.0 * 0.9 - notional,
            max(0.0, eq - notional / 2.0) * 2.0 * 0.98,
        )
        return max(0.0, budget / price)

    def buy(self, requested_price, qty, time, reason, level):
        qty = min(float(qty), self.max_buy_qty(requested_price))
        if qty <= 1e-12:
            return None
        new_qty = self.position_qty + qty
        self.avg_entry = (
            self.avg_entry * self.position_qty + requested_price * qty
        ) / new_qty
        self.position_qty = new_qty
        fill = Fill(
            time, "buy", requested_price, qty, qty * requested_price, 0.0, 0.0, level, reason
        )
        self.fills.append(fill)
        return fill

    def sell(self, requested_price, qty, time, reason, level):
        qty = min(float(qty), self.position_qty)
        if qty <= 1e-12:
            return None
        pnl = (requested_price - self.avg_entry) * qty
        self.realized_pnl += pnl
        self.position_qty -= qty
        if self.position_qty <= 1e-12:
            self.position_qty = 0.0
            self.avg_entry = 0.0
        fill = Fill(
            time,
            "sell",
            requested_price,
            qty,
            qty * requested_price,
            0.0,
            pnl,
            level,
            reason,
        )
        self.fills.append(fill)
        return fill

    def close_position(self, price, time, reason):
        return self.sell(price, self.position_qty, time, reason, None)

    def apply_funding(self, rate, mark_price, time):
        return 0.0

    def mark(self, price):
        self.mark_price = price


def make_adaptive_stack(state: dict | None = None, **adapter_kwargs):
    params = GridParams(grid_levels=6, range_lookback_days=14, rebalance_days=7)
    config = SimulationConfig()
    adapter = FakeAdapter(**adapter_kwargs)
    default_state = {
        "levels": [95.0, 97.0, 99.0, 101.0, 103.0, 105.0],
        "range_low": 95.0,
        "range_high": 105.0,
        "prev_close": 100.0,
    }
    strategy = IncrementalGridStrategy(
        params, config, adapter, {**default_state, **(state or {})}
    )
    return adapter, strategy


def make_bar(open_, high, low, close, when=None):
    return {
        "time": when or datetime(2025, 6, 1, tzinfo=dt_timezone.utc),
        "open": open_,
        "high": high,
        "low": low,
        "close": close,
    }


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
    @patch("apps.trading.paper.risk.fetch_snapshot")
    def test_pause_flattens_inventory_and_freezes_orders(self, fetch):
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
        # Explicit, so the test does not depend on the settings default.
        guard = StrategySettings.load()
        guard.overbought_guard_enabled = True
        guard.save()

        summary = run_risk_guard()

        self.assertTrue(summary["ok"], summary)
        self.assertEqual(summary["resumed"], 1)
        session.refresh_from_db()
        self.assertEqual(session.status, PaperSession.Status.ACTIVE)
        self.assertEqual(session.grid_state, {})
        self.assertEqual(session.risk_state["phase"], "idle")


class AdaptiveStrategyTests(TestCase):
    """The paper strategy mirrors the five adaptive backtest rules."""

    def test_epoch_stop_closes_inventory(self):
        adapter, strategy = make_adaptive_stack(
            {"epoch_stop": 95.0}, position_qty=10.0, avg_entry=100.0
        )

        result = strategy.process_bar(make_bar(100.0, 100.0, 94.5, 94.6))

        self.assertTrue(result["stopped"])
        self.assertEqual(result["reason"], "stop_loss")
        self.assertEqual(adapter.position_qty, 0.0)
        self.assertEqual(adapter.fills[-1].reason, "stop_loss")
        self.assertTrue(strategy.stopped)

    def test_trailing_take_profit_exits_after_peak_retrace(self):
        now = datetime(2025, 6, 1, tzinfo=dt_timezone.utc)
        adapter, strategy = make_adaptive_stack(
            {"tp_active": True, "peak_price": 110.0, "atr": 1.0},
            position_qty=10.0,
            avg_entry=100.0,
        )

        result = strategy.process_bar(make_bar(108.0, 108.0, 106.0, 106.5, now))

        self.assertFalse(result["stopped"])
        self.assertEqual(adapter.position_qty, 0.0)
        self.assertEqual(adapter.fills[-1].reason, "trailing_take_profit")
        self.assertEqual(strategy.trailing_tp_exits, 1)
        self.assertFalse(strategy.tp_active)
        self.assertGreaterEqual(strategy.buy_blocked_until, now)

    def test_crowded_funding_trims_exposure_and_blocks_buys(self):
        adapter, strategy = make_adaptive_stack(
            {
                "atr": 0.5,
                "support": 80.0,
                "trend_up": False,
                "momentum": -0.01,
                "ctx_range_high": 105.0,
                "ctx_range_low": 95.0,
            },
            position_qty=20.0,
            avg_entry=100.0,
        )
        strategy.funding_z = 3.0

        strategy.process_bar(make_bar(100.0, 100.5, 99.5, 100.0))

        self.assertEqual(strategy.funding_trims, 1)
        self.assertLess(adapter.position_qty, 20.0)
        self.assertTrue(any(fill.reason == "funding_trim" for fill in adapter.fills))
        self.assertFalse(strategy.allow_buys)
        self.assertAlmostEqual(strategy.buy_scale, 0.5)

    def test_trailing_up_shifts_levels_and_raises_stop(self):
        _, strategy = make_adaptive_stack(
            {
                "atr": 1.0,
                "trend_up": True,
                "momentum": 0.01,
                "ctx_range_high": 100.0,
                "ctx_range_low": 95.0,
                "prev_close": 105.0,
            }
        )

        strategy.process_bar(make_bar(105.0, 106.0, 104.0, 106.0))

        self.assertEqual(strategy.trail_up_shifts, 1)
        self.assertAlmostEqual(float(strategy.levels[0]), 96.0, places=6)
        self.assertAlmostEqual(float(strategy.levels[-1]), 106.0, places=6)
        self.assertAlmostEqual(strategy.epoch_stop, 94.0, places=6)

    def test_range_position_and_funding_gate_buys(self):
        _, strategy = make_adaptive_stack(
            {
                "atr": 1.0,
                "support": 80.0,
                "ctx_range_low": 90.0,
                "ctx_range_high": 110.0,
                "trend_up": False,
                "momentum": -0.01,
            }
        )
        now = datetime(2025, 6, 1, tzinfo=dt_timezone.utc)

        strategy._refresh_gates(109.0, now)
        self.assertFalse(strategy.allow_buys)
        self.assertGreaterEqual(strategy.ctx_range_pos, 0.9)

        strategy._refresh_gates(95.0, now)
        self.assertTrue(strategy.allow_buys)

        strategy.funding_z = 2.5
        strategy._refresh_gates(95.0, now)
        self.assertFalse(strategy.allow_buys)
        self.assertAlmostEqual(strategy.buy_scale, 0.5)

    def test_blocked_gate_clears_buy_orders(self):
        _, strategy = make_adaptive_stack(
            {
                "prev_close": 109.0,
                "ctx_range_low": 90.0,
                "ctx_range_high": 110.0,
                "trend_up": False,
                "momentum": -0.01,
            }
        )
        # Arm the ladder first so the audit plan holds active buy rungs.
        strategy.allow_buys = True
        strategy._rebuild_orders(100.0)
        self.assertTrue(any(o["side"] == "buy" for o in strategy.orders.values()))

        strategy.process_bar(make_bar(109.0, 109.0, 109.0, 109.0))

        self.assertEqual(strategy.buy_blocks, 1)
        self.assertFalse(any(o["side"] == "buy" for o in strategy.orders.values()))
        buy_rows = [item for item in strategy.level_plan if item["side"] == "buy"]
        self.assertTrue(buy_rows)
        self.assertTrue(all(item["reason"] == "risk_gate" for item in buy_rows))
        self.assertTrue(all(item["qty"] == 0.0 for item in buy_rows))

    def test_rebuild_orders_covers_every_level_below_price(self):
        _, strategy = make_adaptive_stack()

        strategy._rebuild_orders(100.0)

        self.assertEqual(sorted(strategy.orders), [0, 1, 2])
        self.assertTrue(all(o["side"] == "buy" for o in strategy.orders.values()))
        notional = sum(
            order["qty"] * strategy.levels[index]
            for index, order in strategy.orders.items()
        )
        # Default max_inventory_pct=1.0 caps exposure at equity, not 2x equity.
        self.assertAlmostEqual(notional, 1000.0, places=6)
        self.assertTrue(all(item["reason"] == "active" for item in strategy.level_plan[:3]))
        self.assertTrue(
            all(item["reason"] == "no_inventory" for item in strategy.level_plan[3:])
        )

    def test_rebuild_orders_respects_relaxed_inventory_cap(self):
        params = GridParams(
            grid_levels=6, range_lookback_days=14, rebalance_days=7, max_inventory_pct=2.0
        )
        adapter = FakeAdapter()
        strategy = IncrementalGridStrategy(
            params,
            SimulationConfig(),
            adapter,
            {
                "levels": [95.0, 97.0, 99.0, 101.0, 103.0, 105.0],
                "range_low": 95.0,
                "range_high": 105.0,
                "prev_close": 100.0,
            },
        )

        strategy._rebuild_orders(100.0)

        notional = sum(
            order["qty"] * strategy.levels[index]
            for index, order in strategy.orders.items()
        )
        self.assertAlmostEqual(notional, 1800.0, places=6)

    def test_rebuild_orders_arms_the_rung_just_below_price(self):
        _, strategy = make_adaptive_stack()

        # Price sits between levels 0 (95) and 1 (97): level 0 must stay armed.
        strategy._rebuild_orders(95.5)

        self.assertEqual(sorted(strategy.orders), [0])

    def test_rebuild_orders_places_sells_only_above_price(self):
        _, strategy = make_adaptive_stack(position_qty=10.0, avg_entry=100.0)

        strategy._rebuild_orders(100.0)

        sell_levels = sorted(
            index for index, order in strategy.orders.items() if order["side"] == "sell"
        )
        self.assertEqual(sell_levels, [3, 4, 5])

    def test_initialize_applies_risk_gates_before_first_build(self):
        _, strategy = make_adaptive_stack(
            {
                "atr": 1.0,
                "support": 80.0,
                "ctx_range_low": 90.0,
                "ctx_range_high": 110.0,
                "trend_up": False,
                "momentum": -0.01,
                # Gates read the last completed close, as the backtest does.
                "prev_close": 109.0,
            }
        )
        now = datetime(2025, 6, 1, tzinfo=dt_timezone.utc)

        strategy.initialize([], 109.0, now)

        self.assertFalse(strategy.allow_buys)
        self.assertEqual(strategy.orders, {})
        self.assertTrue(strategy.level_plan)
        self.assertTrue(all(item["reason"] != "active" for item in strategy.level_plan))

    def test_level_plan_records_every_level_and_persists(self):
        _, strategy = make_adaptive_stack()

        strategy._rebuild_orders(100.0)

        self.assertEqual(len(strategy.level_plan), len(strategy.levels))
        plan = {item["level"]: item for item in strategy.level_plan}
        self.assertEqual(plan[0]["side"], "buy")
        self.assertEqual(plan[0]["reason"], "active")
        self.assertGreater(plan[0]["qty"], 0.0)
        self.assertEqual(plan[3]["side"], "sell")
        self.assertEqual(plan[3]["reason"], "no_inventory")

        rebuilt = IncrementalGridStrategy(
            strategy.params, strategy.config, FakeAdapter(), strategy.to_state()
        )
        self.assertEqual(rebuilt.level_plan, strategy.level_plan)

    def test_buy_fill_replenishes_sell_rung_and_sell_refills_buy_rung(self):
        adapter, strategy = make_adaptive_stack()
        strategy._rebuild_orders(100.0)
        self.assertEqual(sorted(strategy.orders), [0, 1, 2])

        # Down bar: buy 99, 97 and 95; each arms the next rung as a sell.
        strategy.process_bar(make_bar(99.5, 99.8, 95.0, 95.5))

        self.assertGreater(adapter.position_qty, 0.0)
        sells = [i for i, order in strategy.orders.items() if order["side"] == "sell"]
        self.assertIn(1, sells)
        held = adapter.position_qty

        # Up bar: the sell at 97 fills and re-arms the buy at 95.
        strategy.process_bar(make_bar(95.5, 97.8, 95.4, 97.5))

        self.assertLess(adapter.position_qty, held)
        self.assertEqual(strategy.orders[0]["side"], "buy")

    def test_gate_rearms_buys_once_risk_clears(self):
        _, strategy = make_adaptive_stack(
            {
                "allow_buys": False,
                "ctx_range_low": 90.0,
                "ctx_range_high": 110.0,
                "trend_up": False,
                "momentum": -0.01,
                "prev_close": 95.0,
            }
        )
        self.assertEqual(strategy.orders, {})

        strategy.process_bar(make_bar(98.0, 98.5, 97.8, 98.0))

        self.assertTrue(strategy.allow_buys)
        self.assertTrue(strategy.orders)
        self.assertTrue(all(order["side"] == "buy" for order in strategy.orders.values()))

    def test_adaptive_rebalance_breakout_trigger_and_cooldown(self):
        now = datetime(2025, 6, 10, tzinfo=dt_timezone.utc)
        params = GridParams(
            grid_levels=6,
            range_lookback_days=14,
            rebalance_days=7,
            rebalance_mode="adaptive",
            range_review_hours=24,
            rebalance_cooldown_hours=12,
        )
        state = {
            "levels": [95.0, 97.0, 99.0, 101.0, 103.0, 105.0],
            "range_low": 95.0,
            "range_high": 105.0,
            "prev_close": 100.0,
            "atr": 1.0,
            "trend_up": True,
            "baseline_atr": 1.0,
            "baseline_trend_up": True,
            "last_rebalance_at": (now - timedelta(hours=13)).isoformat(),
            "last_review_at": (now - timedelta(hours=25)).isoformat(),
        }
        strategy = IncrementalGridStrategy(params, SimulationConfig(), FakeAdapter(), state)

        self.assertEqual(strategy.rebalance_reason(now, 107.0), "range_breakout")
        # The review was consumed; the next check inside 24h does nothing.
        self.assertEqual(strategy.rebalance_reason(now, 107.0), "")

        fresh = IncrementalGridStrategy(
            params,
            SimulationConfig(),
            FakeAdapter(),
            {
                **state,
                "last_rebalance_at": (now - timedelta(hours=6)).isoformat(),
            },
        )
        self.assertEqual(fresh.rebalance_reason(now, 120.0), "")

    def test_adaptive_rebalance_detects_volatility_and_regime_shifts(self):
        now = datetime(2025, 6, 10, tzinfo=dt_timezone.utc)
        params = GridParams(
            grid_levels=6,
            range_lookback_days=14,
            rebalance_mode="adaptive",
            range_review_hours=24,
            rebalance_cooldown_hours=12,
            volatility_shift_ratio=0.5,
        )
        state = {
            "levels": [95.0, 97.0, 99.0, 101.0, 103.0, 105.0],
            "range_low": 95.0,
            "range_high": 105.0,
            "atr": 2.0,
            "trend_up": True,
            "baseline_atr": 1.0,
            "baseline_trend_up": True,
            "last_rebalance_at": (now - timedelta(hours=13)).isoformat(),
            "last_review_at": (now - timedelta(hours=25)).isoformat(),
        }
        volatile = IncrementalGridStrategy(params, SimulationConfig(), FakeAdapter(), state)

        self.assertEqual(volatile.rebalance_reason(now, 100.0), "volatility_shift")

        regime = IncrementalGridStrategy(
            params,
            SimulationConfig(),
            FakeAdapter(),
            {**state, "atr": 1.0, "trend_up": False, "range_low": 90.0},
        )
        self.assertEqual(regime.rebalance_reason(now, 100.0), "regime_shift")

    def test_maybe_rebalance_rebuilds_and_counts_event(self):
        now = datetime(2025, 6, 10, tzinfo=dt_timezone.utc)
        params = GridParams(
            grid_levels=6,
            range_lookback_days=14,
            rebalance_mode="adaptive",
            range_review_hours=24,
            rebalance_cooldown_hours=12,
        )
        strategy = IncrementalGridStrategy(
            params,
            SimulationConfig(),
            FakeAdapter(),
            {
                "levels": [95.0, 97.0, 99.0, 101.0, 103.0, 105.0],
                "range_low": 95.0,
                "range_high": 105.0,
                "prev_close": 100.0,
                "atr": 1.0,
                "trend_up": True,
                "baseline_atr": 1.0,
                "baseline_trend_up": True,
                "last_rebalance_at": (now - timedelta(hours=13)).isoformat(),
                "last_review_at": (now - timedelta(hours=25)).isoformat(),
            },
        )

        self.assertTrue(strategy.maybe_rebalance(now, 107.0, []))
        self.assertEqual(strategy.last_rebalance_reason, "range_breakout")
        self.assertEqual(strategy.rebalances, 1)
        self.assertEqual(strategy.adaptive_rebalances, 1)
        self.assertEqual(strategy.baseline_atr, 1.0)
        self.assertFalse(strategy.maybe_rebalance(now + timedelta(hours=1), 120.0, []))

    def test_state_round_trip_preserves_adaptive_fields(self):
        adapter, strategy = make_adaptive_stack(
            {
                "epoch_stop": 94.0,
                "tp_active": True,
                "peak_price": 112.0,
                "funding_z": 2.5,
                "atr": 1.2,
                "trail_up_shifts": 3,
                "trailing_tp_exits": 2,
            }
        )

        rebuilt = IncrementalGridStrategy(
            strategy.params, strategy.config, adapter, strategy.to_state()
        )

        self.assertAlmostEqual(rebuilt.epoch_stop, 94.0)
        self.assertTrue(rebuilt.tp_active)
        self.assertAlmostEqual(rebuilt.peak_price, 112.0)
        self.assertAlmostEqual(rebuilt.funding_z, 2.5)
        self.assertAlmostEqual(rebuilt.atr, 1.2)
        self.assertEqual(rebuilt.trail_up_shifts, 3)
        self.assertEqual(rebuilt.trailing_tp_exits, 2)


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
