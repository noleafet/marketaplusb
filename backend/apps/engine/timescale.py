"""Postgres-backed candle, funding, and equity persistence."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any, Iterable

from django.db import connection

from apps.common.binance import INTERVAL_MS


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def upsert_candles(symbol: str, interval: str, rows: Iterable[dict[str, Any]]) -> int:
    values = [
        (
            _aware(row["time"]),
            symbol,
            interval,
            row["open"],
            row["high"],
            row["low"],
            row["close"],
            row["volume"],
            row["quote_volume"],
            row["trades"],
            row.get("taker_buy_base", 0.0),
        )
        for row in rows
    ]
    if not values:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO market_candles
                (time, symbol, interval, open, high, low, close, volume,
                 quote_volume, trades, taker_buy_base)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, interval, time) DO UPDATE SET
                open = EXCLUDED.open,
                high = EXCLUDED.high,
                low = EXCLUDED.low,
                close = EXCLUDED.close,
                volume = EXCLUDED.volume,
                quote_volume = EXCLUDED.quote_volume,
                trades = EXCLUDED.trades,
                taker_buy_base = EXCLUDED.taker_buy_base
            """,
            values,
        )
    return len(values)


def latest_candle_time(symbol: str, interval: str) -> datetime | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT MAX(time) FROM market_candles WHERE symbol = %s AND interval = %s",
            [symbol, interval],
        )
        value = cursor.fetchone()[0]
    return _aware(value) if value else None


def load_candles(
    symbol: str, interval: str, start: datetime, end: datetime
) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT time, open, high, low, close, volume, quote_volume, trades
            FROM market_candles
            WHERE symbol = %s AND interval = %s AND time >= %s AND time <= %s
            ORDER BY time ASC
            """,
            [symbol, interval, _aware(start), _aware(end)],
        )
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for row in rows:
        row["time"] = _aware(row["time"])
        for key in ("open", "high", "low", "close", "volume", "quote_volume"):
            row[key] = float(row[key])
    return rows


def upsert_funding(symbol: str, rows: Iterable[dict[str, Any]]) -> int:
    values = [
        (_aware(row["time"]), symbol, row["rate"], row.get("mark_price"))
        for row in rows
    ]
    if not values:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO funding_rates (time, symbol, rate, mark_price)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (symbol, time) DO UPDATE SET
                rate = EXCLUDED.rate,
                mark_price = EXCLUDED.mark_price
            """,
            values,
        )
    return len(values)


def latest_funding_time(symbol: str) -> datetime | None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT MAX(time) FROM funding_rates WHERE symbol = %s", [symbol])
        value = cursor.fetchone()[0]
    return _aware(value) if value else None


def load_funding(symbol: str, start: datetime, end: datetime) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT time, rate, mark_price FROM funding_rates
            WHERE symbol = %s AND time >= %s AND time <= %s
            ORDER BY time ASC
            """,
            [symbol, _aware(start), _aware(end)],
        )
        rows = [{"time": _aware(r[0]), "rate": float(r[1]), "mark_price": r[2]} for r in cursor.fetchall()]
    return rows


def page_candles(
    symbol: str,
    interval: str,
    lower: datetime | None,
    upper: datetime,
    after: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Ascending candles with ``lower <= time <= upper`` and ``time > after``."""
    sql = [
        "SELECT time, open, high, low, close, volume, quote_volume, trades",
        "FROM market_candles WHERE symbol = %s AND interval = %s AND time <= %s",
    ]
    params: list[Any] = [symbol, interval, _aware(upper)]
    if lower is not None:
        sql.append("AND time >= %s")
        params.append(_aware(lower))
    if after is not None:
        sql.append("AND time > %s")
        params.append(_aware(after))
    sql.append("ORDER BY time ASC LIMIT %s")
    params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(" ".join(sql), params)
        columns = [c[0] for c in cursor.description]
        rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
    for row in rows:
        row["time"] = _aware(row["time"])
        for key in ("open", "high", "low", "close", "volume", "quote_volume"):
            row[key] = float(row[key])
    return rows


def page_funding(
    symbol: str,
    lower: datetime | None,
    upper: datetime,
    after: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Ascending funding rows with ``lower <= time <= upper`` and ``time > after``."""
    sql = ["SELECT time, rate, mark_price FROM funding_rates WHERE symbol = %s AND time <= %s"]
    params: list[Any] = [symbol, _aware(upper)]
    if lower is not None:
        sql.append("AND time >= %s")
        params.append(_aware(lower))
    if after is not None:
        sql.append("AND time > %s")
        params.append(_aware(after))
    sql.append("ORDER BY time ASC LIMIT %s")
    params.append(limit)
    with connection.cursor() as cursor:
        cursor.execute(" ".join(sql), params)
        return [
            {
                "time": _aware(r[0]),
                "rate": float(r[1]),
                "mark_price": None if r[2] is None else float(r[2]),
            }
            for r in cursor.fetchall()
        ]


def page_equity(
    session_id: str,
    lower: datetime | None,
    upper: datetime,
    after: datetime | None,
    limit: int,
) -> list[dict[str, Any]]:
    """Ascending equity rows with ``lower <= time <= upper`` and ``time > after``."""
    sql = [
        "SELECT time, equity, price, position_qty, drawdown FROM paper_equity_points"
        " WHERE session_id = %s AND time <= %s"
    ]
    params: list[Any] = [str(session_id), _aware(upper)]
    if lower is not None:
        sql.append("AND time >= %s")
        params.append(_aware(lower))
    if after is not None:
        sql.append("AND time > %s")
        params.append(_aware(after))
    sql.append("ORDER BY time ASC LIMIT %s")
    params.append(limit)

    def _f(v):
        return None if v is None else float(v)

    with connection.cursor() as cursor:
        cursor.execute(" ".join(sql), params)
        return [
            {
                "time": _aware(r[0]),
                "equity": _f(r[1]),
                "price": _f(r[2]),
                "position_qty": _f(r[3]),
                "drawdown": _f(r[4]),
            }
            for r in cursor.fetchall()
        ]


def insert_equity_points(rows: Iterable[dict[str, Any]]) -> int:
    values = [
        (
            _aware(row["time"]),
            str(row["session_id"]),
            row["equity"],
            row.get("price"),
            row.get("position_qty"),
            row.get("drawdown"),
        )
        for row in rows
    ]
    if not values:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO paper_equity_points
                (time, session_id, equity, price, position_qty, drawdown)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (session_id, time) DO UPDATE SET
                equity = EXCLUDED.equity,
                price = EXCLUDED.price,
                position_qty = EXCLUDED.position_qty,
                drawdown = EXCLUDED.drawdown
            """,
            values,
        )
    return len(values)


def load_equity_points(session_id: str, limit: int = 5000) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT time, equity, price, position_qty, drawdown
            FROM paper_equity_points
            WHERE session_id = %s
            ORDER BY time ASC
            LIMIT %s
            """,
            [str(session_id), limit],
        )
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]


def load_daily_equity(session_id: str, days: int = 120) -> dict[str, float]:
    """Last equity point of each UTC day for one paper session."""

    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT DISTINCT ON (day) day, equity
            FROM (
                SELECT (time AT TIME ZONE 'UTC')::date AS day, time, equity
                FROM paper_equity_points
                WHERE session_id = %s AND time >= NOW() - (%s || ' days')::interval
            ) AS recent
            ORDER BY day, time DESC
            """,
            [str(session_id), int(days)],
        )
        return {row[0].isoformat(): float(row[1]) for row in cursor.fetchall()}


def expected_bars(interval: str, start: datetime, end: datetime) -> int:
    step = INTERVAL_MS[interval] / 1000
    return max(0, int((end - start).total_seconds() // step))
