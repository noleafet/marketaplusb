import uuid
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.coins.models import Coin
from apps.engine import timescale
from apps.engine.models import ConfigVersion, StrategyVersion
from apps.etl_api.keys import create_client
from apps.etl_api.serializers import METRIC_FIELDS
from apps.trading.models import (
    GridEvent,
    LiveOrderAudit,
    PaperEvaluation,
    PaperFill,
    PaperSession,
    SessionSnapshot,
)

SESSIONS = "/api/v1/sessions/"
FILLS = "/api/v1/fills/"
EVENTS = "/api/v1/events/"
AUDIT = "/api/v1/live/order-audit/"


class Base(TestCase):
    def setUp(self):
        _, key = create_client("t")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        self.now = timezone.now()
        self.past = self.now - timedelta(minutes=10)
        self.sv = StrategyVersion.objects.create(id="sv1", snapshot={}, code_sha="a")
        self.sv2 = StrategyVersion.objects.create(id="sv2", snapshot={}, code_sha="b")
        self.cv = ConfigVersion.objects.create(id="cv1", symbol="BTCUSDT", params={})
        self.coin = Coin.objects.create(
            coingecko_id="b", symbol="BTC", name="B", binance_symbol="BTCUSDT"
        )
        self.eth = Coin.objects.create(
            coingecko_id="e", symbol="ETH", name="E", binance_symbol="ETHUSDT"
        )

    def session(self, coin=None, updated_at=None, **kw):
        s = PaperSession.objects.create(
            coin=coin or self.coin,
            strategy_version=kw.pop("strategy_version", self.sv),
            config_version=kw.pop("config_version", self.cv),
            **kw,
        )
        PaperSession.objects.filter(pk=s.pk).update(updated_at=updated_at or self.past)
        s.refresh_from_db()
        return s

    def fill(self, s, time=None, created_at=None, **kw):
        return PaperFill.objects.create(
            session=s,
            time=time or self.past,
            created_at=created_at or self.past,
            side="buy",
            price=100.0,
            qty=1.0,
            notional=100.0,
            strategy_version=kw.pop("strategy_version", self.sv),
            config_version=self.cv,
            **kw,
        )


class SessionTests(Base):
    def test_session_unrealized_pnl_and_versions(self):
        s = self.session(
            position_qty=2.0,
            avg_entry=100.0,
            mark_price=110.0,
            grid_state={"range_low": 90, "range_high": 120, "levels": [100, 110]},
        )
        r = self.api.get(f"{SESSIONS}{s.pk}/")
        self.assertEqual(r.status_code, 200)
        d = r.json()["data"]
        self.assertEqual(d["id"], str(s.pk))
        self.assertEqual(d["environment"], "paper")
        self.assertEqual(d["symbol"], "BTCUSDT")
        self.assertEqual(d["unrealized_pnl"], 20.0)
        self.assertEqual(d["strategy_version"], "sv1")
        self.assertEqual(d["config_version"], "cv1")
        self.assertEqual(d["grid"]["range_low"], 90)
        self.assertAlmostEqual(d["grid"]["spacing"], 0.1)
        self.assertTrue(d["started_at"].endswith("Z"))
        self.assertEqual(r.json()["completeness"], {"is_partial": True})

    def test_stopped_session_not_partial(self):
        s = self.session(status="stopped")
        r = self.api.get(f"{SESSIONS}{s.pk}/")
        self.assertEqual(r.json()["completeness"], {"is_partial": False})

    def test_odd_grid_state_does_not_500(self):
        s = self.session(grid_state=[1, 2])
        self.assertEqual(self.api.get(f"{SESSIONS}{s.pk}/").status_code, 200)
        s2 = self.session(grid_state={"levels": ["a", "b"]})
        self.assertEqual(self.api.get(f"{SESSIONS}{s2.pk}/").status_code, 200)

    def test_session_list_incremental_by_updated_at(self):
        old = self.session(updated_at=self.now - timedelta(hours=2))
        new = self.session(coin=self.eth, updated_at=self.now - timedelta(minutes=1))
        since = (self.now - timedelta(minutes=5)).isoformat()
        r = self.api.get(SESSIONS, {"updated_since": since})
        ids = [x["id"] for x in r.json()["data"]]
        self.assertEqual(ids, [str(new.pk)])
        self.assertEqual(r.json()["completeness"], {"active_ids": [str(new.pk)]})
        r = self.api.get(SESSIONS)
        self.assertEqual([x["id"] for x in r.json()["data"]], [str(old.pk), str(new.pk)])

    def test_session_list_paging_and_filters(self):
        a = self.session(updated_at=self.now - timedelta(minutes=30))
        b = self.session(coin=self.eth, updated_at=self.now - timedelta(minutes=20))
        r = self.api.get(SESSIONS, {"page_size": 1})
        self.assertEqual([x["id"] for x in r.json()["data"]], [str(a.pk)])
        cur = r.json()["meta"]["next_cursor"]
        self.assertTrue(cur)
        r = self.api.get(SESSIONS, {"page_size": 1, "cursor": cur})
        self.assertEqual([x["id"] for x in r.json()["data"]], [str(b.pk)])
        r = self.api.get(SESSIONS, {"symbol": "ETHUSDT"})
        self.assertEqual([x["id"] for x in r.json()["data"]], [str(b.pk)])
        r = self.api.get(SESSIONS, {"strategy_version": "sv2"})
        self.assertEqual(r.json()["data"], [])

    def test_unknown_session_404_problem(self):
        u = uuid.uuid4()
        for suffix in ("", "snapshots/", "equity/", "evaluations/", "fills/"):
            r = self.api.get(f"{SESSIONS}{u}/{suffix}")
            self.assertEqual(r.status_code, 404, suffix)
            self.assertEqual(r.json()["type"], "https://marketaplusb/errors/not_found")

    def test_live_environment_empty_and_partial(self):
        s = self.session()
        self.fill(s)
        for url in (SESSIONS, FILLS, f"{SESSIONS}{s.pk}/snapshots/", f"{SESSIONS}{s.pk}/equity/",
                    f"{SESSIONS}{s.pk}/evaluations/", f"{SESSIONS}{s.pk}/fills/"):
            r = self.api.get(url, {"environment": "live"})
            self.assertEqual(r.status_code, 200, url)
            self.assertEqual(r.json()["data"], [])
            self.assertEqual(
                r.json()["completeness"], {"is_partial": True, "note": "no live runner"}
            )
        r = self.api.get(SESSIONS, {"environment": "backtest"})
        self.assertEqual(r.json()["data"], [])
        self.assertNotIn("note", r.json().get("completeness") or {})
        r = self.api.get(SESSIONS, {"environment": "paper"})
        self.assertEqual(len(r.json()["data"]), 1)


class SnapshotEvalTests(Base):
    def test_snapshots_ordered_and_paged(self):
        s = self.session()
        other = self.session(coin=self.eth)
        times = [self.now - timedelta(minutes=m) for m in (30, 20, 10)]
        for t, trig in zip(times, ("start", "daily", "stop")):
            SessionSnapshot.objects.create(
                session=s, time=t, trigger=trig, state={"equity": 1.0}, strategy_version=self.sv
            )
        SessionSnapshot.objects.create(session=other, time=times[0], trigger="start", state={})
        r = self.api.get(f"{SESSIONS}{s.pk}/snapshots/", {"page_size": 2})
        d = r.json()["data"]
        self.assertEqual([x["trigger"] for x in d], ["start", "daily"])
        self.assertEqual(d[0]["session_id"], str(s.pk))
        self.assertEqual(d[0]["state"], {"equity": 1.0})
        cur = r.json()["meta"]["next_cursor"]
        r = self.api.get(f"{SESSIONS}{s.pk}/snapshots/", {"page_size": 2, "cursor": cur})
        self.assertEqual([x["trigger"] for x in r.json()["data"]], ["stop"])
        self.assertIsNone(r.json()["meta"]["next_cursor"])

    def test_evaluations_metrics_block(self):
        s = self.session()
        PaperEvaluation.objects.create(
            session=s,
            evaluated_at=self.past,
            passed=True,
            metrics={"sharpe": 1.5, "junk": 1},
            failures=["x"],
            thresholds={"t": 1},
        )
        r = self.api.get(f"{SESSIONS}{s.pk}/evaluations/")
        d = r.json()["data"][0]
        self.assertEqual(set(d["metrics"]), set(METRIC_FIELDS))
        self.assertEqual(d["metrics"]["sharpe"], 1.5)
        self.assertTrue(d["passed"])
        self.assertEqual(d["failures"], ["x"])


class EquityTests(Base):
    def test_equity_window(self):
        s = self.session()
        other = self.session(coin=self.eth)
        base = self.now - timedelta(hours=1)
        rows = [
            {"time": base + timedelta(minutes=i), "session_id": s.pk, "equity": 1000.0 + i,
             "price": 50.0, "position_qty": 1.0, "drawdown": 0.0}
            for i in range(5)
        ]
        rows.append({"time": base, "session_id": other.pk, "equity": 1.0})
        rows.append({"time": self.now - timedelta(seconds=1), "session_id": s.pk, "equity": 9.0})
        timescale.insert_equity_points(rows)
        r = self.api.get(f"{SESSIONS}{s.pk}/equity/")
        d = r.json()["data"]
        self.assertEqual([x["equity"] for x in d], [1000.0 + i for i in range(5)])  # hold-back
        self.assertEqual(set(d[0]), {"time", "equity", "price", "position_qty", "drawdown"})
        r = self.api.get(
            f"{SESSIONS}{s.pk}/equity/",
            {"start": (base + timedelta(minutes=1)).isoformat(),
             "end": (base + timedelta(minutes=3)).isoformat()},
        )
        self.assertEqual([x["equity"] for x in r.json()["data"]], [1001.0, 1002.0, 1003.0])
        r = self.api.get(f"{SESSIONS}{s.pk}/equity/", {"page_size": 2})
        cur = r.json()["meta"]["next_cursor"]
        r = self.api.get(f"{SESSIONS}{s.pk}/equity/", {"page_size": 2, "cursor": cur})
        self.assertEqual([x["equity"] for x in r.json()["data"]], [1002.0, 1003.0])


class FillTests(Base):
    def test_fills_filtered_by_strategy_version(self):
        s = self.session()
        a = self.fill(s)
        self.fill(s, strategy_version=self.sv2)
        r = self.api.get(FILLS, {"strategy_version": "sv1"})
        d = r.json()["data"]
        self.assertEqual([x["id"] for x in d], [a.pk])
        self.assertEqual(
            list(d[0]),
            ["id", "session_id", "environment", "symbol", "time", "created_at", "side", "price",
             "qty", "notional", "fee", "realized_pnl", "level", "reason", "strategy_version",
             "config_version"],
        )
        self.assertEqual(d[0]["symbol"], "BTCUSDT")
        self.assertEqual(d[0]["environment"], "paper")

    def test_session_scoped_and_session_filter(self):
        s = self.session()
        o = self.session(coin=self.eth)
        a = self.fill(s)
        self.fill(o)
        r = self.api.get(f"{SESSIONS}{s.pk}/fills/")
        self.assertEqual([x["id"] for x in r.json()["data"]], [a.pk])
        r = self.api.get(FILLS, {"session": str(s.pk)})
        self.assertEqual([x["id"] for x in r.json()["data"]], [a.pk])
        r = self.api.get(FILLS, {"symbol": "ETHUSDT"})
        self.assertEqual(len(r.json()["data"]), 1)

    def test_fills_incremental_on_created_at_not_time(self):
        s = self.session()
        late = self.fill(
            s, time=self.now - timedelta(hours=2), created_at=self.now - timedelta(minutes=1)
        )
        self.fill(s, time=self.now - timedelta(hours=1), created_at=self.now - timedelta(hours=1))
        since = (self.now - timedelta(minutes=5)).isoformat()
        for url in (FILLS, f"{SESSIONS}{s.pk}/fills/"):
            r = self.api.get(url, {"updated_since": since})
            self.assertEqual([x["id"] for x in r.json()["data"]], [late.pk], url)


class EventTests(Base):
    def event(self, type, **kw):
        return GridEvent.objects.create(
            time=kw.pop("time", self.past), type=type, symbol="BTCUSDT", **kw
        )

    def test_events_filter_by_type_and_session(self):
        s = self.session()
        a = self.event("rebalance", session=s)
        b = self.event("pause", session=s)
        self.event("rebalance")
        self.event("kill_switch", environment="live")
        r = self.api.get(EVENTS, {"type": "rebalance", "session": str(s.pk)})
        self.assertEqual([x["id"] for x in r.json()["data"]], [a.pk])
        r = self.api.get(EVENTS + "?type=rebalance&type=pause&session=" + str(s.pk))
        self.assertEqual({x["id"] for x in r.json()["data"]}, {a.pk, b.pk})
        d = r.json()["data"][0]
        self.assertEqual(d["session_id"], str(s.pk))
        self.assertIn("backfilled", d)
        r = self.api.get(EVENTS, {"environment": "live"})
        self.assertEqual([x["type"] for x in r.json()["data"]], ["kill_switch"])
        self.assertIsNone(r.json()["data"][0]["session_id"])
        r = self.api.get(EVENTS, {"environment": "paper"})
        self.assertEqual(len(r.json()["data"]), 3)

    def test_events_unknown_type_400(self):
        r = self.api.get(EVENTS + "?type=rebalance&type=nope")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"], [{"field": "type", "code": "invalid"}])


class AuditTests(Base):
    def test_order_audit_excludes_response(self):
        LiveOrderAudit.objects.create(
            created_at=self.past, symbol="BTCUSDT", side="buy", order_type="limit",
            qty=1.0, allowed=False, reason="kill switch", response={"secret": 1},
        )
        r = self.api.get(AUDIT)
        self.assertEqual(r.status_code, 200)
        d = r.json()["data"][0]
        self.assertNotIn("response", d)
        self.assertEqual(
            list(d),
            ["created_at", "symbol", "side", "order_type", "qty", "price", "notional",
             "allowed", "reason"],
        )
