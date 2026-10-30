from importlib import import_module

from django.apps import apps as django_apps
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.engine.models import AnalysisRun
from apps.engine.versioning import resolve_strategy_version
from apps.trading.models import StrategySettings


class FieldTests(TestCase):
    def test_defaults(self):
        self.assertTrue(StrategySettings.load().run_comparison_analysis)
        self.assertFalse(AnalysisRun.objects.create().drives_sessions)

    def test_comparison_toggle_does_not_change_strategy_version(self):
        a = resolve_strategy_version()
        row = StrategySettings.load()
        row.run_comparison_analysis = False
        row.save()
        self.assertEqual(resolve_strategy_version(row).id, a.id)

    def test_settings_api_exposes_and_updates_toggle(self):
        client = APIClient()
        client.credentials(HTTP_X_ENGINE_TOKEN="t")
        with override_settings(ENGINE_API_TOKEN="t"):
            got = client.get("/api/strategy/settings/")
            self.assertEqual(got.status_code, 200)
            self.assertIs(got.json()["run_comparison_analysis"], True)
            resp = client.post(
                "/api/strategy/settings/", {"run_comparison_analysis": False}, format="json"
            )
            self.assertEqual(resp.status_code, 200)
        self.assertFalse(StrategySettings.load().run_comparison_analysis)

    def test_backfill_marks_only_session_driving_runs(self):
        drove = AnalysisRun.objects.create(
            mode="normal", scope="universe", summary={"paper_sessions_created": 0})
        solo = AnalysisRun.objects.create(
            mode="normal", scope="solo", summary={"paper_sessions_created": 1})
        hyb = AnalysisRun.objects.create(mode="hybrid", scope="universe", summary={})
        old = AnalysisRun.objects.create(mode="normal", scope="universe", summary={})
        import_module(
            "apps.engine.migrations.0009_analysisrun_drives_sessions"
        ).backfill_drives_sessions(django_apps, None)
        for run in (drove, solo, hyb, old):
            run.refresh_from_db()
        self.assertTrue(drove.drives_sessions)
        # Normal solo job runs bootstrapped sessions too.
        self.assertTrue(solo.drives_sessions)
        self.assertFalse(hyb.drives_sessions)
        self.assertFalse(old.drives_sessions)


from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.utils import timezone

from apps.engine import analysis
from apps.engine.hybrid_gate import build_gate
from apps.engine.timescale import upsert_candles


def _bars(start, count, step, base=100.0):
    rows = []
    for i in range(count):
        price = base + i * 0.01 + (i % 7) * 0.05
        rows.append({
            "time": start + step * i, "open": price, "high": price + 0.5,
            "low": price - 0.5, "close": price + 0.1, "volume": 10.0,
            "quote_volume": 1000.0, "trades": 5,
        })
    return rows


def _floor(dt, step):
    secs = int(step.total_seconds())
    return dt.replace(minute=0, second=0, microsecond=0) if secs == 3600 else dt.replace(
        minute=dt.minute - dt.minute % 15, second=0, microsecond=0)


class StoredGateTests(TestCase):
    def _seed(self, symbol="AAAUSDT", with_15m=True, days=200):
        now = timezone.now()
        h = timedelta(hours=1)
        m = timedelta(minutes=15)
        end_h = _floor(now, h) - h
        end_m = _floor(now, m) - m
        h_rows = _bars(end_h - h * (days * 24), days * 24 + 1, h)
        upsert_candles(symbol, "1h", h_rows)
        m_rows = []
        if with_15m:
            m_rows = _bars(end_m - m * (days * 96), days * 96 + 1, m)
            upsert_candles(symbol, "15m", m_rows)
        return h_rows, m_rows

    def test_gate_from_stored_klines_equals_build_gate(self):
        h_rows, m_rows = self._seed()
        rows = _bars(h_rows[-1]["time"] - timedelta(hours=300), 300, timedelta(hours=1))
        end = timezone.now()
        with patch.object(analysis.timezone, "now", return_value=end):
            got = analysis._build_hybrid_gate(
                "AAAUSDT", rows, lookback_days=180, adx_enabled=False)
        start = end - timedelta(days=180 + analysis.HYBRID_WARMUP_DAYS)

        def window(bars, interval):
            return analysis._closed(
                [r for r in bars if start <= r["time"] <= end], interval, end)

        want = build_gate(
            rows, window(h_rows, "1h"), window(m_rows, "15m"), adx_enabled=False)
        self.assertIsNotNone(got)
        self.assertEqual(got.allow, want.allow)
        self.assertEqual(got.exit, want.exit)
        self.assertEqual(got.derisk, want.derisk)

    def test_forming_bar_is_excluded(self):
        self._seed(days=20)
        now = timezone.now()
        forming = _floor(now, timedelta(minutes=15))
        upsert_candles("AAAUSDT", "15m", _bars(forming, 1, timedelta(minutes=15)))
        captured = {}

        def fake(rows, k1h, k15m, *, adx_enabled=False):
            captured["k15m"] = k15m
            return SimpleNamespace(allow={1: True}, exit={}, derisk={})

        end = now  # frozen: the forming bar must still be forming at the read
        with patch("apps.engine.analysis.build_gate", fake), patch.object(
            analysis.timezone, "now", return_value=end
        ):
            analysis._build_hybrid_gate("AAAUSDT", [], lookback_days=10, adx_enabled=False)
        self.assertTrue(captured["k15m"])
        self.assertTrue(all(
            r["time"] + timedelta(minutes=15) <= end for r in captured["k15m"]))
        self.assertNotIn(forming, [r["time"] for r in captured["k15m"]])

    def test_missing_15m_returns_none(self):
        h_rows, _ = self._seed(with_15m=False, days=20)
        rows = _bars(h_rows[-1]["time"] - timedelta(hours=300), 300, timedelta(hours=1))
        self.assertIsNone(analysis._build_hybrid_gate(
            "AAAUSDT", rows, lookback_days=10, adx_enabled=False))

    def _window(self, sync, stored_days):
        coin = SimpleNamespace(binance_symbol="BBBUSDT")
        if stored_days is not None:
            end = _floor(timezone.now(), timedelta(hours=1))
            upsert_candles("BBBUSDT", "1h", _bars(
                end - timedelta(days=stored_days), 24, timedelta(hours=1)))
        analysis._sync_window(coin, object(), "1h", lookback_days=520, warmup_days=45)
        return sync.call_args

    def _gate_with_15m_from(self, start_15m, *, lookback_days, rows_start=None):
        now = timezone.now()
        h = timedelta(hours=1)
        m = timedelta(minutes=15)
        end_h = _floor(now, h) - h
        end_m = _floor(now, m) - m
        h_count = (lookback_days + analysis.HYBRID_WARMUP_DAYS + 2) * 24
        upsert_candles("AAAUSDT", "1h", _bars(end_h - h * h_count, h_count + 1, h))
        m_count = int((end_m - start_15m) / m)
        upsert_candles("AAAUSDT", "15m", _bars(end_m - m * m_count, m_count + 1, m))
        four = timedelta(hours=4)
        rows_start = rows_start or (end_h - four * 299)
        rows = _bars(rows_start, int((end_h - rows_start) / four) + 1, four)
        return analysis._build_hybrid_gate(
            "AAAUSDT", rows, lookback_days=lookback_days, adx_enabled=False)

    def test_15m_covering_only_recent_half_returns_none(self):
        start = timezone.now() - timedelta(days=20)
        self.assertIsNone(self._gate_with_15m_from(start, lookback_days=30))

    def test_young_coin_covered_from_its_first_base_bar(self):
        listed = _floor(timezone.now() - timedelta(days=15), timedelta(hours=1))
        gate = self._gate_with_15m_from(listed, lookback_days=30, rows_start=listed)
        self.assertIsNotNone(gate)

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_forces_backfill_when_history_short(self, sync):
        call = self._window(sync, 30)
        self.assertIs(call.kwargs["force"], True)
        self.assertEqual(call.kwargs["lookback_days"], 520)
        self.assertEqual(call.kwargs["warmup_days"], 45)
        self.assertEqual(call.kwargs["interval"], "1h")

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_incremental_when_covered(self, sync):
        self.assertIs(self._window(sync, 700).kwargs["force"], False)

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_forces_when_nothing_stored(self, sync):
        self.assertIs(self._window(sync, None).kwargs["force"], True)

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_young_coin_is_incremental(self, sync):
        # Listed 30 days ago: base and 1h history both start at the listing.
        listed = _floor(timezone.now(), timedelta(hours=1)) - timedelta(days=30)
        upsert_candles("BBBUSDT", "1h", _bars(listed, 24, timedelta(hours=1)))
        analysis._sync_window(
            SimpleNamespace(binance_symbol="BBBUSDT"), object(), "1h",
            lookback_days=520, warmup_days=45, floor=listed,
        )
        self.assertIs(sync.call_args.kwargs["force"], False)

    @patch("apps.engine.analysis.sync_coin_market_data")
    def test_sync_window_passes_funding_flag(self, sync):
        self.assertIs(self._window(sync, 700).kwargs["funding"], True)
        analysis._sync_window(
            SimpleNamespace(binance_symbol="BBBUSDT"), object(), "1h",
            lookback_days=520, warmup_days=45, funding=False,
        )
        self.assertIs(sync.call_args.kwargs["funding"], False)

    def test_sync_without_funding_skips_funding_fetch(self):
        from apps.engine.binance_sync import sync_coin_market_data

        client = MagicMock()
        client.klines_range.return_value = []
        coin = SimpleNamespace(binance_symbol="CCCUSDT")
        result = sync_coin_market_data(
            coin, client, lookback_days=10, interval="1h", warmup_days=1, funding=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["funding_added"], 0)
        client.funding_rates.assert_not_called()
        client.funding_rates.return_value = []
        sync_coin_market_data(coin, client, lookback_days=10, interval="1h", warmup_days=1)
        client.funding_rates.assert_called_once()


from contextlib import ExitStack
from unittest.mock import MagicMock

from django.conf import settings as django_settings

from apps.coins.models import Coin
from apps.engine.hybrid_gate import Gate
from apps.engine.models import CoinAnalysis
from apps.engine.regime_timeline import RegimeTimeline
from apps.engine.walkforward import WalkForwardResult


def _stub_wf(kwargs):
    return WalkForwardResult(
        oos_metrics={}, in_sample_metrics={}, folds=[], params={}, chart={},
        trade_sample=[], data_start=None, data_end=None, error="stub walk-forward",
    )


class RunBehaviourTests(TestCase):
    """Run-level wiring: everything below the orchestration is stubbed."""

    def setUp(self):
        analysis._REGIME_TIMELINE_CACHE.clear()
        for rank, symbol in enumerate(("AAA", "BBB"), start=1):
            Coin.objects.create(
                coingecko_id=symbol.lower(), symbol=symbol, name=symbol, rank=rank,
                binance_symbol=f"{symbol}USDT", analysis_eligible=True, in_top100=True,
            )
        row = StrategySettings.load()
        row.btc_regime_enabled = False
        row.save()
        end = _floor(timezone.now(), timedelta(hours=1)) - timedelta(hours=4)
        self.rows = _bars(end - timedelta(hours=4 * 299), 300, timedelta(hours=4))
        self.gate = Gate(allow={r["time"]: True for r in self.rows}, exit={})
        self.gates = {"AAAUSDT": self.gate, "BBBUSDT": self.gate}
        self.candles = {}

    def _run(self, *, extra=(), **kwargs):
        with ExitStack() as stack:
            def p(name, **kw):
                return stack.enter_context(patch.object(analysis, name, **kw))

            p("BinanceFuturesClient")
            self.sync = p("sync_coin_market_data", return_value={"ok": True})
            self.sync_window = p("_sync_window", return_value={"ok": True})
            p(
                "load_candles",
                side_effect=lambda symbol, interval, *a, **k: list(
                    self.candles.get(interval, self.rows)
                ),
            )
            p("load_funding", return_value=[])
            p("compute_features", return_value={})
            p("analysis_workers", return_value=1)
            self.build_gate = p(
                "_build_hybrid_gate",
                side_effect=lambda symbol, rows, **k: self.gates.get(symbol),
            )
            self.wf = p("_walk_forward_job", side_effect=_stub_wf)
            self.reports = p("build_reports", return_value=[])
            self.bootstrap = p("_bootstrap_paper_sessions")
            for name, kw in extra:
                stack.enter_context(patch.object(analysis, name, **kw))
            return analysis.run_daily_analysis(trigger="manual", **kwargs)

    def test_hybrid_run_uses_full_lookback_and_holdout(self):
        run = self._run(trend_mode=AnalysisRun.Mode.HYBRID)
        lookback = max(120, int(django_settings.ENGINE["LOOKBACK_DAYS"]))
        holdout = StrategySettings.load().holdout_days
        self.assertGreater(lookback, 180)
        self.assertGreater(holdout, 0)
        self.assertEqual(run.params["lookback_days"], lookback)
        self.assertEqual(run.params["config"]["holdout_days"], holdout)
        self.assertEqual(run.summary["hybrid"]["lookback_days"], lookback)
        self.assertEqual(run.summary["hybrid"]["warmup_days"], analysis.HYBRID_WARMUP_DAYS)
        self.assertTrue(self.wf.call_args_list)
        for call in self.wf.call_args_list:
            self.assertEqual(call.args[0]["lookback_days"], lookback)
            self.assertEqual(call.args[0]["holdout_days"], holdout)
        for call in self.build_gate.call_args_list:
            self.assertEqual(call.kwargs["lookback_days"], lookback)

    def test_only_driving_run_bootstraps_and_reports(self):
        for mode in (AnalysisRun.Mode.NORMAL, AnalysisRun.Mode.HYBRID):
            run = self._run(trend_mode=mode, drives_sessions=False)
            self.assertEqual(run.status, AnalysisRun.Status.SUCCEEDED)
            self.reports.assert_not_called()
            self.bootstrap.assert_not_called()
        run = self._run(trend_mode=AnalysisRun.Mode.HYBRID, drives_sessions=True)
        self.assertEqual(run.status, AnalysisRun.Status.SUCCEEDED)
        self.reports.assert_called_once()
        self.bootstrap.assert_called_once()
        self.assertEqual(self.bootstrap.call_args.args[0].id, run.id)

    def test_normal_driving_run_bootstraps_and_reports(self):
        self._run(trend_mode=AnalysisRun.Mode.NORMAL, drives_sessions=True)
        self.reports.assert_called_once()
        self.bootstrap.assert_called_once()

    def test_solo_hybrid_run_does_not_retire(self):
        from apps.engine.analysis import _bootstrap_paper_sessions

        with patch("apps.trading.paper.retire_stale_sessions") as retire, patch(
            "apps.trading.paper.ensure_paper_sessions", return_value=[]
        ) as ensure:
            run = self._run(
                trend_mode=AnalysisRun.Mode.HYBRID,
                drives_sessions=True,
                params={"symbols": ["AAA"]},
                extra=[("_bootstrap_paper_sessions", {"side_effect": _bootstrap_paper_sessions})],
            )
        self.assertEqual(run.scope, AnalysisRun.Scope.SOLO)
        self.assertNotIn("paper_session_error", run.summary)
        self.reports.assert_not_called()
        ensure.assert_called_once()
        retire.assert_not_called()

    def test_hybrid_primary_syncs_regime_inputs(self):
        row = StrategySettings.load()
        row.btc_regime_enabled = True
        row.save()
        regime = MagicMock(return_value=None)
        self._run(
            trend_mode=AnalysisRun.Mode.HYBRID,
            drives_sessions=True,
            extra=[("_regime_timeline", {"new": regime})],
        )
        regime.assert_called_once()
        self.assertIs(regime.call_args.kwargs["sync"], True)

    def test_hybrid_run_syncs_1h_and_15m_per_coin(self):
        run = self._run(trend_mode=AnalysisRun.Mode.HYBRID)
        lookback = run.params["lookback_days"]
        calls = {
            (c.args[0].binance_symbol, c.args[2]): c.kwargs
            for c in self.sync_window.call_args_list
        }
        self.assertEqual(len(calls), len(self.sync_window.call_args_list))
        self.assertEqual(
            set(calls),
            {(s, i) for s in ("AAAUSDT", "BBBUSDT") for i in ("1h", "15m")},
        )
        for kwargs in calls.values():
            self.assertEqual(kwargs["lookback_days"], lookback)
            self.assertEqual(kwargs["warmup_days"], analysis.HYBRID_WARMUP_DAYS)
        # The base interval is synced in hybrid runs too.
        self.assertEqual(
            sorted(c.args[0].binance_symbol for c in self.sync.call_args_list),
            ["AAAUSDT", "BBBUSDT"],
        )
        for call in self.sync.call_args_list:
            self.assertEqual(call.kwargs["interval"], run.params["interval"])
            self.assertEqual(call.kwargs["lookback_days"], lookback)

    def test_gate_syncs_skip_funding_and_floor_at_base_history(self):
        floor = self.rows[0]["time"]
        earliest = MagicMock(return_value=floor)
        run = self._run(
            trend_mode=AnalysisRun.Mode.HYBRID,
            extra=[("earliest_candle_time", {"new": earliest})],
        )
        self.assertEqual(len(self.sync_window.call_args_list), 4)
        for call in self.sync_window.call_args_list:
            self.assertIs(call.kwargs["funding"], False)
            self.assertEqual(call.kwargs["floor"], floor)
        self.assertEqual(
            sorted(c.args for c in earliest.call_args_list),
            [("AAAUSDT", run.params["interval"]), ("BBBUSDT", run.params["interval"])],
        )
        # The base-interval sync still brings funding.
        for call in self.sync.call_args_list:
            self.assertNotIn("funding", call.kwargs)

    def test_regime_sync_keeps_funding(self):
        row = StrategySettings.load()
        row.btc_regime_enabled = True
        row.save()
        sync_window = MagicMock(return_value={"ok": True})
        with patch.object(analysis, "_sync_window", sync_window), patch.object(
            analysis, "latest_candle_time", return_value=None
        ):
            analysis._regime_timeline(
                object(), row, lookback_days=10, warmup_days=5, sync=True)
        self.assertEqual(sync_window.call_count, 2)
        for call in sync_window.call_args_list:
            self.assertNotIn("funding", call.kwargs)
            self.assertNotIn("floor", call.kwargs)

    def test_partial_15m_history_excludes_coin(self):
        real_gate = analysis._build_hybrid_gate
        now = timezone.now()
        h, m = timedelta(hours=1), timedelta(minutes=15)
        end_h = _floor(now, h) - h
        end_m = _floor(now, m) - m
        start_h = self.rows[0]["time"] - timedelta(days=2)
        self.candles = {
            "1h": _bars(start_h, int((end_h - start_h) / h) + 1, h),
            # Only the most recent 20 days of 15m bars are stored.
            "15m": _bars(end_m - m * 96 * 20, 96 * 20 + 1, m),
        }
        run = self._run(
            trend_mode=AnalysisRun.Mode.HYBRID,
            extra=[("_build_hybrid_gate", {"side_effect": real_gate})],
        )
        self.assertEqual(run.summary["hybrid_coverage"], {"coins": 2, "covered": 0})
        for row in CoinAnalysis.objects.filter(analysis=run):
            self.assertFalse(row.eligible)
            self.assertTrue(row.exclusion_reason.startswith("hybrid trend data unavailable"))

    def test_normal_run_syncs_base_interval_only(self):
        self._run(trend_mode=AnalysisRun.Mode.NORMAL)
        self.assertEqual(self.sync.call_count, 2)
        self.sync_window.assert_not_called()

    def test_gate_sync_failure_is_a_data_error(self):
        run = self._run(
            trend_mode=AnalysisRun.Mode.HYBRID,
            extra=[("_sync_window", {"side_effect": RuntimeError("15m down")})],
        )
        self.assertEqual(run.status, AnalysisRun.Status.SUCCEEDED)
        self.assertEqual(run.summary["data_errors"], 4)
        self.assertIn("15m down", run.summary["data_error_samples"][0]["error"])

    def test_hybrid_coverage_summary(self):
        self.gates = {"AAAUSDT": self.gate}
        run = self._run(trend_mode=AnalysisRun.Mode.HYBRID)
        self.assertEqual(run.summary["hybrid_coverage"], {"coins": 2, "covered": 1})
        bbb = CoinAnalysis.objects.get(analysis=run, coin__symbol="BBB")
        self.assertFalse(bbb.eligible)
        # Scoring appends its own reasons (the stub history is short).
        self.assertTrue(bbb.exclusion_reason.startswith("hybrid trend data unavailable"))

    def test_drives_sessions_recorded(self):
        for flag in (True, False):
            run = self._run(trend_mode=AnalysisRun.Mode.HYBRID, drives_sessions=flag)
            run.refresh_from_db()
            self.assertIs(run.drives_sessions, flag)
        self.assertFalse(self._run().drives_sessions)

    def test_comparison_reuses_regime_timeline(self):
        row = StrategySettings.load()
        row.btc_regime_enabled = True
        row.save()
        timeline = RegimeTimeline(changes=[(self.rows[0]["time"], "normal")], readings=1)
        build = MagicMock(return_value=timeline)
        extra = [
            ("latest_candle_time", {"return_value": self.rows[-1]["time"]}),
            ("build_regime_timeline", {"new": build}),
        ]
        # Active mode first (hybrid), then the normal comparison.
        self._run(trend_mode=AnalysisRun.Mode.HYBRID, drives_sessions=True, extra=extra)
        self._run(trend_mode=AnalysisRun.Mode.NORMAL, extra=extra)
        self.assertEqual(build.call_count, 1)


from apps.engine.models import DecisionLog
from apps.jobs.handlers import handle_daily_analysis
from apps.jobs.models import JobRun
from apps.trading.models import LiveTradingState


class DailyJobTests(TestCase):
    def setUp(self):
        self.calls = []
        self.statuses = {}
        self.on_first_call = None

    def _fake(self, **kwargs):
        mode = kwargs.get("trend_mode", AnalysisRun.Mode.NORMAL)
        self.calls.append((mode, kwargs.get("drives_sessions", False)))
        if len(self.calls) == 1 and self.on_first_call:
            self.on_first_call()
        status = self.statuses.get(mode, AnalysisRun.Status.SUCCEEDED)
        return SimpleNamespace(
            id=f"{mode}-run",
            Status=AnalysisRun.Status,
            status=status,
            summary={"mode": mode},
            duration_seconds=1.0,
            error="boom" if status == AnalysisRun.Status.FAILED else "",
        )

    def _run(self, *, mode="normal", trigger=JobRun.Trigger.SCHEDULED):
        state = LiveTradingState.load()
        state.mode = mode
        state.save()
        job = JobRun.objects.create(job_type="daily_analysis", trigger=trigger)
        with patch("apps.jobs.handlers.run_daily_analysis", side_effect=self._fake):
            return handle_daily_analysis({}, job)

    def test_hybrid_mode_runs_hybrid_first(self):
        result = self._run(mode="hybrid")
        self.assertEqual(self.calls, [("hybrid", True), ("normal", False)])
        self.assertEqual(result["mode"], "hybrid")
        self.assertEqual(result["analysis_id"], "hybrid-run")
        self.assertEqual(result["comparison"]["mode"], "normal")
        self.assertNotIn("hybrid", result)

    def test_normal_mode_runs_normal_first(self):
        result = self._run(mode="normal")
        self.assertEqual(self.calls, [("normal", True), ("hybrid", False)])
        self.assertEqual(result["mode"], "normal")
        self.assertEqual(result["comparison"]["analysis_id"], "hybrid-run")

    def test_comparison_off_runs_once(self):
        row = StrategySettings.load()
        row.run_comparison_analysis = False
        row.save()
        result = self._run(mode="normal")
        self.assertEqual(self.calls, [("normal", True)])
        self.assertEqual(result["comparison"], {"skipped": True})

    def test_failed_comparison_keeps_job_succeeded(self):
        self.statuses["hybrid"] = AnalysisRun.Status.FAILED
        result = self._run(mode="normal")
        self.assertEqual(result["comparison"]["error"], "boom")
        self.assertEqual(result["comparison"]["mode"], "hybrid")
        logs = DecisionLog.objects.filter(severity="warning")
        self.assertEqual(logs.count(), 1)
        self.assertIn("Comparison hybrid analysis failed", logs.first().message)

    def test_raising_comparison_keeps_job_succeeded(self):
        def fake(**kwargs):
            if kwargs.get("trend_mode") == AnalysisRun.Mode.HYBRID:
                raise ValueError("kaput")
            return self._fake(**kwargs)

        job = JobRun.objects.create(job_type="daily_analysis")
        with patch("apps.jobs.handlers.run_daily_analysis", side_effect=fake):
            result = handle_daily_analysis({}, job)
        self.assertEqual(result["comparison"], {"mode": "hybrid", "error": "kaput"})

    def test_failed_primary_fails_job(self):
        self.statuses["normal"] = AnalysisRun.Status.FAILED
        with self.assertRaises(RuntimeError):
            self._run(mode="normal")
        self.assertEqual(self.calls, [("normal", True)])

    def test_mode_read_once_at_job_start(self):
        def flip():
            LiveTradingState.objects.filter(pk=1).update(mode="normal")

        self.on_first_call = flip
        self._run(mode="hybrid")
        self.assertEqual(self.calls, [("hybrid", True), ("normal", False)])

    def test_manual_trigger_uses_active_mode(self):
        self._run(mode="hybrid", trigger=JobRun.Trigger.MANUAL)
        self.assertEqual(self.calls[0], ("hybrid", True))


class CurrentRunTests(TestCase):
    def setUp(self):
        from apps.coins.models import Coin
        from apps.engine.models import CoinAnalysis

        self.normal = AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED, scope=AnalysisRun.Scope.UNIVERSE
        )
        self.hybrid = AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED,
            scope=AnalysisRun.Scope.UNIVERSE,
            mode=AnalysisRun.Mode.HYBRID,
        )
        for run, sym in ((self.normal, "NRMUSDT"), (self.hybrid, "HYBUSDT")):
            coin = Coin.objects.create(coingecko_id=sym.lower(), symbol=sym, name=sym)
            CoinAnalysis.objects.create(analysis=run, coin=coin)

    def _set_mode(self, mode):
        LiveTradingState.load()
        LiveTradingState.objects.filter(pk=1).update(mode=mode)

    def test_latest_analysis_follows_active_mode(self):
        from apps.engine.analysis import latest_analysis

        self._set_mode("normal")
        self.assertEqual(latest_analysis().id, self.normal.id)
        self._set_mode("hybrid")
        self.assertEqual(latest_analysis().id, self.hybrid.id)
        self.assertEqual(latest_analysis(mode="normal").id, self.normal.id)
        self.assertEqual(latest_analysis(mode="hybrid").id, self.hybrid.id)
        self.assertEqual(latest_analysis(mode="").id, self.hybrid.id)

    def test_results_default_follows_active_mode(self):
        self._set_mode("hybrid")
        client = APIClient()
        client.credentials(HTTP_X_ENGINE_TOKEN="t")
        with override_settings(ENGINE_API_TOKEN="t"):
            data = client.get("/api/engine/results/").json()
        rows = data["results"] if isinstance(data, dict) else data
        self.assertEqual([r["coin"]["symbol"] for r in rows], ["HYBUSDT"])

    def test_dashboard_comparison_is_inactive_mode(self):
        self._set_mode("hybrid")
        client = APIClient()
        client.credentials(HTTP_X_ENGINE_TOKEN="t")
        with override_settings(ENGINE_API_TOKEN="t"):
            engine = client.get("/api/engine/dashboard/").json()["engine"]
        self.assertEqual(engine["latest_analysis"]["mode"], "hybrid")
        self.assertEqual(engine["latest_comparison_analysis"]["mode"], "normal")
        self.assertNotIn("latest_hybrid_analysis", engine)

    def test_validation_history_follows_active_mode(self):
        client = APIClient()
        client.credentials(HTTP_X_ENGINE_TOKEN="t")
        for mode, run in (("hybrid", self.hybrid), ("normal", self.normal)):
            self._set_mode(mode)
            with override_settings(ENGINE_API_TOKEN="t"):
                body = client.get("/api/strategy/validation/").json()
            self.assertEqual(body["analysis"]["id"], str(run.id))
            self.assertEqual([row["analysis"] for row in body["ic_history"]], [str(run.id)])

    def test_dashboard_comparison_when_only_other_mode_ran(self):
        AnalysisRun.objects.all().delete()
        AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED,
            scope=AnalysisRun.Scope.UNIVERSE,
            mode=AnalysisRun.Mode.HYBRID,
        )
        self._set_mode("normal")
        client = APIClient()
        client.credentials(HTTP_X_ENGINE_TOKEN="t")
        with override_settings(ENGINE_API_TOKEN="t"):
            engine = client.get("/api/engine/dashboard/").json()["engine"]
        # latest_analysis never falls back across modes, so the primary is empty
        # and the other mode's run is shown as the comparison.
        self.assertIsNone(engine["latest_analysis"])
        self.assertEqual(engine["latest_comparison_analysis"]["mode"], "hybrid")


class ModeSwitchTests(TestCase):
    def setUp(self):
        LiveTradingState.load()
        self.client = APIClient()
        self.client.credentials(HTTP_X_ENGINE_TOKEN="t")

    def _run(self, mode, age_minutes, *, drives=False, summary=None):
        if summary is None:
            # A hybrid run made since the full-window change records its coverage.
            summary = {"hybrid_coverage": {"coins": 2, "covered": 2}} if mode == "hybrid" else {}
        return AnalysisRun.objects.create(
            status=AnalysisRun.Status.SUCCEEDED,
            scope=AnalysisRun.Scope.UNIVERSE,
            mode=mode,
            drives_sessions=drives,
            summary=summary,
            started_at=timezone.now() - timedelta(minutes=age_minutes),
        )

    def _get(self):
        with override_settings(ENGINE_API_TOKEN="t"):
            return self.client.get("/api/live/status/mode/").json()

    def _post(self, body):
        with override_settings(ENGINE_API_TOKEN="t"):
            return self.client.post("/api/live/status/mode/", body, format="json")

    def _set_mode(self, mode):
        LiveTradingState.objects.filter(pk=1).update(mode=mode)

    def test_stale_when_no_run_for_mode(self):
        self._run("normal", 5)
        data = self._get()
        self.assertEqual(data["mode"], "normal")
        self.assertFalse(data["analysis_stale"])
        self._set_mode("hybrid")
        data = self._get()
        self.assertTrue(data["analysis_stale"])
        self.assertIsNone(data["latest_analysis"])

    def test_stale_when_older_than_other_modes_driving_run(self):
        # Comparison off: the hybrid ranking predates the last session-driving
        # normal run, so switching to hybrid shows it as stale.
        self._run("hybrid", 60)
        newer = self._run("normal", 5, drives=True)
        self._set_mode("hybrid")
        data = self._get()
        self.assertTrue(data["analysis_stale"])
        self.assertEqual(data["latest_analysis"]["mode"], "hybrid")
        self.assertNotEqual(data["latest_analysis"]["id"], str(newer.id))

    def test_fresh_when_newer(self):
        self._run("normal", 60, drives=True)
        hybrid = self._run("hybrid", 5)
        self._set_mode("hybrid")
        data = self._get()
        self.assertFalse(data["analysis_stale"])
        self.assertEqual(data["latest_analysis"]["id"], str(hybrid.id))

    def test_primary_then_comparison_is_fresh_for_both_modes(self):
        # One job: the active-mode primary drives sessions, the other-mode
        # comparison runs after it.
        primary = self._run("hybrid", 10, drives=True)
        self._run("normal", 5)
        self._set_mode("hybrid")
        data = self._get()
        self.assertFalse(data["analysis_stale"])
        self.assertEqual(data["latest_analysis"]["id"], str(primary.id))
        # Switching to the comparison's mode: it is a full ranking, so fresh.
        self._set_mode("normal")
        self.assertFalse(self._get()["analysis_stale"])

    def test_comparison_off_switch_to_older_mode_is_stale(self):
        self._run("normal", 60 * 24 * 3)
        for age in (60 * 24 * 2, 60 * 24):
            self._run("hybrid", age, drives=True)
        self._set_mode("normal")
        self.assertTrue(self._get()["analysis_stale"])

    def test_legacy_hybrid_run_is_stale(self):
        self._run("normal", 60)
        self._run("hybrid", 5, drives=True, summary={"hybrid": {"lookback_days": 60}})
        self._set_mode("hybrid")
        self.assertTrue(self._get()["analysis_stale"])

    def test_reposting_same_mode_logs_nothing(self):
        self._set_mode("hybrid")
        before = LiveTradingState.load().updated_at
        response = self._post({"mode": "hybrid", "rerun_analysis": True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["purge"], {"sessions": 0, "positions": 0})
        self.assertFalse(DecisionLog.objects.filter(category="live").exists())
        self.assertEqual(LiveTradingState.load().updated_at, before)

    def test_same_mode_with_adx_still_logs(self):
        self._set_mode("hybrid")
        response = self._post({"mode": "hybrid", "adx_enabled": True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(DecisionLog.objects.filter(category="live").exists())

    @patch("apps.trading.views.enqueue_job")
    def test_rerun_enqueues_when_stale(self, enqueue):
        enqueue.return_value = JobRun.objects.create(job_type="daily_analysis")
        self._run("normal", 5)
        response = self._post({"mode": "hybrid", "confirm": True, "rerun_analysis": True})
        self.assertEqual(response.status_code, 200, response.content)
        enqueue.assert_called_once()
        self.assertEqual(enqueue.call_args.kwargs["params"], {})
        self.assertIn("job", response.json())

    @patch("apps.trading.views.enqueue_job")
    def test_rerun_skipped_when_fresh(self, enqueue):
        self._run("normal", 60, drives=True)
        self._run("hybrid", 5)
        response = self._post({"mode": "hybrid", "confirm": True, "rerun_analysis": True})
        self.assertEqual(response.status_code, 200, response.content)
        enqueue.assert_not_called()
        self.assertNotIn("job", response.json())

    def test_rerun_conflict_reported(self):
        self._run("normal", 5)
        JobRun.objects.create(job_type="daily_analysis", status=JobRun.Status.QUEUED)
        response = self._post({"mode": "hybrid", "confirm": True, "rerun_analysis": True})
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIn("job_conflict", response.json())
        self.assertEqual(LiveTradingState.load().mode, "hybrid")

    @patch("apps.trading.views.enqueue_job")
    def test_confirmation_409_never_enqueues(self, enqueue):
        self._run("normal", 5)
        response = self._post({"mode": "hybrid", "rerun_analysis": True})
        self.assertEqual(response.status_code, 409)
        enqueue.assert_not_called()


def _score_stub(results, **kwargs):
    """Rank every gated coin as prime with marked params."""

    rank = 0
    for item in results:
        gated = not item["exclusion_reason"].startswith("hybrid trend")
        item["eligible"] = gated
        item["suitability"] = "prime" if gated else "ineligible"
        if gated:
            rank += 1
            item["rank"] = rank
            item["score"] = 1.0
            item["exclusion_reason"] = ""
            item["params"] = {"grid_levels": 9, "source": f"hybrid-{item['coin'].symbol}"}
    return results


class HybridBootstrapTests(TestCase):
    """A universe hybrid run driving sessions through the real bootstrap."""

    setUp = RunBehaviourTests.setUp
    _run = RunBehaviourTests._run

    def _drive(self):
        real = analysis._bootstrap_paper_sessions
        with patch("apps.trading.paper.retire_stale_sessions", return_value=[]) as retire, patch(
            "apps.trading.paper.ensure_paper_sessions", return_value=[]
        ) as ensure:
            run = self._run(
                trend_mode=AnalysisRun.Mode.HYBRID,
                drives_sessions=True,
                extra=[
                    ("_bootstrap_paper_sessions", {"side_effect": real}),
                    ("score_results", {"side_effect": _score_stub}),
                ],
            )
        self.assertNotIn("paper_session_error", run.summary)
        return run, retire, ensure

    @staticmethod
    def _symbols(items):
        return [item["coin"].symbol for item in items]

    def test_universe_hybrid_run_feeds_its_candidates_to_sessions(self):
        run, retire, ensure = self._drive()
        self.assertEqual(run.scope, AnalysisRun.Scope.UNIVERSE)
        self.assertTrue(run.drives_sessions)
        retire.assert_called_once()
        self.assertEqual(retire.call_args.args[0].id, run.id)
        self.assertEqual(self._symbols(retire.call_args.args[1]), ["AAA", "BBB"])
        for item in retire.call_args.args[1]:
            self.assertEqual(item["params"]["source"], f"hybrid-{item['coin'].symbol}")
        ensure.assert_called_once()
        self.assertEqual(ensure.call_args.args[0].id, run.id)
        selected = ensure.call_args.args[1]
        self.assertEqual(self._symbols(selected), ["AAA", "BBB"])
        for item in selected:
            self.assertEqual(item["params"]["source"], f"hybrid-{item['coin'].symbol}")
            self.assertEqual(item["suitability"], "prime")

    def test_half_coverage_still_retires(self):
        self.gates = {"AAAUSDT": self.gate}
        run, retire, ensure = self._drive()
        self.assertEqual(run.summary["hybrid_coverage"], {"coins": 2, "covered": 1})
        retire.assert_called_once()
        self.assertEqual(self._symbols(retire.call_args.args[1]), ["AAA"])
        self.assertFalse(
            DecisionLog.objects.filter(message__contains="retirement skipped").exists()
        )

    def test_low_coverage_skips_retirement_but_opens_covered(self):
        Coin.objects.create(
            coingecko_id="ccc", symbol="CCC", name="CCC", rank=3,
            binance_symbol="CCCUSDT", analysis_eligible=True, in_top100=True,
        )
        self.gates = {"AAAUSDT": self.gate}
        run, retire, ensure = self._drive()
        self.assertEqual(run.summary["hybrid_coverage"], {"coins": 3, "covered": 1})
        retire.assert_not_called()
        self.assertEqual(self._symbols(ensure.call_args.args[1]), ["AAA"])
        log = DecisionLog.objects.get(message__contains="retirement skipped")
        self.assertEqual(log.category, "analysis")
        self.assertEqual(log.severity, "warning")
        self.assertEqual(log.payload["covered"], 1)
        self.assertEqual(log.payload["coins"], 3)
