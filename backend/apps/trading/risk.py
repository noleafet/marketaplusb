"""Overbought pause/resume guard for paper sessions.

State machine evaluated on every 5-minute paper tick:

    RSI(15m) > RISK_RSI_OVERBOUGHT
        -> armed, trail the local peak high
        -> exit when any of:
           * price falls RISK_TRAILING_STOP_PCT from the peak (fast drop)
           * MACD histogram shrinks N bars AND the 15m close loses VWAP
           * 1h open interest drops sharply while price is near the peak
        -> flatten to USDT and pause the session

    While paused:
        * 1h OI turns back up (or OI data is unavailable past the failsafe)
        * RSI(15m) is above RISK_RSI_RECOVERY and rising
        * the last closed 15m candle is above VWAP
        -> resume and let the next tick rebuild the grid

Only closed 15m candles are used, so the decision for a candle is stable
regardless of when the tick lands inside it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any, Mapping

import numpy as np

from apps.common.binance import BinanceError, BinanceFuturesClient

KLINE_LIMIT = 200


@dataclass(frozen=True)
class RiskSnapshot:
    price: float
    bar_time: datetime | None
    bar_high: float
    close: float
    rsi: float | None
    rsi_prev: float | None
    macd_hist: list[float]
    vwap: float | None
    oi_values: list[float] = field(default_factory=list)
    oi_available: bool = False


@dataclass(frozen=True)
class RiskDecision:
    state: dict[str, Any]
    action: str  # idle | armed | hold | pause | resume
    reason: str = ""


def rsi(closes: np.ndarray, period: int) -> float | None:
    """Wilder RSI on closes; None when there is not enough history."""
    if closes.size < period + 1:
        return None
    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for idx in range(period, deltas.size):
        avg_gain = (avg_gain * (period - 1) + gains[idx]) / period
        avg_loss = (avg_loss * (period - 1) + losses[idx]) / period
    if avg_loss <= 1e-12:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    if values.size == 0:
        return values
    alpha = 2.0 / (period + 1.0)
    out = np.empty(values.size, dtype=float)
    out[0] = values[0]
    for idx in range(1, values.size):
        out[idx] = alpha * values[idx] + (1.0 - alpha) * out[idx - 1]
    return out


def macd_histogram(closes: np.ndarray, fast: int, slow: int, signal: int) -> np.ndarray:
    """MACD histogram using adjust=False EMAs (the convention TA libraries use)."""
    if closes.size < slow + signal:
        return np.array([], dtype=float)
    macd_line = _ema(closes, fast) - _ema(closes, slow)
    signal_line = _ema(macd_line, signal)
    return macd_line - signal_line


def anchored_vwap(bars: list[dict[str, Any]], anchor: datetime) -> float | None:
    """Volume-weighted average of typical price from ``anchor`` to the last bar."""
    total_pv = 0.0
    total_volume = 0.0
    for bar in bars:
        if bar["time"] < anchor:
            continue
        typical = (bar["high"] + bar["low"] + bar["close"]) / 3.0
        volume = float(bar["volume"])
        if volume <= 0:
            continue
        total_pv += typical * volume
        total_volume += volume
    if total_volume <= 0:
        return None
    return total_pv / total_volume


def _as_float(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _oi_change_pct(values: list[float]) -> float | None:
    if len(values) < 3:
        return None
    baseline = max(values[:-1])
    if baseline <= 0:
        return None
    return values[-1] / baseline - 1.0


def fetch_snapshot(
    client: BinanceFuturesClient,
    symbol: str,
    now: datetime,
    config: Mapping[str, Any],
    *,
    price: float | None = None,
    bars: list[dict[str, Any]] | None = None,
    oi_rows: list[dict[str, Any]] | None = None,
) -> RiskSnapshot:
    """Collect closed 15m candles and 1h open interest for one symbol."""

    rsi_period = int(config["RSI_PERIOD"])
    macd_fast = int(config["MACD_FAST"])
    macd_slow = int(config["MACD_SLOW"])
    macd_signal = int(config["MACD_SIGNAL"])
    oi_period = str(config["OI_PERIOD"])
    oi_lookback = int(config["OI_LOOKBACK"])

    if bars is None:
        raw = client.klines(symbol, "15m", limit=KLINE_LIMIT)
        now_ms = int(now.timestamp() * 1000)
        bars = [
            {
                "time": datetime.fromtimestamp(float(k[0]) / 1000, tz=dt_timezone.utc),
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
            }
            for k in raw
            if float(k[6]) < now_ms
        ]

    oi_available = False
    oi_values: list[float] = []
    try:
        rows = (
            oi_rows
            if oi_rows is not None
            else client.open_interest_hist(symbol, oi_period, limit=oi_lookback)
        )
        oi_values = [float(row["open_interest"]) for row in rows[-oi_lookback:]]
        oi_available = bool(oi_values)
    except (BinanceError, KeyError, TypeError, ValueError):
        oi_available = False

    if not bars:
        return RiskSnapshot(
            price=float(price or 0.0),
            bar_time=None,
            bar_high=0.0,
            close=0.0,
            rsi=None,
            rsi_prev=None,
            macd_hist=[],
            vwap=None,
            oi_values=oi_values,
            oi_available=oi_available,
        )

    closes = np.array([bar["close"] for bar in bars], dtype=float)
    last = bars[-1]
    live_price = float(price if price is not None else last["close"])
    rsi_now = rsi(closes, rsi_period)
    rsi_prev = rsi(closes[:-1], rsi_period) if closes.size > rsi_period + 1 else None
    hist = macd_histogram(closes, macd_fast, macd_slow, macd_signal)
    day_start = now.astimezone(dt_timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return RiskSnapshot(
        price=live_price,
        bar_time=last["time"],
        bar_high=last["high"],
        close=last["close"],
        rsi=rsi_now,
        rsi_prev=rsi_prev,
        macd_hist=[float(x) for x in hist[-4:]],
        vwap=anchored_vwap(bars, day_start),
        oi_values=oi_values,
        oi_available=oi_available,
    )


def _metrics(snapshot: RiskSnapshot, peak: float | None) -> dict[str, Any]:
    drawdown = (
        1.0 - snapshot.price / peak if peak and peak > 0 and snapshot.price > 0 else None
    )
    change = _oi_change_pct(snapshot.oi_values)
    return {
        "price": round(snapshot.price, 10),
        "bar_time": snapshot.bar_time.isoformat() if snapshot.bar_time else None,
        "rsi_15m": round(snapshot.rsi, 2) if snapshot.rsi is not None else None,
        "macd_hist_15m": [round(x, 10) for x in snapshot.macd_hist],
        "vwap_15m": round(snapshot.vwap, 10) if snapshot.vwap is not None else None,
        "peak_high": round(peak, 10) if peak else None,
        "drawdown_pct": round(drawdown, 6) if drawdown is not None else None,
        "oi_change_pct": round(change, 6) if change is not None else None,
        "oi_available": snapshot.oi_available,
    }


def _momentum_decay(snapshot: RiskSnapshot, shrink_bars: int) -> bool:
    if snapshot.vwap is None or snapshot.close <= 0 or snapshot.close >= snapshot.vwap:
        return False
    hist = snapshot.macd_hist
    if len(hist) < shrink_bars + 1:
        return False
    tail = hist[-(shrink_bars + 1) :]
    return all(tail[idx] < tail[idx - 1] for idx in range(1, len(tail)))


def _oi_flush(
    snapshot: RiskSnapshot, peak: float | None, config: Mapping[str, Any]
) -> bool:
    if not snapshot.oi_available or not peak or peak <= 0:
        return False
    change = _oi_change_pct(snapshot.oi_values)
    if change is None:
        return False
    near_high = snapshot.price >= peak * (1.0 - float(config["OI_NEAR_HIGH_PCT"]))
    return near_high and change <= -float(config["OI_DROP_PCT"])


def _exit_reason(
    snapshot: RiskSnapshot, peak: float, config: Mapping[str, Any]
) -> str:
    drawdown = 1.0 - snapshot.price / peak if peak > 0 else 0.0
    if drawdown >= float(config["TRAILING_STOP_PCT"]):
        return "trailing_drawdown"
    if _momentum_decay(snapshot, int(config["MACD_SHRINK_BARS"])):
        return "momentum_decay"
    if _oi_flush(snapshot, peak, config):
        return "oi_flush"
    return ""


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


def evaluate(
    state: Mapping[str, Any] | None,
    snapshot: RiskSnapshot,
    config: Mapping[str, Any],
    now: datetime,
) -> RiskDecision:
    """Advance the overbought state machine by one observation."""

    current = dict(state or {})
    phase = str(current.get("phase") or "idle")
    peak = _as_float(current.get("peak_high"))
    now_iso = now.isoformat()

    def finish(
        action: str,
        reason: str,
        *,
        peak_for_metrics: float | None = peak,
        **updates: Any,
    ) -> RiskDecision:
        current.update(updates)
        current["last_eval_at"] = now_iso
        current["last_metrics"] = {**_metrics(snapshot, peak_for_metrics), "evaluated_at": now_iso}
        return RiskDecision(state=current, action=action, reason=reason)

    if phase == "paused":
        paused_at = _parse(current.get("paused_at"))
        cooldown = timedelta(minutes=float(config["RECOVERY_COOLDOWN_MINUTES"]))
        cooled = paused_at is None or now - paused_at >= cooldown
        rsi_ok = (
            snapshot.rsi is not None
            and snapshot.rsi > float(config["RSI_RECOVERY"])
            and (snapshot.rsi_prev is None or snapshot.rsi > snapshot.rsi_prev)
        )
        vwap_ok = snapshot.vwap is not None and snapshot.close > snapshot.vwap
        if snapshot.oi_available and len(snapshot.oi_values) >= 2:
            oi_ok = snapshot.oi_values[-1] > snapshot.oi_values[-2]
        else:
            failsafe = timedelta(minutes=float(config["OI_FAILSAFE_MINUTES"]))
            oi_ok = paused_at is not None and now - paused_at >= failsafe
        if cooled and rsi_ok and vwap_ok and oi_ok:
            return finish(
                "resume",
                "recovery_confirmed",
                peak_for_metrics=None,
                phase="idle",
                armed_at=None,
                peak_high=None,
                peak_at=None,
                paused_at=None,
                pause_reason="",
            )
        return finish("hold", "paused")

    if phase == "armed":
        armed_at = _parse(current.get("armed_at"))
        timeout = timedelta(hours=float(config["ARM_TIMEOUT_HOURS"]))
        if armed_at is not None and now - armed_at > timeout:
            return finish(
                "idle",
                "arm_timeout",
                peak_for_metrics=None,
                phase="idle",
                armed_at=None,
                peak_high=None,
                peak_at=None,
            )
        high = max(
            [value for value in (peak, snapshot.bar_high, snapshot.price) if value]
            or [0.0]
        )
        peak_at = current.get("peak_at") if peak is not None and high <= peak else now_iso
        reason = _exit_reason(snapshot, high, config) if high > 0 else ""
        if reason:
            return finish(
                "pause",
                reason,
                peak_for_metrics=high,
                phase="paused",
                peak_high=high,
                peak_at=peak_at,
                paused_at=now_iso,
                pause_reason=reason,
            )
        return finish(
            "hold",
            "armed",
            peak_for_metrics=high,
            phase="armed",
            peak_high=high,
            peak_at=peak_at,
        )

    if snapshot.rsi is not None and snapshot.rsi > float(config["RSI_OVERBOUGHT"]):
        high = max(snapshot.bar_high, snapshot.price)
        return finish(
            "armed",
            "rsi_overbought",
            peak_for_metrics=high,
            phase="armed",
            armed_at=now_iso,
            peak_high=high,
            peak_at=now_iso,
        )
    return finish("idle", "", peak_for_metrics=None, phase="idle")
