from datetime import datetime, timedelta, timezone as dt_timezone


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
