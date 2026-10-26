"""Create the engine's time-series tables and optional Timescale hypertables."""

import logging

from django.conf import settings
from django.db import DatabaseError, migrations, transaction

logger = logging.getLogger("market")

TABLES = [
    "coin_market_snapshots",
    "market_candles",
    "funding_rates",
    "paper_equity_points",
]

CREATE_SQL = [
    """
    CREATE TABLE IF NOT EXISTS coin_market_snapshots (
        time timestamptz NOT NULL,
        symbol varchar(40) NOT NULL,
        coingecko_id varchar(160) NOT NULL DEFAULT '',
        rank integer,
        market_cap_usd numeric(30,2),
        source_last_updated timestamptz,
        PRIMARY KEY (symbol, time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_candles (
        time timestamptz NOT NULL,
        symbol varchar(40) NOT NULL,
        interval varchar(8) NOT NULL,
        open double precision NOT NULL,
        high double precision NOT NULL,
        low double precision NOT NULL,
        close double precision NOT NULL,
        volume double precision NOT NULL DEFAULT 0,
        quote_volume double precision NOT NULL DEFAULT 0,
        trades integer NOT NULL DEFAULT 0,
        taker_buy_base double precision NOT NULL DEFAULT 0,
        PRIMARY KEY (symbol, interval, time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS funding_rates (
        time timestamptz NOT NULL,
        symbol varchar(40) NOT NULL,
        rate double precision NOT NULL,
        mark_price double precision,
        PRIMARY KEY (symbol, time)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS paper_equity_points (
        time timestamptz NOT NULL,
        session_id varchar(64) NOT NULL,
        equity double precision NOT NULL,
        price double precision,
        position_qty double precision,
        drawdown double precision,
        PRIMARY KEY (session_id, time)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_market_candles_symbol ON market_candles (symbol, interval)",
    "CREATE INDEX IF NOT EXISTS idx_funding_symbol ON funding_rates (symbol)",
    "CREATE INDEX IF NOT EXISTS idx_equity_session ON paper_equity_points (session_id)",
]


def _enable_hypertables(cursor) -> None:
    try:
        with transaction.atomic():
            cursor.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
    except DatabaseError:
        logger.warning(
            "TimescaleDB extension unavailable; time-series tables remain regular Postgres tables."
        )
        return
    for table in TABLES:
        try:
            with transaction.atomic():
                cursor.execute(
                    "SELECT create_hypertable(%s, 'time', if_not_exists => TRUE, migrate_data => TRUE)",
                    [table],
                )
        except DatabaseError:
            logger.warning("Could not convert %s into a Timescale hypertable", table)


def create_timeseries_tables(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for statement in CREATE_SQL:
            cursor.execute(statement)
        if settings.TIMESCALE_ENABLED:
            _enable_hypertables(cursor)
        else:
            logger.info("TIMESCALE_ENABLED=false; keeping regular Postgres tables")


def drop_timeseries_tables(apps, schema_editor):
    with schema_editor.connection.cursor() as cursor:
        for table in reversed(TABLES):
            cursor.execute(f"DROP TABLE IF EXISTS {table} CASCADE")


class Migration(migrations.Migration):
    dependencies = [
        ("engine", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(create_timeseries_tables, drop_timeseries_tables),
    ]
