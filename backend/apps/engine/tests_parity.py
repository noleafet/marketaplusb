"""Backtest/paper parity, simulator accounting, holdout, and validation tests."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace

import numpy as np
from django.test import SimpleTestCase

from apps.engine.grid import (
    GridParams,
    buy_crosses,
    sell_crosses,
    SimulationConfig,
    market_context,
    prepare_market,
    simulate_grid,
)
from apps.engine.validation import findings, score_validation, spearman
from apps.engine.walkforward import daily_strategy_returns, run_walk_forward
from apps.trading.execution import PaperExecutionAdapter
from apps.trading.strategy import IncrementalGridStrategy
from apps.engine.grid import funding_zscore

T0 = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)


def synthetic_rows(
    bars: int, hours: int, *, seed: int = 11, crash_at: int | None = None
) -> list[dict]:
    """Mean-reverting path with optional sharp breakdown."""

    rng = np.random.default_rng(seed)
    price = 100.0
    rows = []
    for index in range(bars):
        target = 100.0 * (1.0 + 0.08 * math.sin(index / (30.0 * 24 / hours)))
        price += 0.08 * (target - price) + rng.normal(0.0, 0.6 * math.sqrt(hours))
        if crash_at is not None and crash_at <= index < crash_at + 6:
            price *= 0.96
        price = max(price, 5.0)
        open_ = rows[-1]["close"] if rows else price
        rows.append(
            {
                "time": T0 + timedelta(hours=hours * index),
                "open": open_,
                "high": max(open_, price) * (1.0 + abs(rng.normal(0, 0.004))),
                "low": min(open_, price) * (1.0 - abs(rng.normal(0, 0.004))),
                "close": price,
            }
        )
    return rows


def synthetic_funding(rows: list[dict], *, seed: int = 5) -> list[dict]:
    rng = np.random.default_rng(seed)
    start = rows[0]["time"]
    end = rows[-1]["time"]
    events = []
    moment = start
    while moment <= end:
        events.append({"time": moment, "rate": 0.0001 + rng.normal(0, 0.00008)})
        moment += timedelta(hours=8)
    return events


def make_session(capital: float) -> SimpleNamespace:
    return SimpleNamespace(
        capital=capital,
        position_qty=0.0,
        avg_entry=0.0,
        realized_pnl=0.0,
        fees_paid=0.0,
        funding_paid=0.0,
        carry_cost=0.0,
        mark_price=0.0,
        equity=capital,
        peak_equity=capital,
        max_drawdown=0.0,
        wins=0,
        losses=0,
    )


def replay_paper(rows, funding, start_idx, params, config):
    """Drive the paper strategy bar by bar the way the paper tick does."""

    session = make_session(config.capital)
    adapter = PaperExecutionAdapter(session, config=config, params=params)
    strategy = IncrementalGridStrategy(params, config, adapter, {})
    charge_from = rows[start_idx]["time"]
    for index in range(start_idx, len(rows)):
        bar = rows[index]
        for event in funding:
            if charge_from < event["time"] <= bar["time"]:
                adapter.apply_funding(float(event["rate"]), float(bar["open"]), event["time"])
        charge_from = bar["time"]
        upto = [event for event in funding if event["time"] <= bar["time"]]
        result = strategy.step(
            bar,
            rows[:index],
            funding_z=funding_zscore(upto, params.funding_z_lookback),
        )
        if result.get("stopped"):
            break
    return adapter, strategy


def comparable(trades):
    return [
        (trade["side"], trade["reason"], float(trade["price"]), float(trade["qty"]))
        for trade in trades
        if trade["reason"] != "end_of_test"
    ]


class ParityTests(SimpleTestCase):
    """The paper strategy must reproduce the backtest fill for fill."""

    def _assert_parity(
        self,
        hours: int,
        params: GridParams,
        trade_through: float,
        test_days: int | None = None,
        **row_kw,
    ):
        interval = {1: "1h", 4: "4h"}[hours]
        bars_per_day = 24 // hours
        warmup_days = 60
        test_days = test_days or (20 if hours == 1 else 45)
        rows = synthetic_rows((warmup_days + test_days) * bars_per_day, hours, **row_kw)
        funding = synthetic_funding(rows)
        config = SimulationConfig(interval=interval, trade_through_bps=trade_through)
        start_idx = warmup_days * bars_per_day
        backtest = simulate_grid(rows, funding, rows[start_idx]["time"], params, config)
        self.assertFalse(backtest.error, backtest.error)
        adapter, _ = replay_paper(rows, funding, start_idx, params, config)

        expected = comparable(backtest.trades)
        actual = comparable(
            [
                {"side": f.side, "reason": f.reason, "price": f.price, "qty": f.qty}
                for f in adapter.fills
            ]
        )
        self.assertGreater(len(expected), 5, "scenario produced too few fills to compare")
        self.assertLess(backtest.metrics["trades"], 200, "trade list is truncated at 200")
        self.assertEqual(len(expected), len(actual))
        for index, (left, right) in enumerate(zip(expected, actual)):
            self.assertEqual(left[:2], right[:2], f"fill {index}")
            # Backtest trade logs are rounded to 8 decimals.
            self.assertTrue(math.isclose(left[2], right[2], rel_tol=1e-7), f"price {index}")
            self.assertTrue(math.isclose(left[3], right[3], rel_tol=1e-7), f"qty {index}")
        # The backtest closes leftover inventory at the end; paper keeps it.
        closing_fee = sum(
            float(trade["fee"]) for trade in backtest.trades if trade["reason"] == "end_of_test"
        )
        self.assertAlmostEqual(
            backtest.metrics["fees_paid"] - closing_fee, adapter.fees_paid, places=3
        )

    def test_1h_paper_matches_backtest_with_trade_through_fills(self):
        self._assert_parity(1, GridParams(grid_levels=12, range_lookback_days=7), 2.0)

    def test_4h_paper_matches_backtest_with_touch_fills(self):
        self._assert_parity(4, GridParams(grid_levels=16), 0.0)

    def test_parity_holds_with_adaptive_rebalancing_and_initial_inventory(self):
        params = GridParams(
            grid_levels=14,
            rebalance_mode="adaptive",
            initial_inventory_pct=0.3,
            rebalance_days=3,
        )
        self._assert_parity(4, params, 2.0, test_days=25, seed=23)

    def test_parity_holds_through_a_breakdown(self):
        self._assert_parity(
            4, GridParams(grid_levels=16, stop_atr_multiple=1.5), 2.0, seed=4, crash_at=420
        )


class SimulatorAccountingTests(SimpleTestCase):
    def setUp(self):
        self.rows = synthetic_rows(24 * 120, 1)
        self.funding = synthetic_funding(self.rows)
        self.start = self.rows[24 * 60]["time"]

    def test_trade_results_reconcile_with_net_pnl(self):
        params = GridParams(stop_atr_multiple=50.0, stop_loss_pct=0.99)
        result = simulate_grid(
            self.rows, self.funding, self.start, params, SimulationConfig(interval="1h")
        )
        metrics = result.metrics
        net = metrics["final_equity"] - 1000.0
        # Gross profit/loss now include buy fees and funding carried by the
        # inventory, so the profit factor describes the real net result.
        self.assertAlmostEqual(metrics["gross_profit"] - metrics["gross_loss"], net, places=4)

    def test_prepared_market_does_not_change_results(self):
        params = GridParams()
        config = SimulationConfig(interval="1h", trade_through_bps=2.0)
        prepared = prepare_market(self.rows, self.funding, params, config.bars_per_day)
        prefix = self.rows[: 24 * 100]
        fresh = simulate_grid(prefix, self.funding, self.start, params, config)
        cached = simulate_grid(
            prefix, self.funding, self.start, params, config, prepared=prepared
        )
        self.assertEqual(fresh.metrics, cached.metrics)
        self.assertEqual(fresh.trades, cached.trades)

    def test_trade_through_requires_price_to_pass_the_level(self):
        level = 100.0
        # A low that only touches the level fills under the touch model only.
        self.assertTrue(buy_crosses(level, 101.0, 100.0, 0.0))
        self.assertFalse(buy_crosses(level, 101.0, 100.0, 0.0002))
        self.assertTrue(buy_crosses(level, 101.0, 99.97, 0.0002))
        self.assertTrue(sell_crosses(level, 99.0, 100.0, 0.0))
        self.assertFalse(sell_crosses(level, 99.0, 100.01, 0.0002))
        self.assertTrue(sell_crosses(level, 99.0, 100.03, 0.0002))

    def test_support_breakdown_is_detectable(self):
        rows = synthetic_rows(6 * 90, 4, crash_at=6 * 80)
        params = GridParams()
        detected = 0
        for end in range(6 * 81, 6 * 84):
            history = rows[:end]
            closes = np.array([r["close"] for r in history])
            ctx = market_context(
                np.array([r["high"] for r in history]),
                np.array([r["low"] for r in history]),
                closes,
                float(closes[-1]),
                params,
                6,
            )
            if closes[-1] < ctx.support - params.support_buffer_atr * ctx.atr:
                detected += 1
        # Support now excludes the latest bar, so a close through it registers.
        self.assertGreater(detected, 0)


class WalkForwardHoldoutTests(SimpleTestCase):
    def test_holdout_is_untouched_and_reported(self):
        rows = synthetic_rows(6 * 400, 4)
        funding = synthetic_funding(rows)
        result = run_walk_forward(
            rows,
            funding,
            config=SimulationConfig(interval="4h", trade_through_bps=2.0),
            base_params=GridParams(),
            lookback_days=520,
            min_trades=4,
            sensitivity=False,
            holdout_days=60,
        )
        self.assertFalse(result.error, result.error)
        holdout_start = datetime.fromisoformat(result.holdout_metrics["start"])
        for fold in result.folds:
            self.assertLessEqual(datetime.fromisoformat(fold["test_end"]), holdout_start)
        self.assertIsNotNone(result.oos_metrics["holdout_return"])
        # Deployed parameters are the most recent fold's selection.
        self.assertEqual(result.params, result.folds[-1]["params"])
        # Profit factor is aggregated from gross profit and loss.
        gp = result.oos_metrics["gross_profit"]
        gl = result.oos_metrics["gross_loss"]
        if gl > 0:
            self.assertAlmostEqual(result.oos_metrics["profit_factor"], round(gp / gl, 4), places=3)
        self.assertEqual(result.oos_metrics["alt_fill_model"], "touch")
        # The reported data end includes the holdout, and the chart draws it
        # after the folds as its own segment.
        self.assertEqual(result.data_end, rows[-1]["time"])
        segments = [point.get("segment") for point in result.chart["equity_curve"]]
        self.assertIn("holdout", segments)
        self.assertEqual(segments[-1], "holdout")
        self.assertEqual(segments.index("holdout"), segments.count("oos"))
        self.assertEqual(result.chart["holdout"]["start"], result.holdout_metrics["start"])
        # Every stopped fold is drawn flat up to its test end.
        for fold in result.folds:
            if fold["test_metrics"].get("stopped"):
                fold_points = [p for p in result.chart["equity_curve"] if p.get("fold") == fold["fold"]]
                self.assertEqual(fold_points[-1]["t"], fold["test_end"])
                # The stop itself is kept, so the flat stretch starts at the stop.
                self.assertEqual(fold_points[-2]["equity"], fold_points[-1]["equity"])
        self.assertTrue(result.strategy_returns)

    def test_daily_strategy_returns_fill_flat_days_after_a_stop(self):
        returns = daily_strategy_returns(
            {"2025-01-01": 1010.0, "2025-01-02": 1000.0},
            1000.0,
            datetime(2025, 1, 5, tzinfo=dt_timezone.utc),
        )
        self.assertEqual(len(returns), 5)
        self.assertEqual(returns["2025-01-05"], 0.0)


class ValidationTests(SimpleTestCase):
    def test_spearman_handles_ties_and_order(self):
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(spearman([1, 2, 3, 4], [4, 3, 2, 1]), -1.0)
        self.assertIsNone(spearman([1, 1, 1], [1, 2, 3]))

    def test_mostly_flat_strategies_are_not_reported_as_correlated(self):
        from apps.engine.robustness import _pair_correlation

        days = [f"2025-01-{d:02d}" for d in range(1, 31)]
        # Both strategies stopped early: zero returns after two shared days.
        left = {day: 0.0 for day in days}
        right = {day: 0.0 for day in days}
        left[days[0]], right[days[0]] = -0.02, -0.03
        left[days[1]], right[days[1]] = 0.01, 0.015
        self.assertIsNone(_pair_correlation(left, right, 90))

        rng = np.random.default_rng(3)
        active_left = {day: float(rng.normal(0, 0.01)) for day in days}
        active_right = {day: value * 0.5 + 0.001 for day, value in active_left.items()}
        self.assertAlmostEqual(_pair_correlation(active_left, active_right, 90), 1.0, places=6)

    def test_losing_folds_with_a_lucky_holdout_is_not_a_confirmation(self):
        out = findings(
            {"score": {"n": 0}},
            {"eligible": 0, "scope": "universe", "oos_annualized_mean": -0.03, "holdout_annualized_mean": 0.15},
        )
        codes = {item["code"]: item["kind"] for item in out}
        self.assertEqual(codes.get("period_dependent"), "weakness")
        self.assertNotIn("holdout_consistent", codes)

    def test_score_validation_and_findings(self):
        items = []
        for index in range(12):
            items.append(
                {
                    "score": float(index),
                    "eligible": index >= 6,
                    "component_scores": {"profitability": float(index)},
                    "oos_metrics": {"holdout_return": index / 100.0, "net_return": 0.1},
                }
            )
        scores = score_validation(items)
        self.assertEqual(scores["coins"], 12)
        self.assertAlmostEqual(scores["score"]["ic"], 1.0)
        self.assertTrue(scores["score"]["significant"])
        out = findings(
            scores,
            {
                "eligible": 6,
                "strategy_correlation": {"avg_pair": 0.8, "max_pair": 0.95, "high_pairs": []},
                "price_correlation": {"avg_pair": 0.85},
                "btc_correlation_mean": 0.7,
                "btc_beta_mean": 0.4,
            },
            max_strategy_correlation=0.7,
        )
        codes = {item["code"]: item["kind"] for item in out}
        self.assertEqual(codes["score_predictive"], "strength")
        self.assertEqual(codes["book_concentrated"], "weakness")
        self.assertEqual(codes["market_beta"], "weakness")
