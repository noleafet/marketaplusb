"""Time-series writes/reads backed by Postgres tables."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone
from typing import Any, Iterable

from django.db import connection


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc)


def insert_market_snapshots(rows: Iterable[dict[str, Any]]) -> int:
    """Insert monthly market-cap observations.

    The physical table is keyed by (symbol, time).
    """

    values = [
        (
            _aware(row["time"]),
            row["symbol"],
            row.get("coingecko_id", ""),
            row.get("rank"),
            row.get("market_cap_usd"),
            _aware(row["source_last_updated"]) if row.get("source_last_updated") else None,
        )
        for row in rows
    ]
    if not values:
        return 0
    with connection.cursor() as cursor:
        cursor.executemany(
            """
            INSERT INTO coin_market_snapshots
                (time, symbol, coingecko_id, rank, market_cap_usd, source_last_updated)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (symbol, time) DO UPDATE SET
                rank = EXCLUDED.rank,
                market_cap_usd = EXCLUDED.market_cap_usd,
                source_last_updated = EXCLUDED.source_last_updated
            """,
            values,
        )
    return len(values)


def market_snapshots(symbol: str, limit: int = 24) -> list[dict[str, Any]]:
    with connection.cursor() as cursor:
        cursor.execute(
            """
            SELECT time, symbol, rank, market_cap_usd, source_last_updated
            FROM coin_market_snapshots
            WHERE symbol = %s
            ORDER BY time DESC
            LIMIT %s
            """,
            [symbol, limit],
        )
        columns = [c[0] for c in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
