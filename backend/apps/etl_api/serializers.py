from datetime import datetime, timedelta, timezone as dt_timezone

from .errors import InvalidParameter


def iso(dt: datetime | None) -> str | None:
    """UTC ISO-8601 with a Z suffix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    dt = dt.astimezone(dt_timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


def strategy_version_out(v) -> dict:
    return {
        "id": v.id,
        "snapshot": v.snapshot,
        "code_sha": v.code_sha,
        "parent_id": v.parent_id,
        "diff": v.diff,
        "first_seen_at": iso(v.first_seen_at),
        "backfilled": v.backfilled,
    }


def config_version_out(v) -> dict:
    return {
        "id": v.id,
        "symbol": v.symbol,
        "params": v.params,
        "first_seen_at": iso(v.first_seen_at),
        "backfilled": v.backfilled,
    }


def candle_out(row: dict, symbol: str, timeframe: str, interval_ms: int) -> dict:
    t = row["time"]
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "open_time": iso(t),
        "close_time": iso(t + timedelta(milliseconds=interval_ms)),
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "quote_volume": row["quote_volume"],
        "trades": row["trades"],
    }


def funding_out(row: dict, symbol: str) -> dict:
    return {
        "symbol": symbol,
        "funding_time": iso(row["time"]),
        "rate": row["rate"],
        "mark_price": row["mark_price"],
    }


def features_out(ca) -> dict:
    run = ca.analysis
    features = ca.features
    if isinstance(features, dict) and "insufficient_data" in features:
        features = None
    return {
        "symbol": ca.coin.binance_symbol,
        "timeframe": (run.params or {}).get("interval"),
        "as_of": iso(run.data_cutoff),
        "analysis_id": str(run.id),
        "strategy_version": run.strategy_version_id,
        "features": features,
    }


METRIC_FIELDS = (
    "net_return", "annualized_return", "max_drawdown", "max_drawdown_duration_days",
    "sharpe", "sortino", "calmar", "profit_factor", "win_rate", "trades", "closed_lots",
    "fees_paid", "funding_paid", "slippage_paid", "avg_exposure_ratio", "max_exposure_ratio",
    "time_in_range", "rebalances", "cost_stress_return",
)


def metrics_block(raw: dict | None) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    return {k: raw.get(k) for k in METRIC_FIELDS}


def backtest_out(ca) -> dict:
    run = ca.analysis
    params = run.params if isinstance(run.params, dict) else {}
    config = params.get("config")
    holdout_days = config.get("holdout_days") if isinstance(config, dict) else None
    holdout_start = None
    if ca.data_end is not None and isinstance(holdout_days, (int, float)):
        try:
            holdout_start = iso(ca.data_end - timedelta(days=holdout_days))
        except (OverflowError, ValueError, TypeError):
            holdout_start = None
    return {
        "id": ca.pk,
        "run_id": str(run.id),
        "environment": "backtest",
        "symbol": ca.coin.binance_symbol,
        "timeframe": params.get("interval"),
        "trigger": run.trigger,
        "mode": run.mode,
        "scope": run.scope,
        "strategy_version": run.strategy_version_id,
        "config_version": ca.config_version_id,
        "data_start": iso(ca.data_start),
        "data_end": iso(ca.data_end),
        "data_cutoff": iso(run.data_cutoff),
        "holdout_start": holdout_start,
        "in_sample": metrics_block(ca.in_sample_metrics),
        "out_of_sample": metrics_block(ca.oos_metrics),
        "score": ca.score,
        "rank": ca.rank,
        "suitability": ca.suitability,
        "eligible": ca.eligible,
        "exclusion_reason": ca.exclusion_reason,
        "component_scores": ca.component_scores,
        "trade_sample": ca.trade_sample,
        "created_at": iso(ca.created_at),
    }


def fold_out(fold) -> dict | None:
    """One walk-forward fold, or None when it is malformed or train/test overlap."""
    from .params import parse_datetime

    if not isinstance(fold, dict):
        return None
    keys = ("train_start", "train_end", "test_start", "test_end")
    if not all(isinstance(fold.get(k), str) for k in keys):
        return None
    try:
        ts = {k: parse_datetime(fold[k], k) for k in keys}
    except (KeyError, TypeError, ValueError, AttributeError, InvalidParameter):
        return None
    if ts["test_start"] < ts["train_end"]:
        return None
    return {
        "environment": "walk_forward",
        "fold": fold.get("fold"),
        **{k: iso(v) for k, v in ts.items()},
        "params": fold.get("params"),
        "train_metrics": metrics_block(fold.get("train_metrics")),
        "test_metrics": metrics_block(fold.get("test_metrics")),
        "regime": fold.get("regime"),
    }


def _session_state(session) -> dict:
    from apps.trading.events import session_state

    try:
        return session_state(session)
    except (AttributeError, TypeError, ValueError):
        # Odd stored grid_state (non-dict, non-numeric levels): serve the grid empty.
        session.grid_state = {}
        return session_state(session)


def session_out(s) -> dict:
    return {
        "id": str(s.pk),
        "environment": "paper",
        "symbol": s.coin.binance_symbol,
        "strategy_version": s.strategy_version_id,
        "config_version": s.config_version_id,
        "started_at": iso(s.started_at),
        "last_tick_at": iso(s.last_tick_at),
        "updated_at": iso(s.updated_at),
        **_session_state(s),
    }


def snapshot_out(snap) -> dict:
    return {
        "session_id": str(snap.session_id),
        "time": iso(snap.time),
        "trigger": snap.trigger,
        "state": snap.state if isinstance(snap.state, dict) else {},
        "strategy_version": snap.strategy_version_id,
        "config_version": snap.config_version_id,
        "backfilled": snap.backfilled,
    }


def equity_out(row: dict) -> dict:
    return {
        "time": iso(row["time"]),
        "equity": row["equity"],
        "price": row["price"],
        "position_qty": row["position_qty"],
        "drawdown": row["drawdown"],
    }


def fill_out(f) -> dict:
    return {
        "id": f.pk,
        "session_id": str(f.session_id),
        "environment": "paper",
        "symbol": f.session.coin.binance_symbol,
        "time": iso(f.time),
        "created_at": iso(f.created_at),
        "side": f.side,
        "price": f.price,
        "qty": f.qty,
        "notional": f.notional,
        "fee": f.fee,
        "realized_pnl": f.realized_pnl,
        "level": f.level,
        "reason": f.reason,
        "strategy_version": f.strategy_version_id,
        "config_version": f.config_version_id,
    }


def evaluation_out(e) -> dict:
    return {
        "id": e.pk,
        "session_id": str(e.session_id),
        "evaluated_at": iso(e.evaluated_at),
        "passed": e.passed,
        "metrics": metrics_block(e.metrics),
        "failures": e.failures,
        "thresholds": e.thresholds,
    }


def event_out(e) -> dict:
    return {
        "id": e.pk,
        "time": iso(e.time),
        "type": e.type,
        "environment": e.environment,
        "symbol": e.symbol,
        "session_id": str(e.session_id) if e.session_id else None,
        "reason": e.reason,
        "before": e.before,
        "after": e.after,
        "strategy_version": e.strategy_version_id,
        "config_version": e.config_version_id,
        "backfilled": e.backfilled,
    }


def order_audit_out(a) -> dict:
    return {
        "created_at": iso(a.created_at),
        "symbol": a.symbol,
        "side": a.side,
        "order_type": a.order_type,
        "qty": a.qty,
        "price": a.price,
        "notional": a.notional,
        "allowed": a.allowed,
        "reason": a.reason,
    }


# Resource name -> public field names, in served order. The OpenAPI builder reads
# this, and tests_contract freezes it against the real serializer output.
RESOURCES: dict[str, tuple[str, ...]] = {
    "strategy_version": (
        "id", "snapshot", "code_sha", "parent_id", "diff", "first_seen_at", "backfilled",
    ),
    "config_version": ("id", "symbol", "params", "first_seen_at", "backfilled"),
    "candle": (
        "symbol", "timeframe", "open_time", "close_time", "open", "high", "low", "close",
        "volume", "quote_volume", "trades",
    ),
    "funding": ("symbol", "funding_time", "rate", "mark_price"),
    "features": ("symbol", "timeframe", "as_of", "analysis_id", "strategy_version", "features"),
    "metrics": METRIC_FIELDS,
    "backtest": (
        "id", "run_id", "environment", "symbol", "timeframe", "trigger", "mode", "scope",
        "strategy_version", "config_version", "data_start", "data_end", "data_cutoff",
        "holdout_start", "in_sample", "out_of_sample", "score", "rank", "suitability",
        "eligible", "exclusion_reason", "component_scores", "trade_sample", "created_at",
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
        "session_id", "time", "trigger", "state", "strategy_version", "config_version",
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
        "created_at", "symbol", "side", "order_type", "qty", "price", "notional", "allowed",
        "reason",
    ),
    "health": (
        "market_candles", "funding_rates", "analysis_run", "paper_session_tick",
        "current_strategy_version",
    ),
}
