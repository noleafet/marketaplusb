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
    params = run.params or {}
    holdout_days = (params.get("config") or {}).get("holdout_days")
    holdout_start = None
    if ca.data_end is not None and isinstance(holdout_days, (int, float)):
        try:
            holdout_start = iso(ca.data_end - timedelta(days=holdout_days))
        except OverflowError:
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
    try:
        ts = {
            k: parse_datetime(fold[k], k)
            for k in ("train_start", "train_end", "test_start", "test_end")
        }
    except (KeyError, TypeError, ValueError, InvalidParameter):
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
