from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from apps.coins.models import Coin
from apps.coins.services.ingest import (
    _base_asset_underlying,
    refresh_binance_eligibility,
    refresh_top_coins,
)


class ContractMultiplierTests(TestCase):
    def test_multiplier_contracts_map_to_underlying(self):
        self.assertEqual(_base_asset_underlying("1000PEPE"), ("PEPE", 1000.0))
        self.assertEqual(_base_asset_underlying("1MBABYDOGE"), ("BABYDOGE", 1_000_000.0))
        self.assertEqual(_base_asset_underlying("BTC"), ("BTC", 1.0))


class TopCoinRefreshTests(TestCase):
    @patch("apps.coins.services.ingest.insert_market_snapshots", return_value=1)
    @patch("apps.coins.services.ingest.refresh_binance_eligibility")
    @patch("apps.coins.services.ingest.CoinGeckoClient")
    def test_refresh_updates_existing_and_never_deletes(
        self, client_cls, eligibility, snapshots
    ):
        eligibility.return_value = {"ok": True, "eligible": 1, "excluded": 0}
        existing = Coin.objects.create(
            coingecko_id="bitcoin",
            symbol="BTC",
            name="Bitcoin",
            market_cap_usd=Decimal("1.00"),
            in_top100=True,
            rank=1,
        )
        dropped = Coin.objects.create(
            coingecko_id="old-coin",
            symbol="OLD",
            name="Old Coin",
            market_cap_usd=Decimal("10.00"),
            in_top100=True,
            rank=99,
        )
        client_cls.return_value.top_markets.return_value = [
            {
                "id": "bitcoin",
                "symbol": "btc",
                "name": "Bitcoin",
                "market_cap": 1_608_072_093_975,
                "market_cap_rank": 1,
                "last_updated": "2026-10-01T00:00:00Z",
            }
        ]

        summary = refresh_top_coins()

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["updated"], 1)
        self.assertEqual(summary["created"], 0)
        self.assertEqual(summary["dropped_from_top100"], ["old-coin"])
        existing.refresh_from_db()
        dropped.refresh_from_db()
        self.assertEqual(existing.market_cap_usd, Decimal("1608072093975.00"))
        self.assertTrue(existing.in_top100)
        self.assertIsNotNone(existing.last_seen_in_top100_at)
        self.assertFalse(dropped.in_top100)
        self.assertEqual(Coin.objects.count(), 2)
        snapshots.assert_called_once()
        self.assertEqual(snapshots.call_args.args[0][0]["symbol"], "BTC")


class BinanceEligibilityTests(TestCase):
    @patch("apps.coins.services.ingest.BinanceFuturesClient")
    def test_maps_contracts_and_excludes_unsupported_coins(self, client_cls):
        client_cls.return_value.exchange_info.return_value = {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "onboardDate": 0,
                },
                {
                    "symbol": "1000PEPEUSDT",
                    "baseAsset": "1000PEPE",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "onboardDate": 0,
                },
                {
                    "symbol": "ETHUSDT",
                    "baseAsset": "ETH",
                    "quoteAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "SETTLING",
                    "onboardDate": 0,
                },
            ]
        }
        btc = Coin.objects.create(coingecko_id="bitcoin", symbol="BTC", name="Bitcoin")
        pepe = Coin.objects.create(coingecko_id="pepe", symbol="PEPE", name="Pepe")
        usdc = Coin.objects.create(coingecko_id="usd-coin", symbol="USDC", name="USD Coin")
        missing = Coin.objects.create(coingecko_id="missing", symbol="NOPE", name="No Contract")

        summary = refresh_binance_eligibility([btc, pepe, usdc, missing])

        self.assertTrue(summary["ok"])
        self.assertEqual(summary["eligible"], 2)
        self.assertEqual(summary["excluded"], 2)
        btc.refresh_from_db()
        pepe.refresh_from_db()
        usdc.refresh_from_db()
        missing.refresh_from_db()
        self.assertEqual(btc.binance_symbol, "BTCUSDT")
        self.assertTrue(btc.analysis_eligible)
        self.assertEqual(pepe.binance_symbol, "1000PEPEUSDT")
        self.assertEqual(pepe.binance_contract_multiplier, 1000.0)
        self.assertTrue(pepe.analysis_eligible)
        self.assertTrue(usdc.is_stablecoin)
        self.assertFalse(usdc.analysis_eligible)
        self.assertIn("stablecoin", usdc.analysis_exclusion_reason)
        self.assertFalse(missing.analysis_eligible)
        self.assertIn("no Binance", missing.analysis_exclusion_reason)
