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
        self.assertFalse(solo.drives_sessions)
        self.assertFalse(hyb.drives_sessions)
        self.assertFalse(old.drives_sessions)


from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

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

    def _run(self, *, extra=(), **kwargs):
        with ExitStack() as stack:
            def p(name, **kw):
                return stack.enter_context(patch.object(analysis, name, **kw))

            p("BinanceFuturesClient")
            self.sync = p("sync_coin_market_data", return_value={"ok": True})
            self.sync_window = p("_sync_window", return_value={"ok": True})
            p("load_candles", side_effect=lambda *a, **k: list(self.rows))
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
