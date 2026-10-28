import math
from datetime import datetime, timedelta, timezone as dt_timezone

import numpy as np
from django.test import TestCase

from apps.engine.analysis import latest_analysis
from apps.engine.compare import comparison_variants, compare_grid_variants
from apps.engine.grid import (
    GridParams,
    SimulationConfig,
    build_grid_levels,
    funding_zscore,
    market_context,
    simulate_grid,
)
from apps.engine.models import AnalysisRun, CoinAnalysis
from apps.engine.robustness import (
    classify_regime,
    correlation_diagnostics,
    correlation_penalty,
    daily_returns,
    regime_summary,
    select_diversified,
)
from apps.engine.scoring import score_results
from apps.engine.walkforward import parameter_grid, run_walk_forward


def make_rows(days: int = 75, seed: int = 11) -> list[dict]:
    """Synthetic 4h candles with a choppy but persistent oscillation."""

    rng = np.random.default_rng(seed)
    start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)
    step = timedelta(hours=4)
    count = days * 6
    rows: list[dict] = []
    previous = 100.0
    for i in range(count):
        close = 100.0 * (
            1.0
            + 0.06 * math.sin(2.0 * math.pi * i / 22.0)
            + 0.015 * math.sin(2.0 * math.pi * i / 5.0)
        ) + float(rng.normal(0, 0.15))
        open_ = previous
        high = max(open_, close) * 1.004
        low = min(open_, close) * 0.996
        rows.append(
            {
                "time": start + i * step,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000.0,
            }
        )
        previous = close
    return rows


def path_rows(
    closes: list[float],
    *,
    high_pad: float = 0.001,
    low_pad: float = 0.001,
) -> list[dict]:
    """Deterministic OHLC bars from a close series (open = previous close)."""

    start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)
    rows: list[dict] = []
    previous = closes[0]
    for index, close in enumerate(closes):
        rows.append(
            {
                "time": start + index * timedelta(hours=4),
                "open": previous,
                "high": max(previous, close) * (1.0 + high_pad),
                "low": min(previous, close) * (1.0 - low_pad),
                "close": close,
                "volume": 1000.0,
            }
        )
        previous = close
    return rows


class GridSimulationTests(TestCase):
    def setUp(self):
        self.rows = make_rows()
        self.params = GridParams(grid_levels=10, range_lookback_days=14, rebalance_days=7)
        self.config = SimulationConfig()

    def test_simulation_is_deterministic(self):
        start = self.rows[0]["time"] + timedelta(days=30)
        first = simulate_grid(self.rows, [], start, self.params, self.config)
        second = simulate_grid(self.rows, [], start, self.params, self.config)
        self.assertEqual(first.error, "")
        self.assertEqual(first.metrics, second.metrics)
        self.assertEqual(first.trades, second.trades)
        self.assertEqual(first.equity_curve, second.equity_curve)

    def test_future_bars_do_not_change_past_results(self):
        data_start = self.rows[0]["time"]
        start = data_start + timedelta(days=30)
        cutoff = data_start + timedelta(days=60)
        full = simulate_grid(self.rows, [], start, self.params, self.config)
        truncated = simulate_grid(
            [row for row in self.rows if row["time"] <= cutoff],
            [],
            start,
            self.params,
            self.config,
        )
        self.assertEqual(full.error, "")
        self.assertEqual(truncated.error, "")
        self.assertGreater(len(full.trades), 0)

        full_before_cutoff = [
            trade
            for trade in full.trades
            if datetime.fromisoformat(trade["t"]) <= cutoff
        ]
        truncated_without_close = [
            trade for trade in truncated.trades if trade["reason"] != "end_of_test"
        ]
        self.assertEqual(full_before_cutoff, truncated_without_close)

        full_curve = [
            point
            for point in full.equity_curve
            if datetime.fromisoformat(point["t"]) < cutoff
        ]
        truncated_curve = [
            point
            for point in truncated.equity_curve
            if datetime.fromisoformat(point["t"]) < cutoff
        ]
        self.assertEqual(full_curve, truncated_curve)

    def test_metrics_include_exposure_cycle_and_recovery(self):
        start = self.rows[0]["time"] + timedelta(days=30)
        result = simulate_grid(self.rows, [], start, self.params, self.config)
        self.assertEqual(result.error, "")
        metrics = result.metrics
        for key in (
            "avg_exposure_ratio",
            "max_exposure_ratio",
            "max_unrealized_loss",
            "max_unrealized_loss_pct",
            "buy_fills",
            "sell_fills",
            "cycle_completion_ratio",
            "top_trade_share",
            "slippage_paid",
            "recovery_days",
            "max_drawdown_duration_days",
            "recovered",
        ):
            self.assertIn(key, metrics)
        self.assertGreater(metrics["buy_fills"], 0)
        self.assertGreaterEqual(
            metrics["max_exposure_ratio"], metrics["avg_exposure_ratio"]
        )
        self.assertGreaterEqual(metrics["cycle_completion_ratio"], 0.0)
        self.assertGreaterEqual(metrics["recovery_days"], 0.0)

    def test_rebuild_arms_the_level_just_below_the_open(self):
        start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)
        rows: list[dict] = []
        for index in range(121):
            price = 100.0 + 4.0 * math.sin(index / 6.0)
            when = start + timedelta(hours=4 * index)
            if index == 120:
                rows.append(
                    {"time": when, "open": 100.0, "high": 100.0, "low": 98.4, "close": 98.9}
                )
            else:
                rows.append(
                    {
                        "time": when,
                        "open": price,
                        "high": price * 1.004,
                        "low": price * 0.996,
                        "close": price,
                    }
                )
        params = GridParams(
            grid_levels=10,
            range_lookback_days=14,
            rebalance_days=365,
            stop_atr_multiple=1e9,
        )

        result = simulate_grid(rows, [], rows[120]["time"], params, SimulationConfig())

        self.assertEqual(result.error, "")
        expected = max(
            index for index, level in enumerate(result.grid_levels) if level < 100.0
        )
        buys = [trade for trade in result.trades if trade["side"] == "buy"]
        self.assertTrue(buys)
        self.assertEqual(buys[0]["level"], expected)


class AdaptiveGridTests(TestCase):
    """The five adaptive overlays, each isolated on a deterministic regime."""

    def setUp(self):
        self.config = SimulationConfig()
        self.warmup = [100.0 + 0.3 * math.sin(i / 9.0) for i in range(360)]
        self.start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc) + timedelta(days=60)

    def test_hard_stop_caps_breakdown_drawdown(self):
        decline = list(self.warmup[-1] * np.cumprod([0.99] * 30))
        rows = path_rows(self.warmup + decline)
        tight = simulate_grid(
            rows,
            [],
            self.start,
            GridParams(
                grid_levels=10, range_lookback_days=14, rebalance_days=7, stop_atr_multiple=1.5
            ),
            self.config,
        )
        wide = simulate_grid(
            rows,
            [],
            self.start,
            GridParams(
                grid_levels=10, range_lookback_days=14, rebalance_days=7, stop_atr_multiple=1e9
            ),
            self.config,
        )

        self.assertEqual(tight.error, "")
        self.assertTrue(tight.metrics["stopped"])
        self.assertEqual(tight.metrics["stop_reason"], "stop_loss")
        self.assertLess(tight.metrics["max_drawdown"], wide.metrics["max_drawdown"])
        self.assertLess(tight.metrics["bars"], wide.metrics["bars"])

    def test_trailing_take_profit_is_tuned_by_atr_multiple(self):
        rise = list(self.warmup[-1] * np.cumprod([1.0025] * 10))
        fall = list(rise[-1] * np.cumprod([0.98] * 4))
        rows = path_rows(self.warmup + rise + fall)
        common = dict(
            grid_levels=10,
            range_lookback_days=14,
            rebalance_days=7,
            initial_inventory_pct=0.8,
            tp_activate_net_return=0.005,
            stop_atr_multiple=1e9,
        )
        tight = simulate_grid(
            rows, [], self.start, GridParams(**common, tp_atr_multiple=1.5), self.config
        )
        loose = simulate_grid(
            rows, [], self.start, GridParams(**common, tp_atr_multiple=50.0), self.config
        )

        self.assertGreaterEqual(tight.metrics["trailing_tp_exits"], 1)
        self.assertEqual(loose.metrics["trailing_tp_exits"], 0)
        self.assertTrue(
            any(trade["reason"] == "trailing_take_profit" for trade in tight.trades)
        )
        self.assertGreater(tight.metrics["net_return"], loose.metrics["net_return"])

    def test_funding_zscore_trims_crowded_long_exposure(self):
        closes = [100.0] * 360 + [100.0 * (1.0 - 0.0002 * i) for i in range(1, 361)]
        rows = path_rows(closes, high_pad=0.0, low_pad=0.0)
        funding_rows = [
            {
                "time": datetime(2025, 1, 1, tzinfo=dt_timezone.utc) + timedelta(hours=8 * i),
                "rate": 0.0001 if i % 2 == 0 else 0.0003,
                "mark_price": 100.0,
            }
            for i in range(360)
        ]
        funding_rows[350]["rate"] = 0.002
        params = GridParams(
            grid_levels=10,
            range_lookback_days=14,
            rebalance_days=7,
            initial_inventory_pct=0.8,
            funding_z_threshold=2.0,
            funding_cut_scale=0.5,
            stop_atr_multiple=1e9,
        )
        result = simulate_grid(rows, funding_rows, self.start, params, self.config)

        self.assertGreater(result.metrics["funding_z_max"], 2.0)
        self.assertGreaterEqual(result.metrics["funding_trims"], 1)
        self.assertTrue(any(trade["reason"] == "funding_trim" for trade in result.trades))
        self.assertGreater(result.metrics["buy_blocks"], 0)

    def test_trailing_up_shifts_grid_on_confirmed_breakout(self):
        rise = [self.warmup[-1] * (1.008 ** (i + 1)) for i in range(60)]
        rows = path_rows(self.warmup + rise)
        common = dict(
            grid_levels=10,
            range_lookback_days=14,
            rebalance_days=7,
            initial_inventory_pct=0.3,
            stop_atr_multiple=1e9,
        )
        tight = simulate_grid(
            rows, [], self.start, GridParams(**common, trail_up_atr_multiple=1.0), self.config
        )
        loose = simulate_grid(
            rows, [], self.start, GridParams(**common, trail_up_atr_multiple=6.0), self.config
        )

        self.assertGreater(tight.metrics["trail_up_shifts"], 0)
        self.assertEqual(loose.metrics["trail_up_shifts"], 0)
        self.assertGreater(tight.grid_levels[-1], loose.grid_levels[-1])

    def test_market_context_reports_range_position_and_trend(self):
        closes = np.array([90.0 + 10.0 * math.sin(2.0 * math.pi * i / 60.0) for i in range(200)])
        highs = closes * 1.001
        lows = closes * 0.999
        upper = market_context(highs, lows, closes, 99.5, GridParams(), 6)
        lower = market_context(highs, lows, closes, 80.5, GridParams(), 6)

        self.assertIsNotNone(upper.range_pos)
        self.assertGreaterEqual(upper.range_pos, 0.9)
        self.assertLessEqual(lower.range_pos, 0.1)
        self.assertGreater(upper.atr, 0.0)
        self.assertGreater(upper.support, 0.0)

    def test_funding_zscore_guards_short_and_flat_history(self):
        self.assertIsNone(funding_zscore([{"rate": 0.0001}] * 8, 90))
        self.assertEqual(funding_zscore([{"rate": 0.0001}] * 12, 90), 0.0)
        events = [
            {"rate": 0.0001 if i % 2 == 0 else 0.0003} for i in range(100)
        ] + [{"rate": 0.002}]
        self.assertGreater(funding_zscore(events, 90), 2.0)


class RangeModeTests(TestCase):
    def setUp(self):
        self.closes = np.array(
            [100.0 + 8.0 * math.sin(i / 18.0) for i in range(300)]
        )
        self.highs = self.closes * 1.005
        self.lows = self.closes * 0.995

    def build(self, params: GridParams) -> tuple[np.ndarray, float, float]:
        return build_grid_levels(
            self.highs, self.lows, self.closes, 100.0, params, 6
        )

    def test_price_centered_range_is_symmetric_around_price(self):
        _, low, high = self.build(
            GridParams(range_mode="price_centered", atr_multiplier=2.0)
        )

        self.assertAlmostEqual((low + high) / 2.0, 100.0, places=6)
        self.assertLess(low, 100.0)
        self.assertGreater(high, 100.0)

    def test_atr_support_range_anchors_to_structural_support(self):
        support = float(np.min(self.lows))
        _, low, high = self.build(
            GridParams(range_mode="atr_support", atr_multiplier=3.0)
        )

        self.assertGreaterEqual(low, support)
        self.assertGreater(high, low)
        self.assertLess(low, 100.0)
        self.assertGreaterEqual(high, 100.0)

    def test_percentile_and_centered_ranges_differ(self):
        # Relax the ATR width cap so this synthetic band keeps its asymmetry
        # instead of being recentred onto the live price.
        _, pct_low, pct_high = self.build(
            GridParams(range_mode="percentile", max_range_atr=50.0)
        )
        _, centered_low, centered_high = self.build(
            GridParams(
                range_mode="price_centered", atr_multiplier=3.0, max_range_atr=50.0
            )
        )

        self.assertNotAlmostEqual(pct_low, centered_low, places=3)
        self.assertNotAlmostEqual(pct_high, centered_high, places=3)


class RiskLimitTests(TestCase):
    def setUp(self):
        self.config = SimulationConfig()
        self.warmup = [100.0 + 0.3 * math.sin(i / 9.0) for i in range(360)]
        self.start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc) + timedelta(days=60)

    def test_portfolio_drawdown_limit_stops_before_liquidation(self):
        decline = list(self.warmup[-1] * np.cumprod([0.99] * 30))
        rows = path_rows(self.warmup + decline)
        common = dict(
            grid_levels=10,
            range_lookback_days=14,
            rebalance_days=7,
            initial_inventory_pct=0.8,
            stop_atr_multiple=1e9,
            stop_loss_pct=0.99,
        )
        tight = simulate_grid(
            rows,
            [],
            self.start,
            GridParams(**common, max_total_drawdown=0.05),
            self.config,
        )
        unlimited = simulate_grid(
            rows, [], self.start, GridParams(**common, max_total_drawdown=0.0), self.config
        )

        self.assertTrue(tight.metrics["stopped"])
        self.assertEqual(tight.metrics["stop_reason"], "drawdown_stop")
        self.assertFalse(tight.metrics["liquidated"])
        self.assertFalse(unlimited.metrics["stopped"])
        self.assertLess(tight.metrics["bars"], unlimited.metrics["bars"])

    def test_inventory_cap_bounds_grid_notional(self):
        rows = make_rows(days=75)
        params = GridParams(
            grid_levels=10,
            range_lookback_days=14,
            rebalance_days=7,
            max_inventory_pct=1.0,
            stop_atr_multiple=1e9,
        )
        start = rows[0]["time"] + timedelta(days=30)
        result = simulate_grid(rows, [], start, params, self.config)

        self.assertEqual(result.error, "")
        self.assertLessEqual(
            result.metrics["max_position_notional"],
            self.config.capital * params.max_inventory_pct + 1e-6,
        )

    def test_leverage_liquidation_fires_before_the_equity_stop(self):
        decline = list(self.warmup[-1] * np.cumprod([0.94] * 40))
        rows = path_rows(self.warmup + decline)
        config = SimulationConfig(leverage=5.0)
        result = simulate_grid(
            rows,
            [],
            self.start,
            GridParams(
                grid_levels=10,
                range_lookback_days=14,
                rebalance_days=7,
                initial_inventory_pct=0.9,
                stop_atr_multiple=1e9,
                stop_loss_pct=0.99,
                # Disable the inventory cap so leverage, not the cap, sets the
                # liquidation distance.
                max_inventory_pct=0.0,
            ),
            config,
        )

        self.assertEqual(result.error, "")
        self.assertTrue(result.metrics["liquidated"])
        self.assertEqual(result.metrics["stop_reason"], "liquidation")
        self.assertLess(result.metrics["final_equity"], config.capital)
        self.assertGreaterEqual(result.metrics["final_equity"], 0.0)


class AdaptiveRebalanceTests(TestCase):
    def setUp(self):
        self.config = SimulationConfig()
        self.warmup = [100.0 + 0.3 * math.sin(i / 9.0) for i in range(360)]
        self.start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc) + timedelta(days=60)

    def test_adaptive_mode_rebalances_on_regime_shift(self):
        decline = list(self.warmup[-1] * np.cumprod([0.995] * 40))
        rows = path_rows(self.warmup + decline)
        common = dict(
            grid_levels=10,
            range_lookback_days=14,
            rebalance_days=30,
            stop_atr_multiple=1e9,
        )
        adaptive = simulate_grid(
            rows,
            [],
            self.start,
            GridParams(
                **common,
                rebalance_mode="adaptive",
                range_review_hours=24,
                rebalance_cooldown_hours=12,
            ),
            self.config,
        )
        fixed = simulate_grid(
            rows, [], self.start, GridParams(**common, rebalance_mode="fixed"), self.config
        )

        self.assertEqual(adaptive.error, "")
        self.assertGreater(adaptive.metrics["adaptive_rebalances"], 0)
        self.assertEqual(fixed.metrics["adaptive_rebalances"], 0)
        self.assertIn("time_outside_range", adaptive.metrics)

    def test_fixed_mode_keeps_scheduled_rebalances(self):
        rows = make_rows(days=75)
        start = rows[0]["time"] + timedelta(days=30)
        result = simulate_grid(
            rows,
            [],
            start,
            GridParams(
                grid_levels=10,
                range_lookback_days=14,
                rebalance_days=7,
                rebalance_mode="fixed",
            ),
            self.config,
        )

        self.assertEqual(result.error, "")
        self.assertEqual(result.metrics["adaptive_rebalances"], 0)
        self.assertGreaterEqual(result.metrics["rebalances"], 1)


class ComparisonTests(TestCase):
    def test_matrix_covers_requested_variants(self):
        variants = comparison_variants(GridParams())

        self.assertEqual(len(variants), 24)
        counter = {}
        for variant in variants:
            counter[variant.group] = counter.get(variant.group, 0) + 1
        self.assertEqual(counter["levels_atr_rebalance"], 18)
        self.assertEqual(counter["range_mode"], 3)
        self.assertEqual(counter["drawdown_limit"], 3)
        self.assertEqual(
            {variant.params.range_mode for variant in variants if variant.group == "range_mode"},
            {"percentile", "price_centered", "atr_support"},
        )
        self.assertEqual(
            {variant.params.max_total_drawdown for variant in variants if variant.group == "drawdown_limit"},
            {0.05, 0.10, 0.15},
        )

    def test_comparison_runs_every_variant_on_one_window(self):
        rows = make_rows(days=240)
        config = SimulationConfig()
        report = compare_grid_variants(
            rows,
            [],
            config=config,
            base_params=GridParams(
                grid_levels=10, range_lookback_days=14, rebalance_days=7
            ),
            holdout_days=60,
        )

        self.assertEqual(report["error"], "")
        self.assertEqual(len(report["rows"]), 24)
        for group in ("levels_atr_rebalance", "range_mode", "drawdown_limit"):
            self.assertIn(group, report["groups"])
            self.assertIn(group, report["best"])
            ranked = report["groups"][group]
            self.assertEqual(ranked[0]["rank"], 1)
            self.assertGreaterEqual(
                ranked[0]["net_return"], ranked[-1]["net_return"]
            )
        sample = report["rows"][0]
        for key in (
            "net_return",
            "net_pnl",
            "max_drawdown",
            "fees_paid",
            "funding_paid",
            "time_outside_range",
        ):
            self.assertIn(key, sample)


class WalkForwardTests(TestCase):
    def test_parameter_grid_is_bounded(self):
        candidates = parameter_grid(GridParams())
        self.assertEqual(len(candidates), 96)
        signatures = {
            (
                c.grid_levels,
                c.range_lookback_days,
                c.rebalance_days,
                c.stop_atr_multiple,
                c.tp_atr_multiple,
                c.rebalance_mode,
            )
            for c in candidates
        }
        self.assertEqual(len(signatures), 96)

    def test_folds_use_untouched_test_windows(self):
        rows = make_rows(days=300)
        config = SimulationConfig()
        result = run_walk_forward(
            rows,
            [],
            config=config,
            base_params=GridParams(
                grid_levels=10, range_lookback_days=14, rebalance_days=7
            ),
            lookback_days=300,
            min_trades=5,
        )
        self.assertEqual(result.error, "")
        self.assertGreaterEqual(len(result.folds), 1)
        self.assertEqual(len(result.folds), result.oos_metrics["folds"])
        for fold in result.folds:
            self.assertEqual(fold["train_end"], fold["test_start"])
            self.assertLess(fold["train_start"], fold["train_end"])
            self.assertLess(fold["test_start"], fold["test_end"])
            self.assertIn("net_return", fold["test_metrics"])
            self.assertTrue(fold["params"]["grid_levels"])
            self.assertIn("label", fold["regime"])
            self.assertIn("net_return", fold["stress"])
            self.assertTrue(fold["sensitivity"])
            self.assertIn("variant", fold["sensitivity"][0])
        self.assertIn("net_return", result.oos_metrics)
        for key in (
            "cost_stress_return",
            "cost_stress_profitable",
            "parameter_sensitivity",
            "regime_robustness",
            "regimes_covered",
            "dominant_regime",
            "max_exposure_ratio",
            "max_unrealized_loss_pct",
            "cycle_completion_ratio",
            "top_trade_share",
            "fold_return_std",
            "recovery_days",
        ):
            self.assertIn(key, result.oos_metrics)
        for key in ("regime", "cost_stress", "sensitivity", "exposure", "execution"):
            self.assertIn(key, result.diagnostics)
        self.assertIn("recommended_params", result.chart)
        self.assertTrue(result.chart["equity_curve"])
        self.assertEqual(
            result.chart["equity_curve"], sorted(result.chart["equity_curve"], key=lambda p: p["t"])
        )
        selection = result.oos_metrics["adaptive_selection"]
        self.assertEqual(len(selection), len(result.folds))
        for item in selection:
            self.assertIn(item["stop_atr_multiple"], (1.5, 2.5))
            self.assertIn(item["tp_atr_multiple"], (1.5, 3.0))
        self.assertIn("stop_atr_multiple", result.params)
        self.assertIn("tp_atr_multiple", result.params)
        self.assertIn("stop_atr_multiple", result.chart["recommended_params"])
        self.assertIn("trailing_tp_exits", result.oos_metrics)
        self.assertGreaterEqual(result.oos_metrics["adaptive_stability"], 0.0)
        self.assertLessEqual(result.oos_metrics["adaptive_stability"], 1.0)


class ScoringTests(TestCase):
    def _result(self, symbol: str, **overrides) -> dict:
        features = {
            "adv_usd_median": 12_000_000.0,
            "cost_to_edge": 0.08,
            "funding_long_annual_cost": 0.02,
            "mean_reversion_score": 72.0,
            "range_stability_score": 68.0,
            "volume_cv": 0.4,
            "data_days": 300,
            **overrides.pop("features", {}),
        }
        oos = {
            "net_return": 0.22,
            "calmar": 1.4,
            "max_drawdown": 0.11,
            "sharpe": 1.1,
            "profit_factor": 1.5,
            "win_rate": 0.58,
            "trades": 40,
            "fold_consistency": 1.0,
            "worst_fold_return": 0.02,
            "parameter_stability": 0.67,
            "time_in_range": 0.65,
            "stopped": False,
            "liquidated": False,
            **overrides.pop("oos", {}),
        }
        return {
            "coin": type("Coin", (), {"symbol": symbol})(),
            "eligible": True,
            "features": features,
            "oos_metrics": oos,
            "folds": overrides.pop("folds", [{"fold": 1}]),
            **overrides,
        }

    def test_gates_ranking_and_suitability(self):
        good = self._result("GOOD")
        bad = self._result(
            "BAD",
            oos={
                "net_return": -0.08,
                "calmar": -0.2,
                "max_drawdown": 0.62,
                "profit_factor": 0.2,
                "win_rate": 0.2,
                "trades": 3,
                "fold_consistency": 0.32,
                "worst_fold_return": -0.12,
                "parameter_stability": 0.1,
                "time_in_range": 0.2,
                "stopped": True,
            },
            features={
                "adv_usd_median": 1_000_000.0,
                "cost_to_edge": 0.45,
                "funding_long_annual_cost": 0.12,
                "mean_reversion_score": 20.0,
                "range_stability_score": 18.0,
                "volume_cv": 1.4,
                "data_days": 60,
            },
            folds=[],
        )

        ranked = score_results(
            [bad, good],
            min_adv_usd=5_000_000.0,
            min_trades=12,
            max_drawdown=0.35,
        )

        self.assertEqual([item["coin"].symbol for item in ranked], ["GOOD", "BAD"])
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertTrue(ranked[0]["eligible"])
        self.assertEqual(ranked[0]["suitability"], CoinAnalysis.Suitability.PRIME)
        self.assertGreater(ranked[0]["score"], 60)

        self.assertFalse(ranked[1]["eligible"])
        self.assertEqual(ranked[1]["suitability"], CoinAnalysis.Suitability.INELIGIBLE)
        # Ineligible coins are returned for explainability but never ranked.
        self.assertIsNone(ranked[1]["rank"])
        reasons = " ".join(ranked[1]["eligibility_reasons"])
        self.assertIn("net return", reasons)
        self.assertIn("drawdown", reasons)
        self.assertIn("fills", reasons)
        self.assertIn("folds", reasons)
        self.assertIn("history", reasons)
        self.assertIn("stop-out", reasons)

    def test_liquidity_gate_uses_configured_floor(self):
        thin = self._result("THIN", features={"adv_usd_median": 2_000_000.0})
        ranked = score_results(
            [thin], min_adv_usd=5_000_000.0, min_trades=1, max_drawdown=0.35
        )
        self.assertFalse(ranked[0]["eligible"])
        self.assertIn("liquidity", " ".join(ranked[0]["eligibility_reasons"]))

    def test_profit_factor_review_flag(self):
        item = self._result("PF12", oos={"profit_factor": 12.0})
        ranked = score_results(
            [item], min_adv_usd=5_000_000.0, min_trades=12, max_drawdown=0.35
        )
        diagnostics = ranked[0]["diagnostics"]
        validation = diagnostics["pf_validation"]
        self.assertTrue(validation["review_required"])
        self.assertFalse(validation["validation_required"])
        codes = {flag["code"] for flag in diagnostics["flags"]}
        self.assertIn("profit_factor_review", codes)
        self.assertNotIn("profit_factor_validation", codes)
        self.assertTrue(ranked[0]["eligible"])

    def test_profit_factor_validation_gate_blocks_weak_sample(self):
        item = self._result(
            "PF45",
            oos={
                "profit_factor": 45.0,
                "trades": 12,
                "stopped": True,
            },
        )
        ranked = score_results(
            [item], min_adv_usd=5_000_000.0, min_trades=12, max_drawdown=0.35
        )
        diagnostics = ranked[0]["diagnostics"]
        validation = diagnostics["pf_validation"]
        self.assertTrue(validation["validation_required"])
        self.assertFalse(validation["validation_passed"])
        self.assertFalse(validation["enough_trades"])
        self.assertIn("profit_factor_validation", {
            flag["code"] for flag in diagnostics["flags"]
        })
        reasons = " ".join(ranked[0]["eligibility_reasons"])
        self.assertIn("requires additional validation", reasons)
        self.assertIsNone(ranked[0]["rank"])

    def test_cost_stress_and_atr_gates(self):
        item = self._result(
            "HOT",
            features={"atr_pct": 0.25},
            oos={"cost_stress_return": -0.04, "cost_stress_profitable": False},
        )
        ranked = score_results(
            [item],
            min_adv_usd=5_000_000.0,
            min_trades=12,
            max_drawdown=0.35,
            max_atr_pct=0.12,
        )
        reasons = " ".join(ranked[0]["eligibility_reasons"])
        self.assertIn("ATR%", reasons)
        self.assertIn("stressed fees", reasons)
        codes = {flag["code"] for flag in ranked[0]["diagnostics"]["flags"]}
        self.assertIn("cost_stress_failed", codes)
        gate_names = {
            gate["name"]: gate for gate in ranked[0]["diagnostics"]["hard_gates"]
        }
        self.assertFalse(gate_names["atr"]["passed"])
        self.assertFalse(gate_names["cost_stress"]["passed"])

    def test_correlation_uses_only_gate_eligible_peers(self):
        days = [f"d{index:02d}" for index in range(30)]
        base = {day: 0.01 * math.sin(index / 3.0) for index, day in enumerate(days)}
        copy = {day: value * 2.0 for day, value in base.items()}
        good = self._result("GOOD", _daily_returns=base)
        twin = self._result("TWIN", _daily_returns=copy)
        bad = self._result(
            "BAD",
            oos={
                "profit_factor": 40.0,
                "trades": 5,
                "stopped": True,
                "fold_consistency": 0.0,
            },
            _daily_returns=base,
        )
        correlation_out: dict = {}
        ranked = score_results(
            [good, twin, bad],
            min_adv_usd=5_000_000.0,
            min_trades=12,
            max_drawdown=0.35,
            correlation_out=correlation_out,
        )
        by_symbol = {item["coin"].symbol: item for item in ranked}
        self.assertFalse(by_symbol["BAD"]["eligible"])
        # The ineligible coin is excluded from the pool and has no peers.
        self.assertEqual(by_symbol["BAD"]["diagnostics"]["correlation"], {})
        self.assertEqual(correlation_out["coins"], 2)
        peers = by_symbol["GOOD"]["diagnostics"]["correlation"]["peers"]
        self.assertEqual([peer["symbol"] for peer in peers], ["TWIN"])
        self.assertAlmostEqual(peers[0]["correlation"], 1.0, places=4)
        self.assertIn(
            "high_correlation",
            {flag["code"] for flag in by_symbol["GOOD"]["diagnostics"]["flags"]},
        )

    def test_score_breakdown_weights_and_baseline(self):
        good = self._result("GOOD")
        softer = self._result(
            "SOFTER",
            oos={
                "net_return": 0.08,
                "max_drawdown": 0.2,
                "profit_factor": 1.2,
                "win_rate": 0.52,
                "fold_consistency": 0.75,
                "worst_fold_return": 0.0,
            },
        )
        ranked = score_results(
            [softer, good], min_adv_usd=5_000_000.0, min_trades=12, max_drawdown=0.35
        )
        self.assertEqual(ranked[0]["coin"].symbol, "GOOD")
        self.assertEqual(ranked[1]["coin"].symbol, "SOFTER")
        self.assertEqual([item["rank"] for item in ranked], [1, 2])

        diagnostics = ranked[0]["diagnostics"]
        breakdown = diagnostics["score_breakdown"]
        self.assertEqual(len(breakdown["factors"]), 8)
        self.assertAlmostEqual(
            sum(factor["weight"] for factor in breakdown["factors"].values()),
            1.0,
            places=6,
        )
        self.assertAlmostEqual(breakdown["final_score"], ranked[0]["score"])
        self.assertGreaterEqual(breakdown["base_score"], breakdown["final_score"])
        self.assertIn("baseline_score", diagnostics)
        self.assertEqual(
            diagnostics["score_delta"],
            round(ranked[0]["score"] - diagnostics["baseline_score"], 2),
        )

        gate_names = {gate["name"] for gate in diagnostics["hard_gates"]}
        for name in (
            "history",
            "folds",
            "liquidity",
            "trades",
            "positive_return",
            "drawdown",
            "fold_consistency",
            "no_stop",
            "atr",
            "spread",
            "depth",
            "cost_stress",
            "profit_factor_validation",
        ):
            self.assertIn(name, gate_names)
        # Hard gates stay out of the factor list so eligibility cannot be
        # diluted by a strong ranking score.
        self.assertNotIn("liquidity", breakdown["factors"])
        self.assertNotIn("drawdown", breakdown["factors"])


class RobustnessTests(TestCase):
    @staticmethod
    def _node(symbol: str):
        return type("Coin", (), {"symbol": symbol})()

    def test_classify_regime_detects_trend_and_unknown(self):
        start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)
        rows = []
        price = 100.0
        for index in range(240):
            close = price * 1.01
            rows.append(
                {
                    "time": start + timedelta(hours=4 * index),
                    "open": price,
                    "high": close * 1.001,
                    "low": price * 0.999,
                    "close": close,
                }
            )
            price = close
        regime = classify_regime(rows, interval="4h")
        self.assertEqual(regime["label"], "trending")
        self.assertEqual(regime["bars"], 240)
        self.assertIsNotNone(regime["adx"])

        short = classify_regime(rows[:20], interval="4h")
        self.assertEqual(short["label"], "unknown")

    def test_regime_summary_penalises_single_regime(self):
        single = regime_summary(
            [
                {
                    "regime": {"label": "ranging"},
                    "test_metrics": {"net_return": 0.1, "max_drawdown": 0.05},
                },
                {
                    "regime": {"label": "ranging"},
                    "test_metrics": {"net_return": 0.08, "max_drawdown": 0.04},
                },
            ]
        )
        self.assertEqual(single["regimes_covered"], 1)
        self.assertTrue(single["single_regime"])
        self.assertEqual(single["regimes"]["ranging"]["folds"], 2)

        broad = regime_summary(
            [
                {
                    "regime": {"label": label},
                    "test_metrics": {"net_return": ret, "max_drawdown": dd},
                }
                for label, ret, dd in (
                    ("trending", 0.1, 0.05),
                    ("ranging", 0.08, 0.04),
                    ("high_volatility", 0.06, 0.03),
                )
            ]
        )
        self.assertEqual(broad["regimes_covered"], 3)
        self.assertFalse(broad["single_regime"])
        self.assertEqual(broad["dominant_regime"], "trending")
        self.assertAlmostEqual(broad["dominant_share"], 0.1 / 0.24, places=4)
        self.assertEqual(broad["dependence_penalty"], 0.0)
        self.assertGreater(
            broad["robustness_score"], single["robustness_score"]
        )

    def test_daily_returns_align_on_calendar_days(self):
        start = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)
        rows = [
            {"time": start + timedelta(days=index), "close": 100.0 * (1.1**index)}
            for index in range(3)
        ]
        returns = daily_returns(rows)
        self.assertEqual(sorted(returns), ["2025-01-02", "2025-01-03"])
        self.assertAlmostEqual(returns["2025-01-02"], 0.1)

    def test_correlation_diagnostics_and_diversified_selection(self):
        days = [f"d{index:02d}" for index in range(30)]
        base = {day: 0.01 * math.sin(index / 3.0) for index, day in enumerate(days)}
        inverse = {day: -value for day, value in base.items()}
        copy = {day: value * 2.0 for day, value in base.items()}
        items = [
            {"coin": self._node("AAA"), "eligible": True, "_daily_returns": base},
            {"coin": self._node("BBB"), "eligible": True, "_daily_returns": inverse},
            {"coin": self._node("CCC"), "eligible": True, "_daily_returns": copy},
        ]

        info = correlation_diagnostics(items, lookback_days=30, max_corr=0.85)
        self.assertEqual(info["coins"], 3)
        self.assertAlmostEqual(info["max_pair_correlation"], 1.0, places=6)
        self.assertTrue(any(
            pair["a"] == "AAA" and pair["b"] == "CCC" for pair in info["high_pairs"]
        ))
        self.assertAlmostEqual(items[0]["_correlation"]["max_correlation"], 1.0)
        self.assertTrue(items[0]["_correlation"]["exceeds_limit"])
        self.assertGreater(correlation_penalty(items[0], max_corr=0.85)[0], 0.0)
        self.assertEqual(correlation_penalty(items[1], max_corr=0.85)[0], 0.0)

        selected, skipped = select_diversified(items, max_corr=0.85, limit=5)
        self.assertEqual([item["coin"].symbol for item in selected], ["AAA", "BBB"])
        self.assertEqual(skipped[0]["symbol"], "CCC")
        self.assertEqual(skipped[0]["correlated_with"], "AAA")


class AnalysisScopeTests(TestCase):
    def test_latest_analysis_prefers_universe_scope(self):
        universe = AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED, scope=AnalysisRun.Scope.UNIVERSE
        )
        AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED, scope=AnalysisRun.Scope.SOLO
        )

        self.assertEqual(latest_analysis().id, universe.id)

    def test_latest_analysis_falls_back_to_solo_run(self):
        solo = AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED, scope=AnalysisRun.Scope.SOLO
        )

        self.assertEqual(latest_analysis().id, solo.id)
