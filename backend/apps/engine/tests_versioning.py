import os
from datetime import datetime, timedelta, timezone as dt_timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.db import IntegrityError
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.coins.models import Coin
from apps.engine.models import ConfigVersion, StrategyVersion
from apps.engine.versioning import (
    digest,
    resolve_config_version,
    resolve_strategy_version,
    strategy_snapshot,
)
from apps.trading.events import record_event, session_state, snapshot_session
from apps.engine.models import AnalysisRun
from apps.trading.execution import set_kill_switch
from apps.trading.models import GridEvent, PaperSession, StrategySettings
from apps.trading.paper import (
    ensure_paper_sessions,
    retire_stale_sessions,
    pause_session,
    resume_session,
    stop_session,
    tick_paper_sessions,
)
from apps.trading.strategy import IncrementalGridStrategy
from apps.jobs.handlers import handle_daily_analysis
from apps.jobs.models import JobRun

_NO_MATCH = MagicMock()
_NO_MATCH.first.return_value = None


class CanonicalHashTests(TestCase):
    def test_key_order_and_float_noise_do_not_change_hash(self):
        a = {"b": 0.1 + 0.2, "a": [1, {"y": 2, "x": 1}]}
        b = {"a": [1, {"x": 1, "y": 2}], "b": 0.3}
        self.assertEqual(digest(a), digest(b))

    def test_real_change_changes_hash(self):
        self.assertNotEqual(digest({"spacing": 0.01}), digest({"spacing": 0.011}))


class StrategyVersionTests(TestCase):
    def test_same_settings_same_version(self):
        self.assertEqual(resolve_strategy_version().id, resolve_strategy_version().id)
        self.assertEqual(StrategyVersion.objects.count(), 1)

    def test_changed_setting_creates_child_with_diff(self):
        v1 = resolve_strategy_version()
        row = StrategySettings.load()
        row.trade_through_bps = 5.0
        row.save()
        v2 = resolve_strategy_version(row)
        self.assertNotEqual(v1.id, v2.id)
        self.assertEqual(v2.parent_id, v1.id)
        self.assertEqual(v2.diff["trade_through_bps"], {"from": 2.0, "to": 5.0})

    def test_missing_git_sha_is_unknown(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GIT_SHA", None)
            self.assertEqual(resolve_strategy_version().code_sha, "unknown")

    def test_git_sha_is_part_of_identity(self):
        with patch.dict(os.environ, {"GIT_SHA": "abc"}):
            a = resolve_strategy_version()
        with patch.dict(os.environ, {"GIT_SHA": "def"}):
            b = resolve_strategy_version()
        self.assertNotEqual(a.id, b.id)

    def test_git_sha_only_change_records_code_sha_diff(self):
        with patch.dict(os.environ, {"GIT_SHA": "abc"}):
            a = resolve_strategy_version()
        with patch.dict(os.environ, {"GIT_SHA": "def"}):
            b = resolve_strategy_version()
        self.assertEqual(b.parent_id, a.id)
        self.assertEqual(b.diff["code_sha"], {"from": "abc", "to": "def"})

    def test_resolve_is_race_safe(self):
        # Lost race: the existence check misses, the other writer's row is
        # already committed, and our get_or_create hits the unique key.
        snap = strategy_snapshot()
        vid = digest({"snapshot": snap, "code_sha": "unknown"})
        StrategyVersion.objects.create(id=vid, snapshot=snap, code_sha="unknown")
        with patch.object(
            StrategyVersion.objects, "filter", return_value=_NO_MATCH
        ), patch(
            "django.db.models.query.QuerySet.get_or_create",
            side_effect=IntegrityError("duplicate key"),
        ):
            v = resolve_strategy_version(snapshot=snap, code_sha="unknown")
        self.assertEqual(v.id, vid)
        self.assertEqual(StrategyVersion.objects.count(), 1)
        self.assertEqual(v.snapshot, snap)


class ConfigVersionTests(TestCase):
    def test_same_params_same_version_per_symbol(self):
        p = {"grid_levels": 12, "spacing": 0.01}
        self.assertEqual(
            resolve_config_version("BTCUSDT", p).id,
            resolve_config_version("BTCUSDT", dict(p)).id,
        )
        self.assertNotEqual(
            resolve_config_version("BTCUSDT", p).id,
            resolve_config_version("ETHUSDT", p).id,
        )

    def test_resolve_is_race_safe(self):
        p = {"grid_levels": 12}
        vid = digest({"symbol": "BTCUSDT", "params": p})

        ConfigVersion.objects.create(id=vid, symbol="BTCUSDT", params=p)
        with patch.object(
            ConfigVersion.objects, "filter", return_value=_NO_MATCH
        ), patch(
            "django.db.models.query.QuerySet.get_or_create",
            side_effect=IntegrityError("duplicate key"),
        ):
            v = resolve_config_version("BTCUSDT", p)
        self.assertEqual(v.id, vid)
        self.assertEqual(ConfigVersion.objects.count(), 1)


class EventHelperTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(coingecko_id="btc", symbol="BTC", name="Bitcoin",
                                        binance_symbol="BTCUSDT")
        self.session = PaperSession.objects.create(coin=self.coin, position_qty=2,
                                                   avg_entry=100, mark_price=110)

    def test_session_state_unrealized_pnl(self):
        self.assertEqual(session_state(self.session)["unrealized_pnl"], 20.0)

    def test_record_event_defaults_symbol_and_versions_from_session(self):
        sv = resolve_strategy_version()
        self.session.strategy_version = sv
        self.session.save()
        ev = record_event(GridEvent.Type.PAUSE, session=self.session)
        self.assertEqual(ev.symbol, "BTCUSDT")
        self.assertEqual(ev.strategy_version_id, sv.id)

    def test_snapshot_records_state(self):
        snap = snapshot_session(self.session, "start")
        self.assertEqual(snap.state["equity"], self.session.equity)

    def test_session_state_grid_derives_spacing_from_levels(self):
        self.session.grid_state = {"levels": [121, 100, 110], "range_low": 100, "range_high": 121}
        grid = session_state(self.session)["grid"]
        self.assertEqual(grid["range_low"], 100)
        self.assertEqual(grid["range_high"], 121)
        self.assertEqual(grid["levels"], [121, 100, 110])
        self.assertAlmostEqual(grid["spacing"], 0.1)

    def test_session_state_grid_empty_has_no_spacing(self):
        grid = session_state(self.session)["grid"]
        self.assertIsNone(grid["spacing"])
        self.assertIsNone(grid["levels"])

    def test_session_state_spacing_none_for_nonpositive_level(self):
        self.session.grid_state = {"levels": [0, 10]}
        self.assertIsNone(session_state(self.session)["grid"]["spacing"])

    def test_record_event_explicit_values_override_session(self):
        sv = resolve_strategy_version()
        cv = resolve_config_version("BTCUSDT", {"a": 1})
        self.session.strategy_version = resolve_strategy_version(snapshot={"x": 1}, code_sha="z")
        self.session.save()
        ev = record_event(GridEvent.Type.STOP, session=self.session, symbol="ETHUSDT",
                          strategy_version=sv, config_version=cv, environment="live")
        ev.refresh_from_db()
        self.assertEqual(ev.environment, "live")
        self.assertEqual(ev.symbol, "ETHUSDT")
        self.assertEqual(ev.strategy_version_id, sv.id)
        self.assertEqual(ev.config_version_id, cv.id)
        self.assertEqual(ev.before, {})
        self.assertEqual(ev.after, {})

    def test_snapshot_stamps_versions_and_state_round_trips(self):
        sv = resolve_strategy_version()
        cv = resolve_config_version("BTCUSDT", {"a": 1})
        self.session.strategy_version = sv
        self.session.config_version = cv
        self.session.save()
        snap = snapshot_session(self.session, "daily")
        snap.refresh_from_db()
        self.assertEqual(snap.strategy_version_id, sv.id)
        self.assertEqual(snap.config_version_id, cv.id)
        self.assertEqual(snap.trigger, "daily")
        self.assertEqual(snap.state, session_state(self.session))
        self.assertEqual(snap.state["unrealized_pnl"], 20.0)


def _hist(count=180):
    now = datetime.now(dt_timezone.utc)
    rows = []
    for index in range(count):
        price = 100.0 + 3.0 * ((index % 12) - 6) / 6.0
        rows.append({
            "time": now - timedelta(hours=4 * (count - index)),
            "open": price, "high": price * 1.004, "low": price * 0.996, "close": price,
        })
    return rows


class CaptureTests(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(coingecko_id="btc", symbol="BTC", name="Bitcoin",
                                        binance_symbol="BTCUSDT", analysis_eligible=True)

    def _post_settings(self, body):
        client = APIClient()
        client.credentials(HTTP_X_ENGINE_TOKEN="t")
        with override_settings(ENGINE_API_TOKEN="t"):
            return client.post("/api/strategy/settings/", body, format="json")

    def _open_session(self):
        run = AnalysisRun.objects.create(strategy_version=resolve_strategy_version())
        created = ensure_paper_sessions(run, [{"coin": self.coin, "params": {"grid_levels": 10}}])
        self.assertEqual(len(created), 1)
        return created[0]

    def test_settings_change_writes_param_change_event(self):
        self._post_settings({"trade_through_bps": 7.0})
        ev = GridEvent.objects.get(type="param_change")
        self.assertEqual(ev.after["trade_through_bps"], 7.0)
        self.assertEqual(ev.before["trade_through_bps"], 2.0)
        self.assertNotIn("code_sha", ev.after)
        self.assertIsNotNone(ev.strategy_version_id)

    def test_settings_save_without_change_writes_no_event(self):
        self._post_settings({"trade_through_bps": 7.0})
        self._post_settings({"trade_through_bps": 7.0})
        self.assertEqual(GridEvent.objects.filter(type="param_change").count(), 1)

    def test_new_session_is_stamped_and_snapshotted(self):
        s = self._open_session()
        self.assertIsNotNone(s.strategy_version_id)
        self.assertIsNotNone(s.config_version_id)
        self.assertEqual(s.config_version.symbol, "BTCUSDT")
        self.assertTrue(s.snapshots.filter(trigger="start").exists())

    @override_settings(PAPER={**settings.PAPER, "EVAL_INTERVAL_HOURS": 0})
    @patch("apps.trading.paper.insert_equity_points", return_value=1)
    @patch("apps.trading.paper._fetch_hist")
    @patch("apps.trading.paper._fetch_price", return_value=100.0)
    @patch("apps.trading.paper.BinanceFuturesClient")
    def test_rebalance_recorded_after_save_and_fills_stamped(self, _c, _p, fetch_hist, _e):
        hist = _hist()
        fetch_hist.return_value = hist
        s = self._open_session()
        tick_paper_sessions(session_ids=[str(s.id)])
        self.assertEqual(GridEvent.objects.filter(type="rebalance").count(), 0)

        real_step = IncrementalGridStrategy.step

        def step(self_, bar, history, **kw):
            result = real_step(self_, bar, history, **kw)
            self_.rebalances += 1
            self_.last_rebalance_reason = "range_breakout"
            return result

        fetch_hist.return_value = hist + [{
            "time": datetime.now(dt_timezone.utc) + timedelta(minutes=1), "open": 100.0, "high": 110.0,
            "low": 90.0, "close": 100.0,
        }]
        with patch.object(IncrementalGridStrategy, "step", step):
            tick_paper_sessions(session_ids=[str(s.id)])
        ev = GridEvent.objects.get(type="rebalance")
        self.assertEqual(ev.reason, "range_breakout")
        self.assertIn("range_low", ev.after)
        self.assertIn("active_levels", ev.after)
        s.refresh_from_db()
        snap = s.snapshots.get(trigger="rebalance")
        self.assertEqual(snap.state["grid"]["range_low"], s.grid_state["range_low"])
        self.assertTrue(s.fills.exists())
        for f in s.fills.all():
            self.assertEqual(f.config_version_id, s.config_version_id)
            self.assertEqual(f.strategy_version_id, s.strategy_version_id)

    def test_pause_resume_stop_emit_events(self):
        s = self._open_session()
        pause_session(s)
        resume_session(s)
        stop_session(s, "manual_stop")
        self.assertEqual(
            list(GridEvent.objects.order_by("time", "id").values_list("type", flat=True)),
            ["pause", "resume", "stop"],
        )

    def test_pause_on_paused_session_emits_nothing(self):
        s = self._open_session()
        pause_session(s)
        pause_session(s)
        self.assertEqual(GridEvent.objects.filter(type="pause").count(), 1)

    def test_kill_switch_event_is_live(self):
        set_kill_switch(True, "test")
        ev = GridEvent.objects.get()
        self.assertEqual(ev.environment, "live")
        self.assertEqual(ev.type, "kill_switch")
        self.assertEqual(ev.after, {"engaged": True})

    def test_retire_records_retire_event_snapshot_and_stamped_fills(self):
        sv = resolve_strategy_version()
        cv = resolve_config_version("BTCUSDT", {})
        s = PaperSession.objects.create(
            coin=self.coin, position_qty=2.0, avg_entry=100.0, mark_price=110.0,
            capital=1000.0, equity=1000.0, peak_equity=1000.0, failed_runs=99,
            strategy_version=sv, config_version=cv,
        )
        retired = retire_stale_sessions(
            AnalysisRun.objects.create(), [], StrategySettings.load()
        )
        self.assertEqual(retired, ["BTC"])
        ev = GridEvent.objects.get(type="retire")
        self.assertIn("retired", ev.reason)
        self.assertEqual(ev.session_id, s.id)
        self.assertEqual(GridEvent.objects.filter(type="stop").count(), 0)
        self.assertEqual(s.snapshots.filter(trigger="retire").count(), 1)
        self.assertTrue(s.fills.exists())
        for f in s.fills.all():
            self.assertEqual(f.strategy_version_id, sv.id)
            self.assertEqual(f.config_version_id, cv.id)

    def _fake_analysis(self, hybrid_ok=True):
        def fake(**kwargs):
            normal = kwargs.get("trend_mode", AnalysisRun.Mode.NORMAL) == AnalysisRun.Mode.NORMAL
            ok = normal or hybrid_ok
            return SimpleNamespace(
                id="run", Status=AnalysisRun.Status,
                status=AnalysisRun.Status.SUCCEEDED if ok else AnalysisRun.Status.FAILED,
                summary={}, duration_seconds=1.0, error="" if ok else "boom",
            )
        return fake

    def _three_sessions(self):
        for sym, status in (("AAA", "active"), ("BBB", "paused"), ("CCC", "stopped")):
            coin = Coin.objects.create(coingecko_id=sym.lower(), symbol=sym, name=sym,
                                       binance_symbol=sym + "USDT")
            PaperSession.objects.create(coin=coin, status=status)

    @patch("apps.jobs.handlers.run_daily_analysis")
    def test_daily_job_snapshots_active_and_paused_only(self, run_daily):
        from apps.trading.models import SessionSnapshot
        self._three_sessions()
        run_daily.side_effect = self._fake_analysis()
        handle_daily_analysis({}, JobRun.objects.create(job_type="daily_analysis"))
        snaps = SessionSnapshot.objects.filter(trigger="daily")
        self.assertEqual(snaps.count(), 2)
        self.assertEqual(
            sorted(snaps.values_list("session__coin__symbol", flat=True)), ["AAA", "BBB"]
        )

    @patch("apps.jobs.handlers.run_daily_analysis")
    def test_daily_job_hybrid_failure_writes_no_daily_snapshots(self, run_daily):
        from apps.trading.models import SessionSnapshot
        self._three_sessions()
        run_daily.side_effect = self._fake_analysis(hybrid_ok=False)
        with self.assertRaises(RuntimeError):
            handle_daily_analysis({}, JobRun.objects.create(job_type="daily_analysis"))
        self.assertEqual(SessionSnapshot.objects.filter(trigger="daily").count(), 0)
