from rest_framework import serializers

from apps.coins.models import Coin


class CoinSerializer(serializers.ModelSerializer):
    class Meta:
        model = Coin
        fields = [
            "id",
            "coingecko_id",
            "symbol",
            "name",
            "rank",
            "market_cap_usd",
            "source_last_updated",
            "binance_symbol",
            "binance_contract_multiplier",
            "analysis_eligible",
            "analysis_exclusion_reason",
            "is_stablecoin",
            "in_top100",
            "first_seen_at",
            "last_seen_in_top100_at",
            "last_updated_at",
        ]
