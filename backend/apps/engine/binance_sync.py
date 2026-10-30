"""Incremental sync of Binance futures candles and funding into Postgres."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from apps.common.binance import INTERVAL_MS, BinanceFuturesClient, floor_to_interval
from apps.engine.timescale import (
    latest_candle_time,
    latest_funding_time,
    upsert_candles,
    upsert_funding,
)
from apps.coins.models import Coin

logger = logging.getLogger("market")


def sync_coin_market_data(
    coin: Coin,
    client: BinanceFuturesClient,
    *,
    lookback_days: int,
    interval: str,
    warmup_days: int = 45,
    force: bool = False,
    funding: bool = True,
) -> dict[str, Any]:
    symbol = coin.binance_symbol
    if not symbol:
        return {"ok": False, "error": "no Binance symbol mapped"}

    now = datetime.now(tz=dt_timezone.utc)
    step = timedelta(milliseconds=INTERVAL_MS[interval])
    end = floor_to_interval(now, interval) - timedelta(milliseconds=1)
    start = end - timedelta(days=lookback_days + warmup_days)

    latest = latest_candle_time(symbol, interval)
    candles_added = 0
    if force or latest is None or latest < end - step:
        fetch_start = max(start, latest + step) if latest else start
        if force:
            fetch_start = start
        rows = client.klines_range(symbol, interval, fetch_start, end)
        candles_added = upsert_candles(symbol, interval, rows)

    funding_added = 0
    if funding:
        funding_latest = latest_funding_time(symbol)
        funding_start = (
            max(start, funding_latest + timedelta(seconds=1)) if funding_latest else start
        )
        if force:
            funding_start = start
        if funding_start <= now:
            funding_rows = client.funding_rates(symbol, funding_start, now)
            funding_added = upsert_funding(symbol, funding_rows)

    return {
        "ok": True,
        "symbol": symbol,
        "candles_added": candles_added,
        "funding_added": funding_added,
        "requested_start": start.isoformat(),
        "end": end.isoformat(),
    }
