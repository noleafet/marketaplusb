import json
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.coins.models import Coin
from apps.engine.analysis import base_grid_params, engine_config
from apps.engine.compare import compare_grid_variants
from apps.engine.timescale import load_candles, load_funding


class Command(BaseCommand):
    help = (
        "Compare grid level, ATR pad, rebalance mode, range construction, and "
        "portfolio drawdown limits on one holdout window."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "symbol", help="Coin symbol or Binance symbol (e.g. BTC or BTCUSDT)"
        )
        parser.add_argument(
            "--holdout-days",
            type=int,
            default=90,
            help="Length of the untouched holdout window (default 90).",
        )
        parser.add_argument(
            "--lookback-days",
            type=int,
            default=int(settings.ENGINE["LOOKBACK_DAYS"]),
            help="Stored candle history to load (default ENGINE_LOOKBACK_DAYS).",
        )
        parser.add_argument(
            "--output", help="Optional path to write the JSON report instead of stdout."
        )

    def handle(self, *args, **options):
        symbol = options["symbol"].upper()
        coin = (
            Coin.objects.filter(symbol__iexact=symbol).first()
            or Coin.objects.filter(binance_symbol__iexact=symbol).first()
        )
        if coin is None:
            raise CommandError(f"Coin {symbol} not found")
        if not coin.binance_symbol:
            raise CommandError(f"{coin.symbol} has no Binance futures symbol")

        interval = str(settings.ENGINE["INTERVAL"])
        end = timezone.now()
        start = end - timedelta(days=max(120, int(options["lookback_days"])))
        rows = load_candles(coin.binance_symbol, interval, start, end)
        if len(rows) < 200:
            raise CommandError(
                f"only {len(rows)} stored {interval} candles for {coin.binance_symbol}; "
                "run the daily analysis first"
            )
        funding = load_funding(coin.binance_symbol, start, end)
        report = compare_grid_variants(
            rows,
            funding,
            config=engine_config(),
            base_params=base_grid_params(),
            holdout_days=max(1, int(options["holdout_days"])),
        )
        if report.get("error"):
            raise CommandError(report["error"])
        report["symbol"] = coin.binance_symbol
        report["interval"] = interval
        payload = json.dumps(report, indent=2, default=str)
        if options.get("output"):
            path = Path(options["output"])
            path.write_text(payload, encoding="utf-8")
            self.stdout.write(f"Wrote comparison report to {path}")
        else:
            self.stdout.write(payload)
