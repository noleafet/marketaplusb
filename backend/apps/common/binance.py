"""Binance USDⓈ-M Futures public market data and signed-request primitives."""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Iterable
from urllib.parse import urlencode

import requests
from django.conf import settings

logger = logging.getLogger("market")

INTERVAL_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}

MAX_KLINES_PER_REQUEST = 1500
MAX_FUNDING_PER_REQUEST = 1000


class BinanceError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, code: int | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class BinanceFuturesClient:
    """Thin, rate-limit aware client for public and (guarded) signed endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        session: requests.Session | None = None,
        timeout: int = 20,
    ) -> None:
        self.base_url = (base_url or settings.BINANCE_FUTURES_BASE_URL).rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.session.headers.update(
            {"accept": "application/json", "user-agent": "grid-suitability-engine/1.0"}
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        signed: bool = False,
        max_attempts: int = 4,
    ) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            api_key = settings.LIVE.get("API_KEY", "")
            api_secret = settings.LIVE.get("API_SECRET", "")
            if not api_key or not api_secret:
                raise BinanceError("Binance API credentials are not configured")
            params["timestamp"] = int(time.time() * 1000)
            params["recvWindow"] = 5000
            query = urlencode(params, doseq=True)
            signature = hmac.new(
                api_secret.encode(), query.encode(), hashlib.sha256
            ).hexdigest()
            url = f"{self.base_url}{path}?{query}&signature={signature}"
            headers = {"X-MBX-APIKEY": api_key}
        else:
            url = f"{self.base_url}{path}"
            headers = {}

        last_error: Exception | None = None
        for attempt in range(max_attempts):
            try:
                response = self.session.request(
                    method, url, params=None if signed else params, headers=headers, timeout=self.timeout
                )
                if response.status_code in (418, 429):
                    retry_after = float(response.headers.get("Retry-After", 2 * (attempt + 1)))
                    wait = min(60.0, max(1.0, retry_after))
                    logger.warning("Binance rate limit (%s); sleeping %.1fs", response.status_code, wait)
                    time.sleep(wait)
                    continue
                if response.status_code >= 500:
                    time.sleep(1.5 * (attempt + 1))
                    last_error = BinanceError(
                        f"Binance server error {response.status_code}", response.status_code
                    )
                    continue
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise BinanceError(
                        f"Invalid JSON from Binance ({response.status_code})", response.status_code
                    ) from exc
                if response.status_code >= 400:
                    code = payload.get("code") if isinstance(payload, dict) else None
                    message = payload.get("msg") if isinstance(payload, dict) else str(payload)
                    raise BinanceError(
                        f"Binance error {code}: {message}", response.status_code, code
                    )
                return payload
            except (requests.RequestException, BinanceError) as exc:
                last_error = exc
                if isinstance(exc, BinanceError) and exc.status_code and 400 <= exc.status_code < 500:
                    raise
                time.sleep(1.5 * (attempt + 1))
        raise BinanceError(f"Binance request failed: {last_error}")

    # ------------------------------------------------------------ public
    def ping(self) -> bool:
        self._request("GET", "/fapi/v1/ping")
        return True

    def exchange_info(self) -> dict[str, Any]:
        return self._request("GET", "/fapi/v1/exchangeInfo")

    def klines(
        self,
        symbol: str,
        interval: str,
        *,
        start_ms: int | None = None,
        end_ms: int | None = None,
        limit: int = 1500,
    ) -> list[list[Any]]:
        if interval not in INTERVAL_MS:
            raise ValueError(f"Unsupported Binance interval: {interval}")
        limit = min(max(1, limit), MAX_KLINES_PER_REQUEST)
        payload = self._request(
            "GET",
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            },
        )
        if not isinstance(payload, list):
            raise BinanceError("Unexpected klines response")
        return payload

    def klines_range(
        self,
        symbol: str,
        interval: str,
        start: datetime,
        end: datetime,
        *,
        max_requests: int = 60,
    ) -> list[dict[str, Any]]:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        step = INTERVAL_MS[interval]
        cursor = start_ms
        rows: list[dict[str, Any]] = []
        requests_made = 0
        while cursor <= end_ms and requests_made < max_requests:
            batch = self.klines(
                symbol,
                interval,
                start_ms=cursor,
                end_ms=end_ms,
                limit=MAX_KLINES_PER_REQUEST,
            )
            requests_made += 1
            if not batch:
                break
            for k in batch:
                rows.append(
                    {
                        "time": datetime.fromtimestamp(k[0] / 1000, tz=dt_timezone.utc),
                        "open": float(k[1]),
                        "high": float(k[2]),
                        "low": float(k[3]),
                        "close": float(k[4]),
                        "volume": float(k[5]),
                        "quote_volume": float(k[7]),
                        "trades": int(k[8]),
                        "taker_buy_base": float(k[9]),
                    }
                )
            last_open = int(batch[-1][0])
            next_cursor = last_open + step
            if next_cursor <= cursor:
                break
            cursor = next_cursor
            if len(batch) < MAX_KLINES_PER_REQUEST:
                break
            time.sleep(0.12)
        return rows

    def funding_rates(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        max_requests: int = 30,
    ) -> list[dict[str, Any]]:
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        cursor = start_ms
        rows: list[dict[str, Any]] = []
        requests_made = 0
        while cursor <= end_ms and requests_made < max_requests:
            payload = self._request(
                "GET",
                "/fapi/v1/fundingRate",
                {
                    "symbol": symbol,
                    "startTime": cursor,
                    "endTime": end_ms,
                    "limit": MAX_FUNDING_PER_REQUEST,
                },
            )
            requests_made += 1
            if not payload:
                break
            for item in payload:
                rows.append(
                    {
                        "time": datetime.fromtimestamp(item["fundingTime"] / 1000, tz=dt_timezone.utc),
                        "rate": float(item["fundingRate"]),
                        "mark_price": float(item["markPrice"]) if item.get("markPrice") else None,
                    }
                )
            last_time = int(payload[-1]["fundingTime"])
            if last_time < cursor:
                break
            cursor = last_time + 1
            if len(payload) < MAX_FUNDING_PER_REQUEST:
                break
            time.sleep(0.12)
        return rows

    def ticker_24h(self, symbol: str | None = None) -> Any:
        return self._request("GET", "/fapi/v1/ticker/24h", {"symbol": symbol} if symbol else None)

    def ticker_price(self, symbol: str | None = None) -> Any:
        return self._request("GET", "/fapi/v1/ticker/price", {"symbol": symbol} if symbol else None)

    def premium_index(self, symbol: str | None = None) -> Any:
        return self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol} if symbol else None)

    def depth(self, symbol: str, limit: int = 100) -> dict[str, Any]:
        payload = self._request(
            "GET",
            "/fapi/v1/depth",
            {"symbol": symbol, "limit": min(max(5, limit), 1000)},
        )
        if not isinstance(payload, dict):
            raise BinanceError("Unexpected depth response")
        return payload

    def open_interest_hist(
        self,
        symbol: str,
        period: str = "1h",
        *,
        limit: int = 6,
        start_ms: int | None = None,
        end_ms: int | None = None,
    ) -> list[dict[str, Any]]:
        """Open interest history for `/futures/data/openInterestHist` (public)."""
        payload = self._request(
            "GET",
            "/futures/data/openInterestHist",
            {
                "symbol": symbol,
                "period": period,
                "limit": min(max(1, limit), 500),
                "startTime": start_ms,
                "endTime": end_ms,
            },
        )
        if not isinstance(payload, list):
            raise BinanceError("Unexpected open interest response")
        return [
            {
                "time": datetime.fromtimestamp(float(item["timestamp"]) / 1000, tz=dt_timezone.utc),
                "open_interest": float(item["sumOpenInterest"]),
                "open_interest_value": float(item["sumOpenInterestValue"]),
            }
            for item in payload
        ]

    def server_time(self) -> datetime:
        payload = self._request("GET", "/fapi/v1/time")
        return datetime.fromtimestamp(payload["serverTime"] / 1000, tz=dt_timezone.utc)

    # ------------------------------------------------------------ signed
    def signed(self, method: str, path: str, params: dict[str, Any] | None = None) -> Any:
        """Execute a signed request. Callers must enforce live-trading guards."""
        return self._request(method, path, params, signed=True)

    def live_base_url(self) -> str:
        if settings.LIVE.get("TESTNET"):
            return "https://testnet.binancefuture.com"
        return "https://fapi.binance.com"


def chunked(items: Iterable[Any], size: int) -> Iterable[list[Any]]:
    batch: list[Any] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


def utc_now() -> datetime:
    return datetime.now(tz=dt_timezone.utc)


def floor_to_interval(moment: datetime, interval: str) -> datetime:
    step = INTERVAL_MS[interval] / 1000
    epoch = moment.timestamp()
    return datetime.fromtimestamp(epoch - (epoch % step), tz=dt_timezone.utc)


def default_history_start(lookback_days: int, interval: str) -> datetime:
    return floor_to_interval(utc_now() - timedelta(days=lookback_days), interval)
