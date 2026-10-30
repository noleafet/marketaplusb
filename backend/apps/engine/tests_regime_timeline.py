"""BTC market regime in the backtest: timeline, gates, effect, and parity."""

import math
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase

from apps.engine.grid import GridParams, SimulationConfig
from apps.engine.hybrid_gate import Gate, combine_gates
from apps.engine.regime_timeline import (
    RegimeTimeline,
    build_regime_timeline,
    gate_from_timeline,
)
from apps.engine.tests import make_rows
from apps.engine.validation import findings
from apps.engine.walkforward import run_walk_forward
from apps.trading.models import GridEvent, StrategySettings
from apps.trading.regime import RegimeConfig

UTC = dt_timezone.utc
DAY = datetime(2026, 9, 1, tzinfo=UTC)


def at(hour, minute=0):
    return DAY + timedelta(hours=hour, minutes=minute)


def hourly_rows(start_hour, count):
    return [{"time": at(start_hour + i)} for i in range(count)]


def default_config():
    return RegimeConfig(
        trailing_stop_pct=0.08,
        trailing_window_days=7,
        oi_flush_pct=0.05,
        oi_near_high_pct=0.03,
        funding_z=2.0,
        min_bearish_hours=4.0,
        risk=settings.RISK,
    )


class GateMappingTests(TestCase):
    def test_gate_mapping_is_causal(self):
        timeline = RegimeTimeline(changes=[(at(9), "normal"), (at(10, 15), "bearish")])
        gate = gate_from_timeline(hourly_rows(9, 3), timeline)
        # The 10:00 bar opened before BTC's 10:15 close; the 11:00 bar after it.
        self.assertTrue(gate.allow[at(10)])
        self.assertNotIn(at(10), gate.derisk)
        self.assertFalse(gate.allow[at(11)])
        self.assertTrue(gate.derisk[at(11)])
        self.assertEqual(gate.exit, {})

    def test_bearish_blocks_and_derisks_without_exit(self):
        timeline = RegimeTimeline(
            changes=[(at(9), "normal"), (at(10, 15), "bearish"), (at(14, 15), "normal")]
        )
        gate = gate_from_timeline(hourly_rows(9, 8), timeline)
        self.assertEqual(gate.exit, {})
        self.assertEqual(sorted(gate.derisk), [at(h) for h in range(11, 15)])
        self.assertEqual([gate.allow[at(h)] for h in range(9, 17)], [
            True, True, False, False, False, False, True, True,
        ])
        self.assertEqual(gate.exits, 0)
        self.assertEqual(gate.blocked_bars, 4)

    def test_warning_blocks_without_exit(self):
        timeline = RegimeTimeline(changes=[(at(9), "normal"), (at(10, 15), "warning")])
        gate = gate_from_timeline(hourly_rows(9, 4), timeline)
        self.assertFalse(gate.allow[at(11)])
        self.assertTrue(gate.derisk[at(11)])
        self.assertEqual(gate.exit, {})

    def test_bars_before_coverage_are_ungated(self):
        timeline = RegimeTimeline(changes=[(at(10, 15), "bearish")])
        gate = gate_from_timeline(hourly_rows(8, 5), timeline)
        for hour in (8, 9, 10):
            self.assertNotIn(at(hour), gate.allow)
        self.assertFalse(gate.allow[at(11)])
        self.assertTrue(gate.derisk[at(11)])

    def test_empty_timeline_gives_no_gate(self):
        self.assertIsNone(gate_from_timeline(hourly_rows(8, 5), RegimeTimeline()))
        self.assertIsNone(gate_from_timeline(hourly_rows(8, 5), None))

    def test_combine_gates(self):
        a = Gate(allow={at(1): True, at(2): False, at(3): True}, exit={at(2): True})
        b = Gate(allow={at(1): False, at(3): True, at(4): False}, exit={at(4): "btc_regime"})
        self.assertIs(combine_gates(a, None), a)
        self.assertIs(combine_gates(None, b), b)
        self.assertIsNone(combine_gates(None, None))
        both = combine_gates(a, b)
        self.assertEqual(
            both.allow, {at(1): False, at(2): False, at(3): True, at(4): False}
        )
        self.assertEqual(both.exit, {at(2): True, at(4): "btc_regime"})
        self.assertNotIn(at(5), both.allow)
        self.assertEqual((both.allowed_bars, both.blocked_bars, both.exits), (1, 3, 2))


class TimelineCacheTests(TestCase):
    def regime_timeline(self, lookback_days, *, sync=False, earliest=None, builds=None):
        from apps.engine import analysis

        timeline = RegimeTimeline(changes=[(at(9), "normal")], readings=1)
        with patch.object(
            analysis, "latest_candle_time", return_value=at(20)
        ), patch.object(
            analysis, "earliest_candle_time", return_value=earliest
        ), patch.object(analysis, "load_candles", return_value=[]), patch.object(
            analysis, "load_funding", return_value=[]
        ), patch.object(
            analysis, "build_regime_timeline", return_value=timeline
        ) as build, patch.object(analysis, "sync_coin_market_data") as sync:
            result = analysis._regime_timeline(
                object(),
                StrategySettings.load(),
                lookback_days=lookback_days,
                warmup_days=45,
                sync=sync,
            )
        if builds is not None:
            self.assertEqual(build.call_count, builds)
        return result, sync

    def setUp(self):
        from apps.engine import analysis

        analysis._REGIME_TIMELINE_CACHE.clear()

    def test_timeline_cached_for_hybrid_pass(self):
        first, sync = self.regime_timeline(520, sync=True, builds=1)
        self.assertEqual(
            sorted(call.kwargs["interval"] for call in sync.call_args_list), ["15m", "1h"]
        )
        # The shorter hybrid window is covered by the cached timeline.
        second, _ = self.regime_timeline(180, builds=0)
        self.assertIs(second, first)

    def test_longer_window_rebuilds(self):
        self.regime_timeline(120, builds=1)
        # A cached timeline starting later than this run needs would leave its
        # early bars ungated.
        self.regime_timeline(520, builds=1)

    def test_short_btc_history_is_back_filled(self):
        from django.utils import timezone

        _, sync = self.regime_timeline(520, sync=True, earliest=timezone.now() - timedelta(days=60))
        self.assertTrue(all(call.kwargs["force"] for call in sync.call_args_list))
        _, sync = self.regime_timeline(
            520, sync=True, earliest=timezone.now() - timedelta(days=800)
        )
        self.assertFalse(any(call.kwargs["force"] for call in sync.call_args_list))

    def test_disabled_regime_builds_nothing(self):
        from apps.engine import analysis

        row = StrategySettings.load()
        row.btc_regime_enabled = False
        with patch.object(analysis, "build_regime_timeline") as build:
            self.assertIsNone(
                analysis._regime_timeline(
                    object(), row, lookback_days=520, warmup_days=45, sync=True
                )
            )
        build.assert_not_called()


def btc_market(days=138, event_day=135):
    """Calm BTC, then a rally (warning), a crash (bearish), and a recovery."""

    start = datetime(2026, 4, 1, tzinfo=UTC)
    bars_15m = []
    previous = 100.0
    rally_start = event_day * 96
    rally_end = rally_start + 96
    crash_end = rally_end + 16
    recovery_end = crash_end + 2 * 96
    for i in range(days * 96):
        wobble = 0.002 * math.sin(2.0 * math.pi * i / 8.0)
        if i < rally_start:
            base = 100.0
        elif i < rally_end:
            base = 100.0 + 12.0 * (i - rally_start) / 96.0
        elif i < crash_end:
            base = 112.0 - 14.0 * (i - rally_end) / 16.0
        elif i < recovery_end:
            base = 98.0 + 8.0 * (i - crash_end) / (2 * 96)
        else:
            base = 106.0
        close = base * (1.0 + wobble)
        bars_15m.append(
            {
                "time": start + timedelta(minutes=15 * i),
                "open": previous,
                "high": max(previous, close) * 1.0005,
                "low": min(previous, close) * 0.9995,
                "close": close,
                "volume": 100.0 + (i % 5),
            }
        )
        previous = close
    bars_1h = []
    for j in range(0, len(bars_15m), 4):
        chunk = bars_15m[j : j + 4]
        bars_1h.append(
            {
                "time": chunk[0]["time"],
                "open": chunk[0]["open"],
                "high": max(bar["high"] for bar in chunk),
                "low": min(bar["low"] for bar in chunk),
                "close": chunk[-1]["close"],
                "volume": sum(bar["volume"] for bar in chunk),
            }
        )
    funding = [
        {"time": start + timedelta(hours=8 * k), "rate": 0.0001} for k in range(days * 3)
    ]
    oi = [
        {"time": start + timedelta(hours=h), "open_interest": 1000.0 + 0.5 * h}
        for h in range(days * 24)
    ]
    return bars_15m, bars_1h, funding, oi


class RegimeParityTests(TestCase):
    def test_live_and_backtest_phase_changes_match(self):
        from apps.trading.regime import LIVE_1H_DAYS, LIVE_FUNDING_DAYS, refresh_regime

        bars_15m, bars_1h, funding, oi = btc_market()
        config = RegimeConfig.from_settings(StrategySettings.load())
        timeline = build_regime_timeline(bars_15m, bars_1h, funding, oi, config)
        window_start = bars_15m[135 * 96 - 24]["time"]
        before = [change for change in timeline.changes if change[0] < window_start]
        self.assertEqual(before[-1][1], "normal")
        expected = [change for change in timeline.changes if change[0] >= window_start]
        phases = [phase for _, phase in expected]
        self.assertIn("warning", phases)
        self.assertIn("bearish", phases)
        # BTC recovered out of bearish inside the window.
        self.assertNotEqual(phases[-1], "bearish")

        def live_inputs(client, now, cfg=None):
            # Exactly the windows fetch_live_inputs requests from Binance.
            return (
                [
                    bar
                    for bar in bars_15m
                    if bar["time"] >= now - timedelta(days=8)
                    and bar["time"] + timedelta(minutes=15) <= now
                ],
                [
                    bar
                    for bar in bars_1h
                    if bar["time"] >= now - timedelta(days=LIVE_1H_DAYS)
                    and bar["time"] + timedelta(hours=1) <= now
                ],
                [
                    row
                    for row in funding
                    if now - timedelta(days=LIVE_FUNDING_DAYS) <= row["time"] <= now
                ],
                [row for row in oi if row["time"] <= now][-6:],
            )

        closes = [
            bar["time"] + timedelta(minutes=15)
            for bar in bars_15m
            if bar["time"] + timedelta(minutes=15) >= window_start
        ]
        with patch("apps.trading.regime.fetch_live_inputs", side_effect=live_inputs):
            for moment in closes:
                refresh_regime(moment, client=object())
        live = [
            (event.time, event.after["phase"])
            for event in GridEvent.objects.filter(type=GridEvent.Type.REGIME).order_by("time")
        ]
        self.assertEqual(live, expected)


class WalkForwardRegimeTests(TestCase):
    def test_walk_forward_reports_regime_effect(self):
        rows = make_rows(days=300)
        kwargs = {
            "config": SimulationConfig(),
            "base_params": GridParams(grid_levels=10, range_lookback_days=14, rebalance_days=7),
            "lookback_days": 300,
            "min_trades": 5,
            "sensitivity": False,
        }
        plain = run_walk_forward(rows, [], **kwargs)
        self.assertEqual(plain.error, "")
        self.assertNotIn("regime_effect_return", plain.oos_metrics)

        # A bearish spell inside the last test window only, so every training
        # window (and so every chosen parameter set) is unchanged.
        last_test = datetime.fromisoformat(plain.folds[-1]["test_start"])
        spell = [row["time"] for row in rows if last_test + timedelta(days=3) <= row["time"]]
        blocked = spell[:30]
        gate = Gate(
            allow={moment: moment not in blocked for moment in spell},
            exit={blocked[0]: "btc_regime"},
        )
        regimed = run_walk_forward(rows, [], regime_gate=gate, **kwargs)
        self.assertEqual(regimed.error, "")
        self.assertEqual(
            [fold["params"] for fold in regimed.folds], [fold["params"] for fold in plain.folds]
        )
        oos = regimed.oos_metrics
        for key in ("regime_off_return", "regime_effect_return", "regime_effect_drawdown"):
            self.assertIn(key, oos)
        self.assertAlmostEqual(oos["regime_off_return"], plain.oos_metrics["net_return"], places=6)
        self.assertAlmostEqual(
            oos["regime_effect_return"], oos["net_return"] - oos["regime_off_return"], places=6
        )
        self.assertNotEqual(oos["net_return"], plain.oos_metrics["net_return"])


class RegimeFindingTests(TestCase):
    def codes(self, strategies):
        return {item["code"]: item for item in findings({}, strategies)}

    def test_validation_regime_finding(self):
        good = self.codes(
            {"regime_effect_return_mean": -0.012, "regime_effect_drawdown_mean": 0.041}
        )
        self.assertEqual(good["regime_effective"]["kind"], "strength")
        self.assertIn("cut max drawdown by 4.1 points", good["regime_effective"]["detail"])
        self.assertIn("cost of 1.2 points of return", good["regime_effective"]["detail"])

        bad = self.codes(
            {"regime_effect_return_mean": -0.05, "regime_effect_drawdown_mean": 0.02}
        )
        self.assertEqual(bad["regime_costly"]["kind"], "weakness")
        self.assertNotIn("regime_effective", bad)

        absent = self.codes({})
        self.assertNotIn("regime_effective", absent)
        self.assertNotIn("regime_costly", absent)


class DeriskParityTests(TestCase):
    """Nearest-level sells: the paper strategy reproduces the backtest fill for fill."""

    def replay(self, hours, params, trade_through, *, seed, window, test_days=30):
        from apps.engine.grid import funding_zscore, simulate_grid
        from apps.engine.tests_parity import (
            comparable,
            make_session,
            synthetic_funding,
            synthetic_rows,
        )
        from apps.trading.execution import PaperExecutionAdapter
        from apps.trading.strategy import IncrementalGridStrategy

        bars_per_day = 24 // hours
        rows = synthetic_rows((60 + test_days) * bars_per_day, hours, seed=seed)
        funding = synthetic_funding(rows)
        config = SimulationConfig(
            interval={1: "1h", 4: "4h"}[hours], trade_through_bps=trade_through
        )
        start_idx = 60 * bars_per_day
        first, last = window
        derisk = {
            rows[i]["time"]: True
            for i in range(start_idx + first * bars_per_day, start_idx + last * bars_per_day)
        }
        gate = Gate(allow={}, exit={}, derisk=derisk)
        start = rows[start_idx]["time"]
        backtest = simulate_grid(rows, funding, start, params, config, gate=gate)
        ungated = simulate_grid(rows, funding, start, params, config)

        adapter = PaperExecutionAdapter(
            make_session(config.capital), config=config, params=params
        )
        strategy = IncrementalGridStrategy(params, config, adapter, {})
        charge_from = start
        for index in range(start_idx, len(rows)):
            bar = rows[index]
            for event in funding:
                if charge_from < event["time"] <= bar["time"]:
                    adapter.apply_funding(float(event["rate"]), float(bar["open"]), event["time"])
            charge_from = bar["time"]
            # The paper runner sets the request before each tick's bars.
            strategy.derisk = bar["time"] in derisk
            upto = [event for event in funding if event["time"] <= bar["time"]]
            result = strategy.step(
                bar, rows[:index], funding_z=funding_zscore(upto, params.funding_z_lookback)
            )
            if result.get("stopped"):
                break
        paper = comparable(
            [
                {"side": f.side, "reason": f.reason, "price": f.price, "qty": f.qty}
                for f in adapter.fills
            ]
        )
        return comparable(backtest.trades), paper, comparable(ungated.trades)

    def assert_parity(self, *args, **kwargs):
        expected, actual, ungated = self.replay(*args, **kwargs)
        self.assertGreater(len(expected), 5)
        self.assertLess(len(expected), 199, "trade list is truncated at 200")
        # The de-risk window must actually change the trades.
        self.assertNotEqual(expected, ungated)
        self.assertEqual(len(expected), len(actual))
        for index, (left, right) in enumerate(zip(expected, actual)):
            self.assertEqual(left[:2], right[:2], f"fill {index}")
            self.assertTrue(math.isclose(left[2], right[2], rel_tol=1e-7), f"price {index}")
            self.assertTrue(math.isclose(left[3], right[3], rel_tol=1e-7), f"qty {index}")

    def test_1h_derisk_parity(self):
        params = GridParams(grid_levels=12, range_lookback_days=7)
        self.assert_parity(1, params, 2.0, seed=11, window=(4, 10), test_days=15)

    def test_4h_derisk_parity_with_adaptive_rebalancing(self):
        params = GridParams(grid_levels=16, rebalance_mode="adaptive", rebalance_days=3)
        self.assert_parity(4, params, 0.0, seed=23, window=(8, 20))


class DeriskLadderTests(TestCase):
    def test_sells_move_to_the_nearest_levels_and_back(self):
        from apps.engine.grid import DERISK_SELL_LEVELS
        from apps.engine.tests_parity import make_session, synthetic_rows
        from apps.trading.execution import PaperExecutionAdapter
        from apps.trading.strategy import IncrementalGridStrategy

        rows = synthetic_rows(60 * 24, 1)
        params = GridParams(grid_levels=12, range_lookback_days=7)
        config = SimulationConfig(interval="1h")
        session = make_session(config.capital)
        session.position_qty = 3.0
        session.avg_entry = 99.0
        adapter = PaperExecutionAdapter(session, config=config, params=params)
        strategy = IncrementalGridStrategy(params, config, adapter, {})
        price = float(rows[-1]["close"])
        strategy.initialize(rows, price, rows[-1]["time"])
        full = sorted(idx for idx, order in strategy.orders.items() if order["side"] == "sell")
        self.assertGreater(len(full), DERISK_SELL_LEVELS)

        strategy.derisk = True
        strategy.apply_derisk(price)
        sells = {
            idx: order["qty"] for idx, order in strategy.orders.items() if order["side"] == "sell"
        }
        self.assertEqual(sorted(sells), full[:DERISK_SELL_LEVELS])
        self.assertAlmostEqual(sum(sells.values()), 3.0)
        plan = {
            item["level"]: item["reason"] for item in strategy.level_plan if item["side"] == "sell"
        }
        self.assertEqual(plan[full[-1]], "derisk")
        self.assertTrue(strategy.to_state()["derisking"])

        strategy.derisk = False
        strategy.apply_derisk(price)
        restored = sorted(idx for idx, order in strategy.orders.items() if order["side"] == "sell")
        self.assertEqual(restored, full)
