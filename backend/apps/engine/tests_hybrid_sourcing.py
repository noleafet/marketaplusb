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
        got = analysis._build_hybrid_gate(
            "AAAUSDT", rows, lookback_days=180, adx_enabled=False)
        end = timezone.now()
        want = build_gate(
            rows,
            analysis._closed(h_rows, "1h", end),
            analysis._closed(m_rows, "15m", end),
            adx_enabled=False,
        )
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

        with patch("apps.engine.analysis.build_gate", fake):
            analysis._build_hybrid_gate("AAAUSDT", [], lookback_days=10, adx_enabled=False)
        end = timezone.now()
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
