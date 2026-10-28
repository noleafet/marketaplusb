"""Hybrid trend gate: builder semantics, simulator wiring, and run modes."""

import math
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
from django.test import TestCase

from apps.engine.analysis import latest_analysis
from apps.engine.grid import GridParams, SimulationConfig, simulate_grid
from apps.engine.hybrid_gate import Gate, build_gate
from apps.engine.models import AnalysisRun
from apps.engine.walkforward import run_walk_forward
from apps.jobs.handlers import handle_daily_analysis
from apps.jobs.models import JobRun

HOUR = timedelta(hours=1)
START = datetime(2025, 1, 1, tzinfo=dt_timezone.utc)


def _candle(time: datetime, close: float, pad: float = 0.001) -> dict:
    return {
        "time": time,
        "open": close,
        "high": close * (1.0 + pad),
        "low": close * (1.0 - pad),
        "close": close,
        "volume": 1000.0,
    }


def ramp_klines(count: int, step: timedelta, rate: float) -> list[dict]:
    """Compounding series starting at 100 with ``rate`` per ``step``."""

    price = 100.0
    rows = []
    for index in range(count):
        price *= 1.0 + rate
        rows.append(_candle(START + index * step, price))
    return rows


def oscillating_rows(days: int = 25, seed: int = 7) -> list[dict]:
    """4h candles that cycle enough for the grid to fill."""

    rng = np.random.default_rng(seed)
    rows = []
    previous = 100.0
    for i in range(days * 6):
        close = 100.0 * (
            1.0 + 0.06 * math.sin(2.0 * math.pi * i / 22.0) + 0.015 * math.sin(2.0 * math.pi * i / 5.0)
        ) + float(rng.normal(0, 0.15))
        rows.append(
            {
                "time": START + timedelta(hours=4 * i),
                "open": previous,
                "high": max(previous, close) * 1.004,
                "low": min(previous, close) * 0.996,
                "close": close,
                "volume": 1000.0,
            }
        )
        previous = close
    return rows


class GateBuilderTests(TestCase):
    def test_uptrend_allows_and_downtrend_blocks(self):
        rows = oscillating_rows()
        up_1h = ramp_klines(900, HOUR, 0.002)
        up_15m = ramp_klines(3600, timedelta(minutes=15), 0.0005)
        up_gate = build_gate(rows, up_1h, up_15m)
        self.assertGreater(up_gate.allowed_bars, 0)
        self.assertEqual(up_gate.exits, 0)

        down_1h = ramp_klines(900, HOUR, -0.002)
        down_15m = ramp_klines(3600, timedelta(minutes=15), -0.0005)
        down_gate = build_gate(rows, down_1h, down_15m)
        self.assertEqual(down_gate.allowed_bars, 0)
        self.assertGreater(down_gate.blocked_bars, 0)

    def test_two_closes_below_ema_halt_and_above_streak_resumes(self):
        # 1h closes rise 0.01/hour, so the EMA trails the price and the last 1h
        # close stays above it.
        one_h = [
            _candle(START + i * HOUR, 100.0 + 0.01 * i)
            for i in range(400)
        ]
        # 15m closes sit above the EMA except for a 5-hour dive that ends with
        # two 15m closes below, followed by two closes above.
        events = []
        for i in range(1600):
            close_time = START + timedelta(minutes=15 * (i + 1))
            if 600 <= i <= 619:
                close = 99.0
            else:
                close = 106.0
            events.append(_candle(close_time - timedelta(minutes=15), close))

        rows = oscillating_rows()
        gate = build_gate(rows, one_h, events)

        exit_bar = next(t for t in sorted(gate.exit) if gate.exit[t])
        self.assertEqual(gate.exits, 1)
        self.assertFalse(gate.allow[exit_bar])
        # 15m closes above the EMA resume after index 621; the first 4h bar
        # whose open is past that close re-arms the gate.
        resumed = min(t for t in sorted(gate.allow) if t > exit_bar and gate.allow[t])
        self.assertGreater(resumed, exit_bar)
        for bar, allowed in gate.allow.items():
            if exit_bar <= bar < resumed:
                self.assertFalse(allowed, bar)

    def test_adx_gate_requires_trend_strength(self):
        rows = oscillating_rows()
        flat_1h = [_candle(START + i * HOUR, 100.0) for i in range(400)]
        flat_15m = [_candle(START + timedelta(minutes=15 * i), 100.0) for i in range(1600)]
        without_adx = build_gate(rows, flat_1h, flat_15m, adx_enabled=False)
        with_adx = build_gate(rows, flat_1h, flat_15m, adx_enabled=True)
        # A flat market never closes above a flat EMA, so both stay blocked;
        # the ADX variant must not invent an allowance either.
        self.assertEqual(without_adx.allowed_bars, 0)
        self.assertEqual(with_adx.allowed_bars, 0)


class GateSimulationTests(TestCase):
    def test_blocked_gate_suppresses_all_buys_and_initial_inventory(self):
        rows = oscillating_rows()
        gate = Gate(allow={row["time"]: False for row in rows}, exit={})
        result = simulate_grid(
            rows,
            [],
            rows[30]["time"],
            GridParams(
                grid_levels=10,
                range_lookback_days=14,
                rebalance_days=7,
                initial_inventory_pct=0.5,
            ),
            SimulationConfig(),
            gate=gate,
        )

        self.assertEqual(result.error, "")
        self.assertEqual(result.metrics["buy_fills"], 0)
        self.assertEqual(result.metrics["hybrid_blocked_bars"], len(rows) - 30)
        self.assertFalse(any(t["reason"] == "initial" for t in result.trades))

    def test_confirmed_exit_flattens_open_inventory(self):
        flat = [
            {
                "time": START + timedelta(hours=4 * i),
                "open": 100.0,
                "high": 100.0,
                "low": 100.0,
                "close": 100.0,
                "volume": 1000.0,
            }
            for i in range(120)
        ]
        gate = Gate(
            allow={row["time"]: index < 100 for index, row in enumerate(flat)},
            exit={flat[60]["time"]: True},
        )
        result = simulate_grid(
            flat,
            [],
            flat[30]["time"],
            GridParams(
                grid_levels=10,
                range_lookback_days=14,
                rebalance_days=7,
                initial_inventory_pct=0.5,
            ),
            SimulationConfig(),
            gate=gate,
        )

        self.assertEqual(result.error, "")
        self.assertEqual(result.metrics["hybrid_exits"], 1)
        self.assertEqual(result.metrics["closed_lots"], 1)
        exits = [t for t in result.trades if t["reason"] == "hybrid_exit"]
        self.assertEqual(len(exits), 1)
        self.assertFalse(
            any(t["side"] == "buy" and t["t"] > flat[30]["time"].isoformat() for t in result.trades)
        )

    def test_blocked_window_suppresses_buys_and_rearms(self):
        rows = oscillating_rows()
        blocked_start = rows[40]["time"]
        blocked_end = rows[60]["time"]
        gate = Gate(
            allow={
                row["time"]: not (40 <= index < 60)
                for index, row in enumerate(rows)
            },
            exit={},
        )
        result = simulate_grid(
            rows,
            [],
            rows[30]["time"],
            GridParams(grid_levels=10, range_lookback_days=14, rebalance_days=7),
            SimulationConfig(),
            gate=gate,
        )

        self.assertEqual(result.error, "")
        buys_in_block = [
            t
            for t in result.trades
            if t["side"] == "buy"
            and blocked_start <= datetime.fromisoformat(t["t"]) < blocked_end
        ]
        self.assertEqual(buys_in_block, [])
        self.assertTrue(
            any(t["side"] == "buy" and datetime.fromisoformat(t["t"]) >= blocked_end for t in result.trades)
        )

    def test_walk_forward_applies_gate_to_every_fold(self):
        rows = oscillating_rows(days=200)
        gate = Gate(allow={row["time"]: False for row in rows}, exit={})
        result = run_walk_forward(
            rows,
            [],
            config=SimulationConfig(),
            base_params=GridParams(grid_levels=10, range_lookback_days=14, rebalance_days=7),
            lookback_days=200,
            min_trades=5,
            gate=gate,
        )

        self.assertEqual(result.error, "")
        self.assertGreaterEqual(len(result.folds), 1)
        self.assertGreater(result.oos_metrics["hybrid_blocked_bars"], 0)
        self.assertIn("hybrid_exits", result.oos_metrics)
        for fold in result.folds:
            self.assertEqual(fold["test_metrics"]["buy_fills"], 0)


class AnalysisModeTests(TestCase):
    def test_latest_analysis_separates_modes(self):
        normal = AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED, scope=AnalysisRun.Scope.UNIVERSE
        )
        hybrid = AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED,
            scope=AnalysisRun.Scope.UNIVERSE,
            mode=AnalysisRun.Mode.HYBRID,
        )

        self.assertEqual(latest_analysis().id, normal.id)
        self.assertEqual(latest_analysis(mode=AnalysisRun.Mode.HYBRID).id, hybrid.id)

    @patch("apps.jobs.handlers.run_daily_analysis")
    def test_daily_job_runs_hybrid_after_normal(self, run_daily):
        job = JobRun.objects.create(job_type="daily_analysis")

        def fake(**kwargs):
            mode = kwargs.get("trend_mode", AnalysisRun.Mode.NORMAL)
            return SimpleNamespace(
                id=f"{mode}-run",
                Status=AnalysisRun.Status,
                status=AnalysisRun.Status.SUCCEEDED,
                summary={"mode": mode},
                duration_seconds=1.0,
                error="",
            )

        run_daily.side_effect = fake
        result = handle_daily_analysis({}, job)

        self.assertEqual(
            [call.kwargs.get("trend_mode", AnalysisRun.Mode.NORMAL) for call in run_daily.call_args_list],
            [AnalysisRun.Mode.NORMAL, AnalysisRun.Mode.HYBRID],
        )
        self.assertEqual(result["analysis_id"], "normal-run")
        self.assertEqual(result["hybrid"]["analysis_id"], "hybrid-run")

    @patch("apps.jobs.handlers.run_daily_analysis")
    def test_hybrid_failure_is_reported_after_normal_results(self, run_daily):
        job = JobRun.objects.create(job_type="daily_analysis")

        def fake(**kwargs):
            status = (
                AnalysisRun.Status.SUCCEEDED
                if kwargs.get("trend_mode", AnalysisRun.Mode.NORMAL) == AnalysisRun.Mode.NORMAL
                else AnalysisRun.Status.FAILED
            )
            return SimpleNamespace(
                id="run",
                Status=AnalysisRun.Status,
                status=status,
                summary={},
                duration_seconds=1.0,
                error="hybrid broke" if status == AnalysisRun.Status.FAILED else "",
            )

        run_daily.side_effect = fake
        with self.assertRaises(RuntimeError):
            handle_daily_analysis({}, job)
