from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch

from django.conf import settings
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.engine.models import AnalysisRun
from apps.trading.models import PaperSession, StrategySettings
from apps.trading.paper import (
    ensure_paper_sessions,
    retire_stale_sessions,
    run_risk_guard,
    tick_paper_sessions,
)
from apps.trading.portfolio import buy_block_reason, refresh_book_state
from apps.trading.tests import make_coin, make_session

TOKEN = "test-engine-token"


def make_analysis() -> AnalysisRun:
    return AnalysisRun.objects.create(status=AnalysisRun.Status.SUCCEEDED)


class StrategySettingsTests(TestCase):
    def test_defaults_match_the_agreed_plan(self):
        row = StrategySettings.load()
        self.assertEqual(row.signal_interval, "1h")
        self.assertEqual(row.fill_model, "trade_through")
        self.assertTrue(row.overbought_guard_enabled)
        self.assertEqual(row.holdout_days, 90)
        self.assertEqual(row.book_capital, 8000.0)
        self.assertEqual(row.max_sessions, 8)
        self.assertAlmostEqual(row.session_capital, 1000.0)
        self.assertAlmostEqual(row.daily_loss_limit_pct, 0.03)
        self.assertEqual(row.retire_after_failed_runs, 2)

    def test_touch_model_disables_trade_through(self):
        row = StrategySettings.load()
        row.fill_model = StrategySettings.FillModel.TOUCH
        self.assertEqual(row.effective_trade_through_bps, 0.0)


@override_settings(ENGINE_API_TOKEN=TOKEN, ENGINE_ALLOW_PUBLIC_READS=True)
class StrategySettingsApiTests(TestCase):
    def setUp(self):
        self.client = APIClient()

    def test_read_is_public_and_update_needs_token(self):
        response = self.client.get("/api/strategy/settings/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["signal_interval"], "1h")

        denied = self.client.post(
            "/api/strategy/settings/", {"signal_interval": "4h"}, format="json"
        )
        self.assertEqual(denied.status_code, 403)

        allowed = self.client.post(
            "/api/strategy/settings/",
            {"signal_interval": "4h", "max_sessions": 4, "book_capital": 4000},
            format="json",
            HTTP_X_ENGINE_TOKEN=TOKEN,
        )
        self.assertEqual(allowed.status_code, 200, allowed.content)
        body = allowed.json()
        self.assertEqual(body["signal_interval"], "4h")
        self.assertAlmostEqual(body["session_capital"], 1000.0)

    def test_rejects_out_of_range_values(self):
        response = self.client.post(
            "/api/strategy/settings/",
            {"daily_loss_limit_pct": 0.9, "holdout_days": 5},
            format="json",
            HTTP_X_ENGINE_TOKEN=TOKEN,
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("daily_loss_limit_pct", response.json())
        self.assertIn("holdout_days", response.json())

    def test_validation_endpoint_returns_structure_without_runs(self):
        response = self.client.get("/api/strategy/validation/")
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertIsNone(body["analysis"])
        self.assertEqual(body["findings"], [])
        self.assertIn("paper_correlation", body)


class BookRiskTests(TestCase):
    def test_daily_loss_limit_latches_a_buy_halt(self):
        row = StrategySettings.load()
        session = make_session(make_coin())
        now = datetime(2026, 1, 5, 12, tzinfo=dt_timezone.utc)
        state = refresh_book_state(row, now)
        self.assertFalse(state["halted"])
        self.assertEqual(buy_block_reason(state, row), "")

        # 3% of the 8,000 book is 240.
        session.equity = 1000.0 - 250.0
        session.save()
        state = refresh_book_state(row, now + timedelta(hours=1))
        self.assertTrue(state["halted"])
        self.assertEqual(buy_block_reason(state, row), "daily_loss_halt")

        # A recovery the same day does not lift the halt; the next day does.
        session.equity = 1000.0
        session.save()
        self.assertTrue(refresh_book_state(row, now + timedelta(hours=2))["halted"])
        self.assertFalse(refresh_book_state(row, now + timedelta(days=1))["halted"])

    def test_exposure_cap_blocks_buys(self):
        row = StrategySettings.load()
        row.max_book_exposure = 0.1
        row.save()
        make_session(make_coin(), position_qty=10.0, mark_price=100.0)
        state = refresh_book_state(row, datetime.now(dt_timezone.utc))
        self.assertAlmostEqual(state["exposure"], 1000.0 / 8000.0)
        self.assertEqual(buy_block_reason(state, row), "book_exposure_cap")


class SessionAllocationTests(TestCase):
    def test_new_sessions_use_book_sizing_and_respect_the_cap(self):
        row = StrategySettings.load()
        row.max_sessions = 2
        row.book_capital = 3000.0
        row.save()
        analysis = make_analysis()
        coins = [make_coin(symbol, f"{symbol}USDT") for symbol in ("AAA", "BBB", "CCC")]
        created = ensure_paper_sessions(analysis, [{"coin": coin} for coin in coins])
        self.assertEqual(len(created), 2)
        for session in created:
            self.assertAlmostEqual(session.capital, 1500.0)
            self.assertEqual(session.config["interval"], "1h")

    def test_sessions_retire_after_consecutive_failed_runs(self):
        row = StrategySettings.load()
        keep = make_session(make_coin("KEEP", "KEEPUSDT"))
        drop = make_session(
            make_coin("DROP", "DROPUSDT"),
            position_qty=2.0,
            avg_entry=100.0,
            mark_price=110.0,
        )
        qualifying = [{"coin": keep.coin}]

        self.assertEqual(retire_stale_sessions(make_analysis(), qualifying, row), [])
        drop.refresh_from_db()
        self.assertEqual(drop.failed_runs, 1)
        self.assertEqual(drop.status, PaperSession.Status.ACTIVE)

        self.assertEqual(retire_stale_sessions(make_analysis(), qualifying, row), ["DROP"])
        drop.refresh_from_db()
        keep.refresh_from_db()
        self.assertEqual(drop.status, PaperSession.Status.STOPPED)
        self.assertEqual(drop.position_qty, 0.0)
        self.assertIn("retired", drop.stop_reason)
        self.assertEqual(keep.status, PaperSession.Status.ACTIVE)
        self.assertEqual(keep.failed_runs, 0)


class OverboughtGuardSwitchTests(TestCase):
    def test_disabled_guard_resumes_its_paused_sessions(self):
        row = StrategySettings.load()
        row.overbought_guard_enabled = False
        row.save()
        session = make_session(
            make_coin(),
            status=PaperSession.Status.PAUSED,
            risk_state={"phase": "paused"},
        )
        manual = make_session(
            make_coin("ETH", "ETHUSDT"), status=PaperSession.Status.PAUSED
        )
        summary = run_risk_guard()
        self.assertFalse(summary["enabled"])
        self.assertEqual(summary["resumed"], 1)
        session.refresh_from_db()
        manual.refresh_from_db()
        self.assertEqual(session.status, PaperSession.Status.ACTIVE)
        # A manual pause is the operator's call and stays paused.
        self.assertEqual(manual.status, PaperSession.Status.PAUSED)


def hourly_hist(now: datetime, count: int = 24 * 40) -> list[dict]:
    end = now.replace(minute=0, second=0, microsecond=0)
    rows = []
    for index in range(count):
        moment = end - timedelta(hours=count - index)
        price = 100.0 + 2.0 * ((index % 10) - 5) / 5.0
        rows.append(
            {
                "time": moment,
                "open": price,
                "high": price * 1.01,
                "low": price * 0.99,
                "close": price,
            }
        )
    return rows


class BearishLockTests(TestCase):
    @override_settings(PAPER={**settings.PAPER, "EVAL_INTERVAL_HOURS": 0})
    @patch("apps.trading.paper.insert_equity_points", return_value=1)
    @patch("apps.trading.paper._fetch_hist")
    @patch("apps.trading.paper._fetch_price", return_value=100.0)
    @patch("apps.trading.paper.BinanceFuturesClient")
    def test_locked_session_never_fills(self, client_cls, _price, fetch_hist, _pts):
        client_cls.return_value.funding_rates.return_value = []
        now = datetime.now(dt_timezone.utc)
        hist = hourly_hist(now)
        fetch_hist.return_value = hist[:-48]
        session = make_session(make_coin())
        tick_paper_sessions(session_ids=[str(session.id)])
        session.refresh_from_db()
        self.assertTrue(session.grid_state["levels"])

        session.bias = PaperSession.Bias.BEARISH
        session.save()
        # 48 new oscillating bars that would cross many rungs.
        fetch_hist.return_value = hist
        state = dict(session.grid_state)
        state["last_bar_time"] = hist[-49]["time"].isoformat()
        session.grid_state = state
        session.save()

        tick_paper_sessions(session_ids=[str(session.id)])
        session.refresh_from_db()
        self.assertEqual(session.fill_count, 0)
        self.assertFalse(session.grid_state["orders"])
