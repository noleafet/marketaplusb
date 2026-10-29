from datetime import datetime, timedelta, timezone as dt_tz

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.coins.models import Coin
from apps.engine import timescale
from apps.engine.models import AnalysisRun, CoinAnalysis, ConfigVersion, StrategyVersion
from apps.etl_api.keys import create_client

UTC = dt_tz.utc
T0 = datetime(2026, 10, 1, tzinfo=UTC)
CANDLES = "/api/v1/market/candles/"
FUNDING = "/api/v1/market/funding/"
FEATURES = "/api/v1/market/features/"


def candle(t):
    return {
        "time": t, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5,
        "volume": 10.0, "quote_volume": 15.0, "trades": 7,
    }


class MarketBase(TestCase):
    def setUp(self):
        _, key = create_client("t")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")


class CandleTests(MarketBase):
    def seed(self, hours):
        timescale.upsert_candles("BTCUSDT", "1h", [candle(T0 + timedelta(hours=h)) for h in hours])

    def test_candles_require_symbol_and_timeframe(self):
        r = self.api.get(CANDLES)
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["type"], "https://marketaplusb/errors/invalid_parameter")
        fields = {e["field"] for e in r.json()["errors"]}
        self.assertEqual(fields, {"symbol", "timeframe"})

    def test_candles_single_symbol_and_known_timeframe(self):
        r = self.api.get(CANDLES + "?symbol=BTCUSDT&symbol=ETHUSDT&timeframe=1h")
        self.assertEqual(r.status_code, 400)
        r = self.api.get(CANDLES + "?symbol=BTCUSDT&timeframe=7x")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"][0]["field"], "timeframe")

    def test_candles_never_past_end_or_partial(self):
        self.seed(range(6))
        r = self.api.get(
            CANDLES + "?symbol=BTCUSDT&timeframe=1h&start=2026-10-01T00:00:00Z&end=2026-10-01T03:30:00Z"
        )
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(data[-1]["open_time"], "2026-10-01T02:00:00Z")
        self.assertEqual(data[-1]["close_time"], "2026-10-01T03:00:00Z")
        self.assertEqual(
            list(data[0]),
            [
                "symbol", "timeframe", "open_time", "close_time", "open", "high", "low",
                "close", "volume", "quote_volume", "trades",
            ],
        )

    def test_candles_not_yet_closed_are_held_back(self):
        now = timezone.now().replace(minute=0, second=0, microsecond=0)
        timescale.upsert_candles("BTCUSDT", "1h", [candle(now - timedelta(hours=1)), candle(now)])
        r = self.api.get(CANDLES + "?symbol=BTCUSDT&timeframe=1h")
        opens = [c["open_time"] for c in r.json()["data"]]
        self.assertNotIn(now.isoformat().replace("+00:00", "Z"), opens)

    def test_candles_gap_reported(self):
        self.seed([0, 1, 3, 4])
        r = self.api.get(CANDLES + "?symbol=BTCUSDT&timeframe=1h&start=2026-10-01T00:00:00Z")
        c = r.json()["completeness"]
        self.assertEqual(c["gaps"], [{"from": "2026-10-01T02:00:00Z", "to": "2026-10-01T03:00:00Z"}])
        self.assertEqual(c["expected_bars"], 5)
        self.assertEqual(c["returned_bars"], 4)

    def test_candles_empty_completeness(self):
        r = self.api.get(CANDLES + "?symbol=BTCUSDT&timeframe=1h")
        self.assertEqual(
            r.json()["completeness"], {"expected_bars": 0, "returned_bars": 0, "gaps": []}
        )

    def test_candles_window_too_large(self):
        r = self.api.get(
            CANDLES + "?symbol=BTCUSDT&timeframe=1h&start=2024-01-01T00:00:00Z&end=2026-01-01T00:00:00Z"
        )
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["type"], "https://marketaplusb/errors/window_too_large")
        r = self.api.get(CANDLES + "?symbol=BTCUSDT&timeframe=1h&start=2024-01-01T00:00:00Z")
        self.assertEqual(r.status_code, 400)

    def test_candles_paginate_and_incremental(self):
        self.seed(range(5))
        base = CANDLES + "?symbol=BTCUSDT&timeframe=1h&page_size=2"
        r = self.api.get(base).json()
        self.assertEqual(len(r["data"]), 2)
        seen = [c["open_time"] for c in r["data"]]
        while r["meta"]["next_cursor"]:
            r = self.api.get(base + "&cursor=" + r["meta"]["next_cursor"]).json()
            seen += [c["open_time"] for c in r["data"]]
        self.assertEqual(len(seen), 5)
        self.assertEqual(seen, sorted(seen))
        r = self.api.get(
            CANDLES + "?symbol=BTCUSDT&timeframe=1h&updated_since=2026-10-01T03:00:00Z"
        ).json()
        self.assertEqual(r["data"][0]["open_time"], "2026-10-01T03:00:00Z")


class FundingTests(MarketBase):
    def test_funding_requires_symbol(self):
        self.assertEqual(self.api.get(FUNDING).status_code, 400)

    def test_funding_incremental(self):
        timescale.upsert_funding(
            "BTCUSDT",
            [
                {"time": T0 + timedelta(hours=8 * i), "rate": 0.0001 * i, "mark_price": 100.0}
                for i in range(4)
            ],
        )
        r = self.api.get(FUNDING + "?symbol=BTCUSDT&updated_since=2026-10-01T16:00:00Z")
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(
            [d["funding_time"] for d in data], ["2026-10-01T16:00:00Z", "2026-10-02T00:00:00Z"]
        )
        self.assertEqual(list(data[0]), ["symbol", "funding_time", "rate", "mark_price"])
        self.assertEqual(r.json()["meta"]["watermark"], "2026-10-02T00:00:00Z")


class FeaturesTests(MarketBase):
    def test_features_as_of_is_cutoff(self):
        sv = StrategyVersion.objects.create(id="sv1", snapshot={}, code_sha="abc")
        cutoff = T0 + timedelta(hours=3)
        run = AnalysisRun.objects.create(
            status="succeeded", data_cutoff=cutoff, params={"interval": "1h"}, strategy_version=sv
        )
        bad = AnalysisRun.objects.create(
            status="failed", data_cutoff=cutoff, params={"interval": "1h"}
        )
        coin = Coin.objects.create(coingecko_id="b", symbol="BTC", name="B", binance_symbol="BTCUSDT")
        coin2 = Coin.objects.create(coingecko_id="e", symbol="ETH", name="E", binance_symbol="ETHUSDT")
        CoinAnalysis.objects.create(analysis=run, coin=coin, features={"adx": 20})
        CoinAnalysis.objects.create(analysis=run, coin=coin2, features={"insufficient_data": True})
        CoinAnalysis.objects.create(analysis=bad, coin=coin, features={"adx": 1})
        r = self.api.get(FEATURES)
        self.assertEqual(r.status_code, 200)
        data = r.json()["data"]
        self.assertEqual(len(data), 2)
        by = {d["symbol"]: d for d in data}
        self.assertEqual(by["BTCUSDT"]["as_of"], "2026-10-01T03:00:00Z")
        self.assertEqual(by["BTCUSDT"]["timeframe"], "1h")
        self.assertEqual(by["BTCUSDT"]["analysis_id"], str(run.id))
        self.assertEqual(by["BTCUSDT"]["strategy_version"], "sv1")
        self.assertEqual(by["BTCUSDT"]["features"], {"adx": 20})
        self.assertIsNone(by["ETHUSDT"]["features"])
        self.assertEqual(
            list(data[0]),
            ["symbol", "timeframe", "as_of", "analysis_id", "strategy_version", "features"],
        )
        r = self.api.get(FEATURES + "?symbol=ETHUSDT&timeframe=1h")
        self.assertEqual(len(r.json()["data"]), 1)
        r = self.api.get(FEATURES + "?timeframe=4h")
        self.assertEqual(r.json()["data"], [])


class VersionTests(MarketBase):
    def test_strategy_version_detail_and_list(self):
        p = StrategyVersion.objects.create(
            id="p1", snapshot={"a": 1}, code_sha="abc", first_seen_at=T0
        )
        StrategyVersion.objects.create(
            id="c1", snapshot={"a": 2}, code_sha="", parent=p, diff={"a": [1, 2]},
            first_seen_at=T0 + timedelta(hours=1), backfilled=True,
        )
        r = self.api.get("/api/v1/strategy-versions/")
        self.assertEqual(r.status_code, 200)
        j = r.json()
        self.assertEqual([d["id"] for d in j["data"]], ["p1", "c1"])
        self.assertEqual(
            list(j["data"][0]),
            ["id", "snapshot", "code_sha", "parent_id", "diff", "first_seen_at", "backfilled"],
        )
        self.assertEqual(j["data"][1]["parent_id"], "p1")
        self.assertEqual(j["completeness"], {"code_sha_unknown_ids": ["c1"]})
        r = self.api.get("/api/v1/strategy-versions/c1/")
        self.assertEqual(r.json()["data"]["id"], "c1")
        self.assertEqual(r.json()["completeness"], {"code_sha_known": False})
        self.assertEqual(
            self.api.get("/api/v1/strategy-versions/p1/").json()["completeness"],
            {"code_sha_known": True},
        )
        r = self.api.get("/api/v1/strategy-versions/nope/")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(r["Content-Type"], "application/problem+json")

    def test_config_versions_filter_by_symbol(self):
        ConfigVersion.objects.create(id="a", symbol="BTCUSDT", params={"x": 1}, first_seen_at=T0)
        ConfigVersion.objects.create(id="b", symbol="ETHUSDT", params={"x": 2}, first_seen_at=T0)
        r = self.api.get("/api/v1/config-versions/?symbol=ethusdt")
        self.assertEqual([d["id"] for d in r.json()["data"]], ["b"])
        self.assertEqual(
            list(r.json()["data"][0]), ["id", "symbol", "params", "first_seen_at", "backfilled"]
        )
        self.assertEqual(
            self.api.get("/api/v1/config-versions/a/").json()["data"]["symbol"], "BTCUSDT"
        )
        self.assertEqual(self.api.get("/api/v1/config-versions/zz/").status_code, 404)


class HardeningTests(MarketBase):
    def test_candles_far_past_end_is_400(self):
        r = self.api.get(CANDLES + "?symbol=BTCUSDT&timeframe=1h&end=0001-01-01T00:00:00Z")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"][0], {"field": "end", "code": "out_of_range"})

    def test_funding_page_size_capped_at_1000(self):
        self.assertEqual(self.api.get(FUNDING + "?symbol=BTCUSDT&page_size=1001").status_code, 400)
        self.assertEqual(self.api.get(FUNDING + "?symbol=BTCUSDT&page_size=1000").status_code, 200)

    def test_nul_in_params_is_400(self):
        r = self.api.get(CANDLES + "?symbol=%00&timeframe=1h")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["errors"][0], {"field": "symbol", "code": "invalid"})

    def test_nul_in_version_pk_is_404(self):
        for route in ("strategy-versions", "config-versions"):
            r = self.api.get(f"/api/v1/{route}/%00/")
            self.assertEqual(r.status_code, 404)
            self.assertEqual(r["Content-Type"], "application/problem+json")
