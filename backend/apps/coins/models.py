from django.utils import timezone
from django.db import models


class Coin(models.Model):
    """A coin observed in the monthly top-100 market-cap snapshot.

    Rows are never deleted by ingestion. When a coin drops out of the top 100
    it is retained with ``in_top100=False`` so its history stays queryable.
    """

    coingecko_id = models.CharField(max_length=160, unique=True)
    symbol = models.CharField(max_length=40, db_index=True)
    name = models.CharField(max_length=160)
    rank = models.PositiveIntegerField(null=True, blank=True, db_index=True)
    market_cap_usd = models.DecimalField(max_digits=30, decimal_places=2, null=True, blank=True)
    source_last_updated = models.DateTimeField(null=True, blank=True)

    binance_symbol = models.CharField(max_length=40, blank=True, default="")
    binance_contract_multiplier = models.FloatField(default=1.0)
    analysis_eligible = models.BooleanField(default=False, db_index=True)
    analysis_exclusion_reason = models.CharField(max_length=255, blank=True, default="")
    is_stablecoin = models.BooleanField(default=False)

    in_top100 = models.BooleanField(default=False, db_index=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_in_top100_at = models.DateTimeField(null=True, blank=True)
    last_updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["rank", "symbol"]
        indexes = [models.Index(fields=["in_top100", "rank"])]

    def __str__(self) -> str:
        return f"{self.symbol} ({self.name})"
