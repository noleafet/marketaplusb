"""CoinGecko public market data client."""

from __future__ import annotations

import logging
import time
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger("market")

COINGECKO_PAGE_SIZE = 100


class MarketDataError(RuntimeError):
    pass


class CoinGeckoClient:
    def __init__(self, session: requests.Session | None = None) -> None:
        self.session = session or requests.Session()
        self.base_url = settings.COINGECKO_BASE_URL.rstrip("/")

    @property
    def headers(self) -> dict[str, str]:
        headers = {"accept": "application/json", "user-agent": "grid-suitability-engine/1.0"}
        if settings.COINGECKO_API_KEY:
            headers["x-cg-demo-api-key"] = settings.COINGECKO_API_KEY
        return headers

    def _get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        last_error: Exception | None = None
        for attempt in range(4):
            try:
                response = self.session.get(url, params=params, headers=self.headers, timeout=25)
                if response.status_code == 429:
                    wait = min(60, 5 * (attempt + 1))
                    logger.warning("CoinGecko rate limit hit; retrying in %ss", wait)
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError) as exc:
                last_error = exc
                time.sleep(2 * (attempt + 1))
        raise MarketDataError(f"CoinGecko request failed: {last_error}")

    def top_markets(self, per_page: int = COINGECKO_PAGE_SIZE) -> list[dict[str, Any]]:
        if not 1 <= per_page <= 250:
            raise ValueError("CoinGecko per_page must be between 1 and 250")
        payload = self._get(
            "/coins/markets",
            {
                "vs_currency": "usd",
                "order": "market_cap_desc",
                "per_page": per_page,
                "page": 1,
                "sparkline": "false",
                "price_change_percentage": "24h",
            },
        )
        if not isinstance(payload, list):
            raise MarketDataError("Unexpected CoinGecko response shape")
        return payload
