import re

from rest_framework import serializers

from apps.coins.models import Coin


class CoinAddSerializer(serializers.Serializer):
    symbol = serializers.CharField(max_length=15)
    name = serializers.CharField(max_length=160, required=False, allow_blank=True, default="")

    def validate_symbol(self, value):
        symbol = value.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,15}", symbol):
            raise serializers.ValidationError("Use 2-15 letters or digits, e.g. BTC.")
        return symbol


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
