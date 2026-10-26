import math
from datetime import datetime, timedelta, timezone as dt_timezone

import numpy as np
from django.test import TestCase

from apps.engine.grid import GridParams, SimulationConfig, simulate_grid
from apps.engine.models import CoinAnalysis
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


class WalkForwardTests(TestCase):
    def test_parameter_grid_is_bounded(self):
        candidates = parameter_grid(GridParams())
        self.assertEqual(len(candidates), 12)
        signatures = {
            (c.grid_levels, c.range_lookback_days, c.rebalance_days) for c in candidates
        }
        self.assertEqual(len(signatures), 12)

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
        self.assertIn("net_return", result.oos_metrics)
        self.assertIn("recommended_params", result.chart)
        self.assertTrue(result.chart["equity_curve"])
        self.assertEqual(
            result.chart["equity_curve"], sorted(result.chart["equity_curve"], key=lambda p: p["t"])
        )


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
        self.assertEqual(ranked[1]["rank"], 2)
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
