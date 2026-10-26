"""CoinGecko top-100 ingestion and Binance eligibility mapping."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone as dt_timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.binance import BinanceError, BinanceFuturesClient
from apps.coins.models import Coin
from apps.coins.services.coingecko import CoinGeckoClient, MarketDataError
from apps.coins.services.timescale import insert_market_snapshots

logger = logging.getLogger("market")

STABLECOINS = {
    "USDT", "USDC", "DAI", "BUSD", "TUSD", "USDP", "FDUSD", "PYUSD", "USDE", "USDS",
    "EURC", "EURS", "EURT", "USD1", "RLUSD", "USDG", "GUSD", "FRAX", "LUSD", "USTC",
    "PAXG", "XAUT",
}

_MULTIPLIER_RE = re.compile(r"^(1M|1000000|100000|10000|1000)(.+)$")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value)).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def _base_asset_underlying(base_asset: str) -> tuple[str, float]:
    """Map multiplier contracts (1000PEPE, 1MBABYDOGE) back to the base symbol."""
    match = _MULTIPLIER_RE.match(base_asset)
    if not match:
        return base_asset, 1.0
    prefix, rest = match.groups()
    multiplier = {"1M": 1_000_000.0, "1000000": 1_000_000.0}.get(prefix)
    if multiplier is None:
        multiplier = float(prefix)
    return rest, multiplier


def _contract_map(exchange_info: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    mapping: dict[str, list[dict[str, Any]]] = {}
    for item in exchange_info.get("symbols", []):
        if item.get("contractType") != "PERPETUAL" or item.get("quoteAsset") != "USDT":
            continue
        if item.get("status") != "TRADING":
            continue
        base = item.get("baseAsset", "")
        underlying, multiplier = _base_asset_underlying(base)
        record = {
            "symbol": item["symbol"],
            "base_asset": base,
            "underlying": underlying,
            "multiplier": multiplier,
            "onboard_date": item.get("onboardDate"),
            "price_precision": item.get("pricePrecision"),
            "quantity_precision": item.get("quantityPrecision"),
        }
        mapping.setdefault(underlying.upper(), []).append(record)
    return mapping


def _pick_contract(candidates: list[dict[str, Any]], symbol: str) -> dict[str, Any]:
    exact = [c for c in candidates if c["symbol"] == f"{symbol}USDT"]
    if exact:
        return exact[0]
    direct = [c for c in candidates if c["base_asset"] == symbol]
    if direct:
        return direct[0]
    return candidates[0]


def refresh_binance_eligibility(coins: list[Coin]) -> dict[str, Any]:
    client = BinanceFuturesClient()
    try:
        exchange_info = client.exchange_info()
    except BinanceError as exc:
        logger.warning("Binance eligibility refresh failed: %s", exc)
        return {"ok": False, "error": str(exc), "eligible": 0}

    contracts = _contract_map(exchange_info)
    eligible = 0
    excluded = 0
    for coin in coins:
        symbol = coin.symbol.upper()
        coin.is_stablecoin = symbol in STABLECOINS
        candidates = contracts.get(symbol, [])
        if coin.is_stablecoin:
            coin.binance_symbol = ""
            coin.binance_contract_multiplier = 1.0
            coin.analysis_eligible = False
            coin.analysis_exclusion_reason = "stablecoin / low-volatility asset"
            excluded += 1
        elif not candidates:
            coin.binance_symbol = ""
            coin.binance_contract_multiplier = 1.0
            coin.analysis_eligible = False
            coin.analysis_exclusion_reason = "no Binance USDⓈ-M USDT perpetual contract"
            excluded += 1
        else:
            contract = _pick_contract(candidates, symbol)
            coin.binance_symbol = contract["symbol"]
            coin.binance_contract_multiplier = contract["multiplier"]
            coin.analysis_eligible = True
            coin.analysis_exclusion_reason = ""
            eligible += 1
        coin.save(
            update_fields=[
                "binance_symbol",
                "binance_contract_multiplier",
                "analysis_eligible",
                "analysis_exclusion_reason",
                "is_stablecoin",
            ]
        )
    return {"ok": True, "eligible": eligible, "excluded": excluded}


def refresh_top_coins(per_page: int = 100) -> dict[str, Any]:
    """Fetch the current top-100 and apply non-destructive upserts.

    Existing rows are updated in place. Coins that dropped out are retained and
    flagged ``in_top100=False``; nothing is deleted.
    """

    started = timezone.now()
    client = CoinGeckoClient()
    try:
        markets = client.top_markets(per_page=per_page)
    except MarketDataError as exc:
        logger.exception("Top-coin refresh failed")
        return {"ok": False, "error": str(exc), "finished_at": started.isoformat()}

    observed_at = timezone.now()
    created: list[str] = []
    updated = 0
    snapshots: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    top_coins: list[Coin] = []

    with transaction.atomic():
        for rank, item in enumerate(markets, start=1):
            cg_id = str(item.get("id") or "").strip()
            symbol = str(item.get("symbol") or "").strip().upper()
            name = str(item.get("name") or "").strip()
            if not cg_id or not symbol or not name:
                continue
            seen_ids.add(cg_id)
            market_cap = _decimal(item.get("market_cap"))
            source_updated = _parse_timestamp(item.get("last_updated"))
            defaults = {
                "symbol": symbol,
                "name": name,
                "rank": item.get("market_cap_rank") or rank,
                "market_cap_usd": market_cap,
                "source_last_updated": source_updated,
                "in_top100": True,
                "last_seen_in_top100_at": observed_at,
                "last_updated_at": observed_at,
            }
            coin, was_created = Coin.objects.update_or_create(
                coingecko_id=cg_id, defaults=defaults
            )
            if was_created:
                created.append(cg_id)
            else:
                updated += 1
            top_coins.append(coin)
            snapshots.append(
                {
                    "time": observed_at,
                    "symbol": symbol,
                    "coingecko_id": cg_id,
                    "rank": defaults["rank"],
                    "market_cap_usd": market_cap,
                    "source_last_updated": source_updated,
                }
            )

        dropped_qs = Coin.objects.filter(in_top100=True).exclude(coingecko_id__in=seen_ids)
        dropped = list(dropped_qs.values_list("coingecko_id", flat=True))
        dropped_qs.update(in_top100=False, last_updated_at=observed_at)

    snap_count = insert_market_snapshots(snapshots)
    eligibility = refresh_binance_eligibility(top_coins)

    summary = {
        "ok": True,
        "fetched": len(markets),
        "created": len(created),
        "updated": updated,
        "dropped_from_top100": dropped,
        "snapshots": snap_count,
        "eligible_for_analysis": eligibility.get("eligible", 0),
        "excluded": eligibility.get("excluded", 0),
        "binance_mapping_ok": eligibility.get("ok", False),
        "binance_mapping_error": eligibility.get("error"),
        "observed_at": observed_at.isoformat(),
        "duration_seconds": (timezone.now() - started).total_seconds(),
    }
    logger.info(
        "Top-100 refresh complete: %s fetched, %s new, %s dropped, %s eligible",
        summary["fetched"],
        summary["created"],
        len(dropped),
        summary["eligible_for_analysis"],
    )
    return summary
