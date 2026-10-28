"""Hybrid trend filters evaluated on fully closed candles only.

Hybrid mode limits new long inventory to confirmed uptrends. The entry gate is
the last closed 1h close versus EMA(100); exits are confirmed by two
consecutive closed 15m candles below that EMA. The optional ADX filter adds a
trend-strength gate with hysteresis (enter at ADX >= 25, no re-entry while ADX
< 20) and requires +DI > -DI.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

import numpy as np

EMA_PERIOD = 100
ADX_PERIOD = 14
ADX_ENTRY = 25.0
ADX_EXIT = 20.0
CONFIRM_BARS = 2


@dataclass(frozen=True)
class TrendSignal:
    ready: bool
    ema_100: float
    price_1h: float
    adx: float | None
    plus_di: float | None
    minus_di: float | None
    above_streak: int
    below_streak: int
    halted: bool
    allow_buys: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def ema_series(values: np.ndarray, period: int) -> np.ndarray:
    if values.size == 0:
        return values
    alpha = 2.0 / (period + 1.0)
    out = np.empty(values.size, dtype=float)
    out[0] = float(values[0])
    for idx in range(1, values.size):
        out[idx] = alpha * float(values[idx]) + (1.0 - alpha) * out[idx - 1]
    return out


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])


def dmi(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ADX_PERIOD
) -> tuple[float, float, float] | None:
    """Wilder ADX plus the directional indicators at the last candle."""

    n = high.size
    if n < period * 3:
        return None
    plus_dm = np.zeros(n)
    minus_dm = np.zeros(n)
    for i in range(1, n):
        up = high[i] - high[i - 1]
        down = low[i - 1] - low[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
    tr = _true_range(high, low, close)
    atr_s = np.zeros(n)
    plus_s = np.zeros(n)
    minus_s = np.zeros(n)
    atr_s[period] = float(np.mean(tr[1 : period + 1]))
    plus_s[period] = float(np.mean(plus_dm[1 : period + 1]))
    minus_s[period] = float(np.mean(minus_dm[1 : period + 1]))
    dx = np.zeros(n)
    for i in range(period + 1, n):
        atr_s[i] = (atr_s[i - 1] * (period - 1) + tr[i]) / period
        plus_s[i] = (plus_s[i - 1] * (period - 1) + plus_dm[i]) / period
        minus_s[i] = (minus_s[i - 1] * (period - 1) + minus_dm[i]) / period
        if atr_s[i] > 1e-12:
            pdi = 100.0 * plus_s[i] / atr_s[i]
            mdi = 100.0 * minus_s[i] / atr_s[i]
            denom = pdi + mdi
            dx[i] = 100.0 * abs(pdi - mdi) / denom if denom > 1e-12 else 0.0
    start = period * 2
    if n <= start or atr_s[-1] <= 1e-12:
        return None
    adx_s = np.zeros(n)
    adx_s[start] = float(np.mean(dx[period + 1 : start + 1]))
    for i in range(start + 1, n):
        adx_s[i] = (adx_s[i - 1] * (period - 1) + dx[i]) / period
    plus_di = 100.0 * plus_s[-1] / atr_s[-1]
    minus_di = 100.0 * minus_s[-1] / atr_s[-1]
    return float(adx_s[-1]), float(plus_di), float(minus_di)


def _streaks(closes: list[float], ema: float) -> tuple[int, int]:
    above = 0
    for close in reversed(closes):
        if close > ema:
            above += 1
        else:
            break
    below = 0
    for close in reversed(closes):
        if close < ema:
            below += 1
        else:
            break
    return above, below


def evaluate(
    klines_1h: list[dict[str, Any]],
    klines_15m: list[dict[str, Any]],
    *,
    adx_enabled: bool = False,
    halted: bool = False,
) -> TrendSignal:
    """Return the hybrid gate from the latest closed 1h and 15m candles."""

    if len(klines_1h) < EMA_PERIOD + 1:
        return TrendSignal(
            ready=False,
            ema_100=0.0,
            price_1h=0.0,
            adx=None,
            plus_di=None,
            minus_di=None,
            above_streak=0,
            below_streak=0,
            halted=halted,
            allow_buys=False,
            reason="insufficient_history",
        )

    highs = np.array([row["high"] for row in klines_1h], dtype=float)
    lows = np.array([row["low"] for row in klines_1h], dtype=float)
    closes = np.array([row["close"] for row in klines_1h], dtype=float)
    ema_100 = float(ema_series(closes, EMA_PERIOD)[-1])
    price_1h = float(closes[-1])
    trend_ok = price_1h > ema_100

    closes_15m = [float(row["close"]) for row in klines_15m]
    above_streak, below_streak = _streaks(closes_15m, ema_100)

    adx_value: float | None = None
    plus_di: float | None = None
    minus_di: float | None = None
    dmi_value = dmi(highs, lows, closes, ADX_PERIOD)
    if dmi_value is not None:
        adx_value, plus_di, minus_di = dmi_value
    adx_ok = True
    if adx_enabled:
        adx_ok = bool(
            dmi_value is not None
            and adx_value is not None
            and adx_value >= ADX_ENTRY
            and plus_di is not None
            and minus_di is not None
            and plus_di > minus_di
        )

    new_halted = halted
    if below_streak >= CONFIRM_BARS:
        new_halted = True
    elif halted and above_streak >= CONFIRM_BARS and trend_ok and adx_ok:
        new_halted = False

    allow_buys = trend_ok and adx_ok and not new_halted

    if below_streak >= CONFIRM_BARS:
        reason = "exit_confirmed"
    elif new_halted:
        if above_streak >= CONFIRM_BARS and trend_ok and adx_enabled and not adx_ok:
            reason = "awaiting_adx"
        else:
            reason = "halted"
    elif not trend_ok:
        reason = "below_ema"
    elif not adx_ok:
        reason = "adx_weak"
    else:
        reason = "ok"

    return TrendSignal(
        ready=True,
        ema_100=ema_100,
        price_1h=price_1h,
        adx=adx_value,
        plus_di=plus_di,
        minus_di=minus_di,
        above_streak=above_streak,
        below_streak=below_streak,
        halted=new_halted,
        allow_buys=allow_buys,
        reason=reason,
    )
