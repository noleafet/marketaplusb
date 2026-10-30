"""Contract snapshots: the public field lists, envelope, schema coverage and UTC timestamps.

FROZEN_FIELDS is deliberately a literal copy. Removing or renaming a public field
must fail here, so update it only as an intentional (versioned) contract change.
"""

import re
from datetime import timedelta

from django.test import TestCase
from django.urls.resolvers import RoutePattern
from django.utils import timezone
from rest_framework.test import APIClient

from apps.coins.models import Coin
from apps.engine import timescale
from apps.engine.models import AnalysisRun, CoinAnalysis, ConfigVersion, StrategyVersion
from apps.etl_api import serializers, urls
from apps.etl_api.keys import create_client
from apps.etl_api.views import meta
from apps.trading.models import (
    GridEvent,
    LiveOrderAudit,
    PaperEvaluation,
    PaperFill,
    PaperSession,
    SessionSnapshot,
)

METRICS = (
    "net_return", "annualized_return", "max_drawdown", "max_drawdown_duration_days",
    "sharpe", "sortino", "calmar", "profit_factor", "win_rate", "trades", "closed_lots",
    "fees_paid", "funding_paid", "slippage_paid", "avg_exposure_ratio", "max_exposure_ratio",
    "time_in_range", "rebalances", "cost_stress_return",
)

FROZEN_FIELDS = {
    "strategy_version": (
        "id", "snapshot", "code_sha", "parent_id", "diff", "first_seen_at", "backfilled",
    ),
    "config_version": ("id", "symbol", "params", "first_seen_at", "backfilled"),
    "candle": (
        "symbol", "timeframe", "open_time", "close_time", "open", "high", "low", "close",
        "volume", "quote_volume", "trades",
    ),
    "funding": ("symbol", "funding_time", "rate", "mark_price"),
    "features": (
        "symbol", "timeframe", "as_of", "finished_at", "analysis_id", "strategy_version",
        "features",
    ),
    "metrics": METRICS,
    "backtest": (
        "id", "run_id", "environment", "symbol", "timeframe", "trigger", "mode", "scope",
        "drives_sessions", "strategy_version", "config_version", "data_start", "data_end",
        "data_cutoff", "holdout_start", "in_sample", "out_of_sample", "score", "rank",
        "suitability", "eligible", "exclusion_reason", "component_scores", "trade_sample",
        "created_at",
    ),
    "fold": (
        "environment", "fold", "train_start", "train_end", "test_start", "test_end", "params",
        "train_metrics", "test_metrics", "regime",
    ),
    "session": (
        "id", "environment", "symbol", "strategy_version", "config_version", "started_at",
        "last_tick_at", "updated_at", "status", "bias", "capital", "leverage", "position_qty",
        "avg_entry", "mark_price", "equity", "peak_equity", "max_drawdown", "realized_pnl",
        "unrealized_pnl", "fees_paid", "funding_paid", "carry_cost", "fill_count",
        "closed_lots", "wins", "losses", "net_return", "grid", "stop_reason",
    ),
    "snapshot": (
        "id", "session_id", "time", "trigger", "state", "strategy_version", "config_version",
        "backfilled",
    ),
    "equity": ("time", "equity", "price", "position_qty", "drawdown"),
    "fill": (
        "id", "session_id", "environment", "symbol", "time", "created_at", "side", "price",
        "qty", "notional", "fee", "realized_pnl", "level", "reason", "strategy_version",
        "config_version",
    ),
    "evaluation": (
        "id", "session_id", "evaluated_at", "passed", "metrics", "failures", "thresholds",
    ),
    "event": (
        "id", "time", "type", "environment", "symbol", "session_id", "reason", "before",
        "after", "strategy_version", "config_version", "backfilled",
    ),
    "order_audit": (
        "id", "created_at", "symbol", "side", "order_type", "qty", "price", "notional", "allowed",
        "reason",
    ),
    "health": (
        "market_candles", "funding_rates", "analysis_run", "paper_session_tick",
        "current_strategy_version",
    ),
}

ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
ISO_ANY = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")


class ContractBase(TestCase):
    def setUp(self):
        _, key = create_client("t")
        self.api = APIClient()
        self.api.credentials(HTTP_AUTHORIZATION=f"Bearer {key}")
        now = timezone.now()
        self.past = now - timedelta(hours=2)
        base = (now - timedelta(days=3)).replace(minute=0, second=0, microsecond=0)

        self.sv = StrategyVersion.objects.create(id="sv1", snapshot={"a": 1}, code_sha="abc")
        self.cv = ConfigVersion.objects.create(id="cv1", symbol="BTCUSDT", params={"grid": 1})
        for obj in (self.sv, self.cv):
            type(obj).objects.filter(pk=obj.pk).update(first_seen_at=self.past)
        coin = Coin.objects.create(
            coingecko_id="b", symbol="BTC", name="B", binance_symbol="BTCUSDT"
        )

        row = {
            "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 1.0,
            "quote_volume": 1.0, "trades": 1,
        }
        timescale.upsert_candles(
            "BTCUSDT", "1h", [{"time": base + timedelta(hours=h), **row} for h in range(3)]
        )
        timescale.upsert_funding(
            "BTCUSDT", [{"time": base + timedelta(hours=8), "rate": 0.0001, "mark_price": 1.0}]
        )

        run = AnalysisRun.objects.create(
            status="succeeded",
            strategy_version=self.sv,
            data_cutoff=self.past,
            finished_at=self.past,
            params={"interval": "1h", "config": {"holdout_days": 30}},
        )
        fold = {
            "fold": 1,
            "train_start": "2026-01-01T00:00:00Z",
            "train_end": "2026-02-01T00:00:00Z",
            "test_start": "2026-02-01T00:00:00Z",
            "test_end": "2026-03-01T00:00:00Z",
            "params": {},
            "train_metrics": {"sharpe": 1.0},
            "test_metrics": {"sharpe": 0.5},
            "regime": "range",
        }
        self.ca = CoinAnalysis.objects.create(
            analysis=run,
            coin=coin,
            rank=1,
            score=1.0,
            suitability="viable",
            eligible=True,
            config_version=self.cv,
            data_start=self.past - timedelta(days=60),
            data_end=self.past,
            in_sample_metrics={"sharpe": 1.0},
            oos_metrics={"sharpe": 0.5},
            folds=[fold],
            features={"adx": 20},
        )
        CoinAnalysis.objects.filter(pk=self.ca.pk).update(created_at=self.past)

        self.session = PaperSession.objects.create(
            coin=coin,
            strategy_version=self.sv,
            config_version=self.cv,
            grid_state={"range_low": 90, "range_high": 120, "levels": [100, 110]},
        )
        PaperSession.objects.filter(pk=self.session.pk).update(updated_at=self.past)
        SessionSnapshot.objects.create(
            session=self.session, time=self.past, trigger="start", state={"equity": 1.0}
        )
        PaperFill.objects.create(
            session=self.session, time=self.past, created_at=self.past, side="buy",
            price=100.0, qty=1.0, notional=100.0,
        )
        PaperEvaluation.objects.create(
            session=self.session, evaluated_at=self.past, passed=True, metrics={"sharpe": 1}
        )
        timescale.insert_equity_points(
            [{
                "time": base, "session_id": self.session.pk, "equity": 1000.0, "price": 50.0,
                "position_qty": 1.0, "drawdown": 0.0,
            }]
        )
        GridEvent.objects.create(time=self.past, type="rebalance", symbol="BTCUSDT")
        LiveOrderAudit.objects.create(
            created_at=self.past, symbol="BTCUSDT", side="buy", order_type="limit",
            qty=1.0, allowed=False, reason="kill switch",
        )

        sid = self.session.pk
        # resource -> (url, params, sub-path of the sampled row inside the body, if any)
        self.samples = {
            "strategy_version": "/api/v1/strategy-versions/",
            "config_version": "/api/v1/config-versions/",
            "candle": "/api/v1/market/candles/?symbol=BTCUSDT&timeframe=1h",
            "funding": "/api/v1/market/funding/?symbol=BTCUSDT",
            "features": "/api/v1/market/features/",
            "backtest": "/api/v1/backtests/",
            "fold": f"/api/v1/backtests/{self.ca.pk}/folds/",
            "session": "/api/v1/sessions/",
            "snapshot": f"/api/v1/sessions/{sid}/snapshots/",
            "equity": f"/api/v1/sessions/{sid}/equity/",
            "fill": "/api/v1/fills/",
            "evaluation": f"/api/v1/sessions/{sid}/evaluations/",
            "event": "/api/v1/events/",
            "order_audit": "/api/v1/live/order-audit/",
            "health": "/api/v1/meta/health/",
        }
        self.lists = [u for k, u in self.samples.items() if k not in ("fold", "health")]

    def get(self, url):
        r = self.api.get(url)
        self.assertEqual(r.status_code, 200, url)
        return r.json()

    def sample(self, name):
        body = self.get(self.samples[name])
        data = body["data"]
        if name == "health":
            return data
        self.assertTrue(data, f"no sample row for {name}")
        return data[0]


class ContractTests(ContractBase):
    def test_every_resource_matches_frozen_fields(self):
        for name, fields in FROZEN_FIELDS.items():
            if name == "metrics":
                continue
            self.assertEqual(tuple(self.sample(name).keys()), fields, name)
        bt = self.sample("backtest")
        self.assertEqual(tuple(bt["in_sample"]), METRICS)
        self.assertEqual(tuple(bt["out_of_sample"]), METRICS)

    def test_detail_routes_match_list_fields(self):
        sid = self.session.pk
        for name, url in (
            ("strategy_version", "/api/v1/strategy-versions/sv1/"),
            ("config_version", "/api/v1/config-versions/cv1/"),
            ("backtest", f"/api/v1/backtests/{self.ca.pk}/"),
            ("session", f"/api/v1/sessions/{sid}/"),
        ):
            self.assertEqual(tuple(self.get(url)["data"]), FROZEN_FIELDS[name], name)

    def test_registry_matches_frozen_fields(self):
        self.assertEqual(
            {k: tuple(v) for k, v in serializers.RESOURCES.items()}, FROZEN_FIELDS
        )

    def test_every_list_has_envelope(self):
        for url in self.lists:
            body = self.get(url)
            self.assertEqual(set(body), {"data", "meta", "completeness"}, url)
            self.assertTrue(body["meta"]["generated_at"].endswith("Z"), url)
            self.assertEqual(body["meta"]["api_version"], "1")
            self.assertEqual(
                {"next_cursor", "watermark", "page_size"} - set(body["meta"]), set(), url
            )

    def test_detail_has_envelope(self):
        body = self.get(f"/api/v1/sessions/{self.session.pk}/")
        self.assertEqual(set(body), {"data", "meta", "completeness"})

    def test_all_timestamps_are_utc_z(self):
        urls_ = list(self.samples.values()) + [
            f"/api/v1/sessions/{self.session.pk}/",
            f"/api/v1/backtests/{self.ca.pk}/",
            "/api/v1/strategy-versions/sv1/",
        ]
        seen = 0

        def walk(node, url):
            nonlocal seen
            if isinstance(node, dict):
                for v in node.values():
                    walk(v, url)
            elif isinstance(node, list):
                for v in node:
                    walk(v, url)
            elif isinstance(node, str) and ISO_ANY.match(node):
                seen += 1
                self.assertRegex(node, ISO_Z, url)

        for url in urls_:
            walk(self.get(url), url)
        self.assertGreater(seen, 30)


class SchemaTests(ContractBase):
    def schema(self):
        return self.get("/api/v1/meta/schema/")

    def test_schema_requires_key(self):
        r = APIClient().get("/api/v1/meta/schema/")
        self.assertEqual(r.status_code, 401)

    def test_document_header(self):
        s = self.schema()
        self.assertEqual(s["openapi"], "3.0.3")
        self.assertEqual(s["info"]["title"], "Marketa+b ETL API")
        self.assertEqual(s["info"]["version"], "1")
        scheme = s["components"]["securitySchemes"]["bearerAuth"]
        self.assertEqual((scheme["type"], scheme["scheme"]), ("http", "bearer"))

    def test_schema_lists_every_route(self):
        """Compares against the real urlpatterns, so a new route without a ROUTES entry fails."""
        actual = {
            "/" + re.sub(r"<[^>]+>", "{id}", str(p.pattern))
            for p in urls.urlpatterns
            if isinstance(p.pattern, RoutePattern)  # re_path is the catch-all
        }
        self.assertIn("/meta/schema/", actual)
        self.assertEqual({r[0] for r in meta.ROUTES}, actual)
        self.assertEqual(set(self.schema()["paths"]), actual)

    def test_operations_and_components(self):
        s = self.schema()
        for path, resource, params in meta.ROUTES:
            op = s["paths"][path]["get"]
            self.assertEqual(set(s["paths"][path]), {"get"}, path)
            declared = [p["name"] for p in op["parameters"] if p["in"] == "query"]
            self.assertEqual(tuple(declared), params, path)
            if resource != "openapi":
                self.assertIn(resource, serializers.RESOURCES, path)
        for name, fields in serializers.RESOURCES.items():
            props = s["components"]["schemas"][name]["properties"]
            self.assertEqual(tuple(props), tuple(fields), name)
            for spec in props.values():
                self.assertIn(
                    spec["type"], {"string", "number", "integer", "boolean", "object", "array"}
                )

    def test_nullability_and_metric_types(self):
        schemas = self.schema()["components"]["schemas"]
        self.assertIn("may be null", self.schema()["info"]["description"])
        for f in METRICS:
            self.assertTrue(schemas["metrics"]["properties"][f].get("nullable"), f)
        for f in ("trades", "closed_lots", "rebalances"):
            self.assertEqual(schemas["metrics"]["properties"][f]["type"], "integer")
        self.assertTrue(schemas["features"]["properties"]["features"]["nullable"])
        self.assertTrue(schemas["event"]["properties"]["session_id"]["nullable"])
        self.assertTrue(schemas["strategy_version"]["properties"]["parent_id"]["nullable"])
        for name in ("snapshot", "order_audit"):
            spec = schemas[name]["properties"]["id"]
            self.assertEqual(spec["type"], "integer", name)
            self.assertFalse(spec.get("nullable"), name)
        self.assertFalse(schemas["features"]["properties"]["finished_at"].get("nullable"))
        self.assertNotIn("nullable", schemas["candle"]["properties"]["open_time"])
        self.assertNotIn("nullable", schemas["fill"]["properties"]["session_id"])

    def test_every_route_has_a_registered_query_contract(self):
        by_path = {p: params for p, _, params in meta.ROUTES}
        self.assertEqual(
            by_path["/market/candles/"],
            (
                "symbol", "timeframe", "start", "end", "updated_since", "cursor", "page_size",
            ),
        )
        self.assertEqual(by_path["/meta/health/"], ())

    def test_every_sampled_resource_has_a_route(self):
        resources = {r for _, r, _ in meta.ROUTES}
        self.assertEqual(set(FROZEN_FIELDS) - {"metrics"}, resources - {"openapi"})
