from django.db import connection
from django.utils import timezone

from apps.engine.models import AnalysisRun
from apps.engine.versioning import resolve_strategy_version
from apps.trading.models import PaperSession

from rest_framework.response import Response

from ..serializers import RESOURCES, iso
from .base import V1View, envelope


def _stamp(now, latest):
    lag = None if latest is None else max(0.0, (now - latest).total_seconds())
    return {"latest": iso(latest), "lag_seconds": lag}


class HealthView(V1View):
    def get(self, request):
        now = timezone.now()
        with connection.cursor() as cur:
            cur.execute("SELECT interval, MAX(time) FROM market_candles GROUP BY interval")
            candles = {iv: _stamp(now, t) for iv, t in cur.fetchall()}
            cur.execute("SELECT MAX(time) FROM funding_rates")
            funding = cur.fetchone()[0]
        run = (
            AnalysisRun.objects.exclude(finished_at__isnull=True)
            .order_by("-finished_at")
            .values_list("finished_at", flat=True)
            .first()
        )
        tick = (
            PaperSession.objects.exclude(last_tick_at__isnull=True)
            .order_by("-last_tick_at")
            .values_list("last_tick_at", flat=True)
            .first()
        )
        data = {
            "market_candles": candles,
            "funding_rates": _stamp(now, funding),
            "analysis_run": _stamp(now, run),
            "paper_session_tick": _stamp(now, tick),
            "current_strategy_version": resolve_strategy_version().id,
        }
        return envelope(data)


_PAGING = ("start", "end", "updated_since", "cursor", "page_size")

# (path template, resource name, accepted query parameters). One entry per v1 GET
# route; tests_contract compares this against the real urlpatterns.
ROUTES: list[tuple[str, str, tuple[str, ...]]] = [
    ("/meta/health/", "health", ()),
    ("/meta/schema/", "openapi", ()),
    ("/strategy-versions/", "strategy_version", _PAGING),
    ("/strategy-versions/{id}/", "strategy_version", ()),
    ("/config-versions/", "config_version", ("symbol", *_PAGING)),
    ("/config-versions/{id}/", "config_version", ()),
    (
        "/market/candles/",
        "candle",
        ("symbol", "timeframe", "start", "end", "updated_since", "cursor", "page_size"),
    ),
    ("/market/funding/", "funding", ("symbol", *_PAGING)),
    ("/market/features/", "features", ("symbol", "timeframe", *_PAGING)),
    (
        "/backtests/",
        "backtest",
        ("symbol", "timeframe", "environment", "strategy_version", "config_version", *_PAGING),
    ),
    ("/backtests/{id}/", "backtest", ()),
    ("/backtests/{id}/folds/", "fold", ()),
    (
        "/sessions/",
        "session",
        ("symbol", "environment", "strategy_version", "config_version", "session", *_PAGING),
    ),
    ("/sessions/{id}/", "session", ()),
    ("/sessions/{id}/snapshots/", "snapshot", ("environment", *_PAGING)),
    ("/sessions/{id}/equity/", "equity", ("environment", *_PAGING)),
    ("/sessions/{id}/evaluations/", "evaluation", ("environment", *_PAGING)),
    (
        "/sessions/{id}/fills/",
        "fill",
        ("symbol", "environment", "strategy_version", "config_version", *_PAGING),
    ),
    (
        "/fills/",
        "fill",
        ("symbol", "environment", "strategy_version", "config_version", "session", *_PAGING),
    ),
    (
        "/events/",
        "event",
        (
            "type", "environment", "symbol", "session", "strategy_version", "config_version",
            *_PAGING,
        ),
    ),
    ("/live/order-audit/", "order_audit", ("symbol", *_PAGING)),
]

# Field name -> JSON type; anything not listed is a string (ids, timestamps, enums).
FIELD_TYPES: dict[str, str] = {
    **{
        f: "object"
        for f in (
            "snapshot", "diff", "params", "features", "in_sample", "out_of_sample",
            "component_scores", "state", "metrics", "thresholds", "before", "after", "grid",
            "train_metrics", "test_metrics", "market_candles", "funding_rates",
            "analysis_run", "paper_session_tick",
        )
    },
    **{f: "array" for f in ("trade_sample", "failures")},
    **{f: "boolean" for f in ("backfilled", "eligible", "passed", "allowed")},
    **{
        f: "integer"
        for f in ("trades", "rank", "fold", "level", "fill_count", "closed_lots", "wins", "losses")
    },
    **{
        f: "number"
        for f in (
            "open", "high", "low", "close", "volume", "quote_volume", "rate", "mark_price",
            "score", "capital", "leverage", "position_qty", "avg_entry", "equity",
            "peak_equity", "max_drawdown", "realized_pnl", "unrealized_pnl", "fees_paid",
            "funding_paid", "carry_cost", "net_return", "price", "drawdown", "qty",
            "notional", "fee",
        )
    },
}
# `id` is an integer for backtests, fills, evaluations and events; a string elsewhere.
_INTEGER_ID = {"backtest", "fill", "evaluation", "event"}
_PARAMS = {
    "symbol": (
        {"type": "string"},
        "Binance symbol, e.g. BTCUSDT. Repeatable; candles and funding take exactly one.",
    ),
    "timeframe": ({"type": "string"}, "Candle interval, e.g. 1h."),
    "start": (
        {"type": "string", "format": "date-time"},
        "UTC lower bound (inclusive) on the endpoint's incremental key.",
    ),
    "end": (
        {"type": "string", "format": "date-time"},
        "UTC upper bound (inclusive). A date-only value means midnight UTC.",
    ),
    "updated_since": (
        {"type": "string", "format": "date-time"},
        "Inclusive lower bound; send back the previous meta.watermark.",
    ),
    "cursor": ({"type": "string"}, "Opaque cursor from meta.next_cursor."),
    "page_size": (
        {"type": "integer", "minimum": 1},
        "Rows per page. Default 500, max 1000 (candles: 5000).",
    ),
    "environment": (
        {"type": "string", "enum": ["backtest", "walk_forward", "paper", "live"]},
        "Environment filter.",
    ),
    "strategy_version": ({"type": "string"}, "Strategy version id."),
    "config_version": ({"type": "string"}, "Config version id."),
    "session": ({"type": "string", "format": "uuid"}, "Session UUID."),
    "type": ({"type": "string"}, "Event type; repeatable."),
}
_REQUIRED = {
    "/market/candles/": {"symbol", "timeframe"},
    "/market/funding/": {"symbol"},
}


def _field_type(resource: str, field: str) -> str:
    if resource == "metrics":
        return "integer" if field in ("trades", "closed_lots", "rebalances") else "number"
    if field == "id" and resource in _INTEGER_ID:
        return "integer"
    return FIELD_TYPES.get(field, "string")


# Fields guaranteed non-null; every other field may be null (see info.description).
_NON_NULL = {
    "id", "environment", "symbol", "time", "open_time", "close_time", "funding_time",
    "created_at", "first_seen_at", "evaluated_at", "started_at", "updated_at", "as_of",
    "type", "trigger", "status", "side", "order_type", "backfilled", "passed", "allowed",
    "eligible", "open", "high", "low", "close", "volume", "quote_volume", "rate",
    "equity", "analysis_id", "run_id", "code_sha", "state", "snapshot", "diff", "grid",
    "market_candles", "funding_rates", "analysis_run", "paper_session_tick", "before",
    "after", "metrics", "in_sample", "out_of_sample", "train_metrics", "test_metrics",
    "reason",
}
# session_id is non-null everywhere except events.
_NON_NULL_RESOURCE = {
    ("snapshot", "session_id"), ("fill", "session_id"), ("evaluation", "session_id"),
}


def _nullable(resource: str, field: str) -> bool:
    if resource == "metrics":
        return True
    return field not in _NON_NULL and (resource, field) not in _NON_NULL_RESOURCE


def _ref(name: str) -> dict:
    return {"$ref": f"#/components/schemas/{name}"}


def _operation(path: str, resource: str, params: tuple[str, ...]) -> dict:
    parameters = []
    if "{id}" in path:
        parameters.append(
            {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
        )
    for name in params:
        schema, description = _PARAMS[name]
        parameters.append(
            {
                "name": name,
                "in": "query",
                "required": name in _REQUIRED.get(path, ()),
                "description": description,
                "schema": schema,
            }
        )
    if resource == "openapi":
        ok = {
            "description": "This OpenAPI document (not wrapped in the envelope).",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }
    else:
        single = path.endswith("{id}/") and not path.endswith("/folds/") or resource == "health"
        item = _ref(resource)
        ok = {
            "description": "Success.",
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "data": item if single else {"type": "array", "items": item},
                            "meta": _ref("Meta"),
                            "completeness": {"type": "object"},
                        },
                    }
                }
            },
        }
    return {
        "summary": f"GET {path}",
        "parameters": parameters,
        "responses": {
            "200": ok,
            "default": {
                "description": "RFC 7807 problem.",
                "content": {"application/problem+json": {"schema": _ref("Problem")}},
            },
        },
    }


def build_schema() -> dict:
    schemas = {
        name: {
            "type": "object",
            "properties": {
                f: {
                    "type": _field_type(name, f),
                    **({"nullable": True} if _nullable(name, f) else {}),
                }
                for f in fields
            },
        }
        for name, fields in RESOURCES.items()
    }
    schemas["Meta"] = {
        "type": "object",
        "properties": {
            "api_version": {"type": "string"},
            "generated_at": {"type": "string", "format": "date-time"},
            "next_cursor": {"type": "string", "nullable": True},
            "watermark": {"type": "string", "format": "date-time", "nullable": True},
            "page_size": {"type": "integer"},
        },
    }
    schemas["Problem"] = {
        "type": "object",
        "properties": {
            "type": {"type": "string"},
            "title": {"type": "string"},
            "status": {"type": "integer"},
            "detail": {"type": "string"},
            "instance": {"type": "string"},
            "errors": {"type": "array", "items": {"type": "object"}},
        },
    }
    return {
        "openapi": "3.0.3",
        "info": {
            "title": "Marketa+b ETL API",
            "version": "1",
            "description": (
                "Read-only ETL API. See docs/etl-api-v1.md. Any field not marked otherwise "
                "may be null; a metric the engine did not compute is null, never omitted."
            ),
        },
        "servers": [{"url": "/api/v1"}],
        "security": [{"bearerAuth": []}],
        "paths": {
            path: {"get": _operation(path, resource, params)} for path, resource, params in ROUTES
        },
        "components": {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}},
            "schemas": schemas,
        },
    }


class SchemaView(V1View):
    def get(self, request):
        return Response(build_schema())
