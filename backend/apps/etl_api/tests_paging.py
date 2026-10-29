import base64
import json
import uuid
from datetime import datetime, timedelta, timezone as dt_tz

from django.test import TestCase
from django.utils import timezone
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory

from apps.coins.models import Coin
from apps.trading.models import PaperFill, PaperSession

from .errors import CursorExpired, InvalidParameter, WindowTooLarge
from .pagination import (
    Page,
    decode_cursor,
    encode_cursor,
    page_meta,
    paginate_queryset,
    paginate_rows,
)
from .params import ENVIRONMENTS, HOLD_BACK, parse_query
from .serializers import iso

UTC = dt_tz.utc


def req(**params):
    return Request(APIRequestFactory().get("/api/v1/x", params))


def q(**kw):
    return parse_query(req(**kw))


def b64(obj):
    raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
    return base64.urlsafe_b64encode(raw).decode()


class ParseQueryTests(TestCase):
    def test_defaults(self):
        query = q()
        self.assertEqual(query.page_size, 500)
        self.assertEqual(query.symbols, [])
        self.assertIsNone(query.start)

    def test_parse_naive_and_offset_dates(self):
        query = q(start="2026-10-01T00:00:00", end="2026-10-01T02:00:00+02:00")
        self.assertEqual(query.start, datetime(2026, 10, 1, tzinfo=UTC))
        self.assertEqual(query.end, datetime(2026, 10, 1, tzinfo=UTC))

    def test_z_suffix_and_updated_since(self):
        query = q(updated_since="2026-10-01T00:00:00Z")
        self.assertEqual(query.updated_since, datetime(2026, 10, 1, tzinfo=UTC))

    def test_bad_date(self):
        with self.assertRaises(InvalidParameter) as c:
            q(start="yesterday")
        self.assertEqual(c.exception.errors[0]["field"], "start")

    def test_start_after_end_invalid(self):
        with self.assertRaises(InvalidParameter) as c:
            q(start="2026-10-02T00:00:00Z", end="2026-10-01T00:00:00Z")
        self.assertEqual(c.exception.errors[0]["field"], "start")
        self.assertEqual(c.exception.errors[0]["code"], "after_end")

    def test_window_too_large(self):
        r = req(start="2025-01-01T00:00:00Z", end="2026-02-06T00:00:00Z")  # 401 days
        with self.assertRaises(WindowTooLarge):
            parse_query(r, max_window_days=400)
        ok = req(start="2025-01-01T00:00:00Z", end="2026-02-05T00:00:00Z")  # 400 days
        parse_query(ok, max_window_days=400)

    def test_page_size_bounds(self):
        for bad in ("0", "1001", "abc", "-5"):
            with self.assertRaises(InvalidParameter, msg=bad) as c:
                q(page_size=bad)
            self.assertEqual(c.exception.errors[0]["field"], "page_size")
        self.assertEqual(q(page_size="1000").page_size, 1000)
        self.assertEqual(parse_query(req(), max_page_size=100).page_size, 100)

    def test_required(self):
        with self.assertRaises(InvalidParameter) as c:
            parse_query(req(), required=("start", "timeframe"))
        self.assertEqual([e["field"] for e in c.exception.errors], ["start", "timeframe"])
        self.assertEqual(c.exception.errors[0]["code"], "required")

    def test_symbols_repeatable_uppercased(self):
        r = Request(APIRequestFactory().get("/x?symbol=btc&symbol=eth"))
        self.assertEqual(parse_query(r).symbols, ["BTC", "ETH"])

    def test_environment(self):
        self.assertEqual(q(environment="paper").environment, "paper")
        with self.assertRaises(InvalidParameter) as c:
            q(environment="moon")
        self.assertEqual(c.exception.errors[0]["field"], "environment")
        with self.assertRaises(InvalidParameter):
            parse_query(req(environment="live"), environments=("paper",))
        self.assertIn("walk_forward", ENVIRONMENTS)

    def test_session_uuid(self):
        u = uuid.uuid4()
        self.assertEqual(q(session=str(u)).session, u)
        with self.assertRaises(InvalidParameter) as c:
            q(session="nope")
        self.assertEqual(c.exception.errors[0]["field"], "session")

    def test_unknown_params_ignored(self):
        self.assertEqual(q(banana="1").page_size, 500)


class CursorTests(TestCase):
    def test_roundtrip_int_and_uuid(self):
        t = datetime(2026, 10, 1, 12, 0, 0, 123456, tzinfo=UTC)
        self.assertEqual(decode_cursor("fills", encode_cursor("fills", t, 42)), (t, 42))
        u = uuid.uuid4()
        self.assertEqual(decode_cursor("s", encode_cursor("s", t, u)), (t, str(u)))

    def test_bad_cursor_400(self):
        good_k = "2026-10-01T00:00:00Z"
        bad = [
            "!!!",
            b64({"x": 1}),
            b64(b"not json"),
            b64({"e": "fills", "k": "garbage", "i": 1}),
            b64({"e": "fills", "k": good_k, "i": [1]}),
            b64({"e": "fills", "k": good_k}),
            b64([1, 2]),
        ]
        for token in bad:
            with self.assertRaises(InvalidParameter, msg=token) as c:
                decode_cursor("fills", token)
            self.assertEqual(c.exception.errors, [{"field": "cursor", "code": "invalid"}])

    def test_foreign_cursor_410(self):
        t = datetime(2026, 10, 1, tzinfo=UTC)
        with self.assertRaises(CursorExpired):
            decode_cursor("fills", encode_cursor("events", t, 1))


class PagingBase(TestCase):
    def setUp(self):
        self.coin = Coin.objects.create(coingecko_id="btc", symbol="BTC", name="Bitcoin")
        self.session = PaperSession.objects.create(coin=self.coin)

    def make_fills(self, n, time):
        return [
            PaperFill.objects.create(
                session=self.session, time=time, side="buy", price=1, qty=1, notional=1
            )
            for _ in range(n)
        ]

    def page(self, cursor=None, **kw):
        params = {k: v for k, v in kw.items() if v is not None}
        if cursor:
            params["cursor"] = cursor
        return paginate_queryset(
            PaperFill.objects.all(), endpoint="fills", key="time", query=parse_query(req(**params))
        )

    def walk_all(self, **kw):
        seen, cursor = [], None
        while True:
            p = self.page(cursor, **kw)
            seen += [r.id for r in p.rows]
            cursor = p.next_cursor
            if cursor is None:
                return seen, p


class PaginateQuerysetTests(PagingBase):
    def test_cursor_ties_across_pages(self):
        t = timezone.now() - timedelta(minutes=1)
        self.make_fills(7, t)
        seen, _ = self.walk_all(page_size=3)
        self.assertEqual(sorted(seen), sorted(PaperFill.objects.values_list("id", flat=True)))
        self.assertEqual(len(seen), len(set(seen)))

    def test_inserts_during_walk_not_duplicated_or_skipped(self):
        t = timezone.now() - timedelta(minutes=1)
        self.make_fills(7, t)
        p1 = self.page(page_size=3)
        new = self.make_fills(1, t + timedelta(seconds=20))[0]
        seen, cursor = [r.id for r in p1.rows], p1.next_cursor
        while cursor:
            p = self.page(cursor, page_size=3)
            seen += [r.id for r in p.rows]
            cursor = p.next_cursor
        self.assertEqual(seen.count(new.id), 1)
        self.assertEqual(len(seen), 8)
        self.assertEqual(len(seen), len(set(seen)))

    def test_hold_back_excludes_last_5_seconds(self):
        now = timezone.now()
        fresh = self.make_fills(1, now)[0]
        old = self.make_fills(1, now - timedelta(seconds=10))[0]
        ids = [r.id for r in self.page().rows]
        self.assertIn(old.id, ids)
        self.assertNotIn(fresh.id, ids)
        self.assertEqual(HOLD_BACK, timedelta(seconds=5))

    def test_hold_back_applies_with_updated_since_and_end(self):
        now = timezone.now()
        fresh = self.make_fills(1, now)[0]
        p = self.page(
            updated_since=iso(now - timedelta(hours=1)), end=iso(now + timedelta(hours=1))
        )
        self.assertNotIn(fresh.id, [r.id for r in p.rows])

    def test_watermark_roundtrip_covers_everything(self):
        base = timezone.now() - timedelta(minutes=10)
        for i in range(5):
            self.make_fills(1, base + timedelta(seconds=i))
        _, last = self.walk_all(page_size=2)
        w = last.watermark
        self.assertEqual(w, iso(base + timedelta(seconds=4)))
        new = self.make_fills(3, base + timedelta(minutes=5))
        p = self.page(updated_since=w)
        got = {r.id for r in p.rows}
        self.assertTrue({n.id for n in new} <= got)
        self.assertLessEqual(len(got - {n.id for n in new}), 1)
        self.assertEqual(len(p.rows), len(got))

    def test_watermark_empty_page_echoes_updated_since_else_none(self):
        self.assertIsNone(self.page().watermark)
        since = iso(timezone.now() - timedelta(minutes=1))
        p = self.page(updated_since=since)
        self.assertEqual(p.rows, [])
        self.assertEqual(p.watermark, since)

    def test_watermark_is_last_row_of_this_page(self):
        t = timezone.now() - timedelta(minutes=5)
        for i in range(4):
            self.make_fills(1, t + timedelta(seconds=i))
        p = self.page(page_size=2)
        self.assertEqual(p.watermark, iso(t + timedelta(seconds=1)))
        self.assertIsNotNone(p.next_cursor)

    def test_updated_since_inclusive(self):
        t = timezone.now() - timedelta(minutes=5)
        f = self.make_fills(1, t)[0]
        self.assertIn(f.id, [r.id for r in self.page(updated_since=iso(t)).rows])

    def test_start_end_applied_to_key(self):
        t = timezone.now() - timedelta(minutes=30)
        fills = [self.make_fills(1, t + timedelta(minutes=i))[0] for i in range(4)]
        p = self.page(start=iso(t + timedelta(minutes=1)), end=iso(t + timedelta(minutes=2)))
        self.assertEqual([r.id for r in p.rows], [fills[1].id, fills[2].id])

    def test_foreign_cursor_and_bad_cursor_via_paginate(self):
        with self.assertRaises(InvalidParameter):
            self.page("!!!")
        with self.assertRaises(CursorExpired):
            self.page(encode_cursor("events", timezone.now(), 1))

    def test_related_key_path(self):
        t = timezone.now() - timedelta(minutes=5)
        self.make_fills(3, t)
        qs = PaperFill.objects.select_related("session")
        # session started_at is ~now: move it into the past, clear hold-back
        PaperSession.objects.update(started_at=t)
        self.session.refresh_from_db()
        p1 = paginate_queryset(
            qs, endpoint="f", key="session__started_at", query=parse_query(req(page_size=2))
        )
        self.assertEqual(len(p1.rows), 2)
        self.assertEqual(p1.watermark, iso(t))
        p2 = paginate_queryset(
            qs,
            endpoint="f",
            key="session__started_at",
            query=parse_query(req(page_size=2, cursor=p1.next_cursor)),
        )
        self.assertEqual(len(p2.rows), 1)
        self.assertIsNone(p2.next_cursor)

    def test_uuid_pk(self):
        past = timezone.now() - timedelta(minutes=5)
        sessions = [PaperSession.objects.create(coin=self.coin, started_at=past) for _ in range(4)]
        seen, cursor = [], None
        while True:
            params = {"page_size": 2}
            if cursor:
                params["cursor"] = cursor
            p = paginate_queryset(
                PaperSession.objects.all(),
                endpoint="sessions",
                key="started_at",
                query=parse_query(req(**params)),
            )
            seen += [r.id for r in p.rows]
            cursor = p.next_cursor
            if not cursor:
                break
        # self.session (started ~now) is held back
        self.assertEqual(sorted(seen, key=str), sorted((s.id for s in sessions), key=str))

    def test_page_meta(self):
        self.assertEqual(
            page_meta(Page(rows=[], next_cursor="c", watermark="w"), q(page_size=7)),
            {"next_cursor": "c", "watermark": "w", "page_size": 7},
        )


class PaginateRowsTests(TestCase):
    def setUp(self):
        t = datetime(2026, 1, 1, tzinfo=UTC)
        self.data = [{"ts": t + timedelta(seconds=i // 2), "rid": i} for i in range(5)]

    def fetch(self, after_key, after_id, limit):
        rows = [
            r
            for r in self.data
            if after_key is None
            or r["ts"] > after_key
            or (r["ts"] == after_key and r["rid"] > after_id)
        ]
        return rows[:limit]

    def test_walk(self):
        seen, cursor = [], None
        while True:
            params = {"page_size": 2}
            if cursor:
                params["cursor"] = cursor
            p = paginate_rows(
                self.fetch, endpoint="e", key="ts", id_key="rid", query=parse_query(req(**params))
            )
            seen += [r["rid"] for r in p.rows]
            cursor = p.next_cursor
            if not cursor:
                break
        self.assertEqual(seen, [0, 1, 2, 3, 4])
        self.assertEqual(p.watermark, iso(self.data[-1]["ts"]))

    def test_empty_uses_updated_since(self):
        query = q(updated_since="2026-01-01T00:00:00Z")
        p = paginate_rows(lambda *a: [], endpoint="e", key="ts", id_key="rid", query=query)
        self.assertEqual(p.watermark, "2026-01-01T00:00:00Z")
        self.assertIsNone(p.next_cursor)
