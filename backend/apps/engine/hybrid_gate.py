"""Hybrid trend gate for backtests.

The live and paper hybrid mode trades long only while the last closed 1h candle
is above EMA(100), exits after two consecutive closed 15m candles below that
EMA, and optionally requires ADX(14) >= 25 with +DI > -DI. This module mirrors
that rule on historical klines and reduces it to a per-bar gate the grid
simulator can consume.

The gate is sampled at each simulation bar boundary. 1h closes and the ADX/DMI
state are advanced per closed 1h candle; the 15m confirmation state machine is
replayed per closed 15m candle so halts and resumes land on the first
simulation bar after they are confirmed.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np

EMA_PERIOD = 100
ADX_PERIOD = 14
ADX_ENTRY = 25.0
CONFIRM_BARS = 2
ONE_HOUR = timedelta(hours=1)
FIFTEEN_MINUTES = timedelta(minutes=15)


@dataclass(frozen=True)
class Gate:
    """Per-bar switches keyed by the bar's open time.

    ``allow`` is False while new buys must stay disarmed; ``exit`` is truthy on
    the first bar after a confirmed exit so the simulator flattens inventory
    (a string value names the exit reason; ``True`` means ``hybrid_exit``).
    Bars without an entry are outside the fetched kline coverage and trade
    ungated.
    """

    allow: dict[datetime, bool]
    exit: dict[datetime, bool]
    allowed_bars: int = 0
    blocked_bars: int = 0
    exits: int = 0
    # Bars on which open inventory is offered on the nearest sell levels only.
    derisk: dict[datetime, bool] = field(default_factory=dict)

    def stats(self) -> dict[str, Any]:
        total = self.allowed_bars + self.blocked_bars
        return {
            "bars": total,
            "allowed_bars": self.allowed_bars,
            "blocked_bars": self.blocked_bars,
            "blocked_pct": round(self.blocked_bars / total, 4) if total else 0.0,
            "confirmed_exits": self.exits,
        }


def combine_gates(a: Gate | None, b: Gate | None) -> Gate | None:
    """Buys allowed only where both allow; exit where either exits.

    A bar missing from one gate counts as allowed by it, and a bar missing
    from both stays missing, so it still trades ungated.
    """

    if a is None or b is None:
        return a if b is None else b
    allow: dict[datetime, bool] = {}
    for key in a.allow.keys() | b.allow.keys():
        allow[key] = a.allow.get(key, True) and b.allow.get(key, True)
    exits: dict[datetime, Any] = {}
    for key in a.exit.keys() | b.exit.keys():
        value = a.exit.get(key) or b.exit.get(key)
        if value:
            exits[key] = value
    allowed = sum(1 for value in allow.values() if value)
    derisk = {
        key: True for key in a.derisk.keys() | b.derisk.keys() if a.derisk.get(key) or b.derisk.get(key)
    }
    return Gate(
        allow=allow,
        exit=exits,
        allowed_bars=allowed,
        blocked_bars=len(allow) - allowed,
        exits=len(exits),
        derisk=derisk,
    )


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


def adx_series(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ADX_PERIOD
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wilder ADX, +DI and -DI per candle (NaN until enough history)."""

    n = high.size
    adx = np.full(n, np.nan)
    plus_di = np.full(n, np.nan)
    minus_di = np.full(n, np.nan)
    if n < period * 3:
        return adx, plus_di, minus_di

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
    adx_s = np.zeros(n)
    adx_s[start] = float(np.mean(dx[period + 1 : start + 1]))
    for i in range(start + 1, n):
        adx_s[i] = (adx_s[i - 1] * (period - 1) + dx[i]) / period
    for i in range(period, n):
        if atr_s[i] > 1e-12:
            plus_di[i] = 100.0 * plus_s[i] / atr_s[i]
            minus_di[i] = 100.0 * minus_s[i] / atr_s[i]
    adx[start:] = adx_s[start:]
    return adx, plus_di, minus_di


def build_gate(
    rows: list[dict[str, Any]],
    klines_1h: list[dict[str, Any]],
    klines_15m: list[dict[str, Any]],
    *,
    adx_enabled: bool = False,
) -> Gate:
    """Reduce closed 1h/15m klines to per-bar allow/exit switches."""

    if not rows or len(klines_1h) < EMA_PERIOD + 1 or not klines_15m:
        return Gate(allow={}, exit={})

    h1_times = [row["time"] for row in klines_1h]
    c1 = np.array([row["close"] for row in klines_1h], dtype=float)
    h1 = np.array([row["high"] for row in klines_1h], dtype=float)
    l1 = np.array([row["low"] for row in klines_1h], dtype=float)
    ema = ema_series(c1, EMA_PERIOD)
    adx: np.ndarray | None = None
    pdi: np.ndarray | None = None
    mdi: np.ndarray | None = None
    if adx_enabled:
        adx, pdi, mdi = adx_series(h1, l1, c1, ADX_PERIOD)

    # Replay the 15m confirmation state machine over closed candles.
    event_close: list[datetime] = []
    event_allow: list[bool] = []
    event_halted: list[bool] = []
    one_h_idx = -1
    halted = False
    above = 0
    below = 0
    for candle in klines_15m:
        close_time = candle["time"] + FIFTEEN_MINUTES
        while one_h_idx + 1 < len(h1_times) and h1_times[one_h_idx + 1] + ONE_HOUR <= close_time:
            one_h_idx += 1
        ready = one_h_idx >= EMA_PERIOD
        allow_now = False
        if ready:
            ema_now = float(ema[one_h_idx])
            close_15 = float(candle["close"])
            if close_15 > ema_now:
                above += 1
                below = 0
            elif close_15 < ema_now:
                below += 1
                above = 0
            else:
                above = below = 0
            trend_ok = float(c1[one_h_idx]) > ema_now
            adx_ok = True
            if adx_enabled and adx is not None and pdi is not None and mdi is not None:
                adx_value = float(adx[one_h_idx])
                adx_ok = bool(
                    np.isfinite(adx_value)
                    and adx_value >= ADX_ENTRY
                    and float(pdi[one_h_idx]) > float(mdi[one_h_idx])
                )
            if below >= CONFIRM_BARS:
                halted = True
            elif halted and above >= CONFIRM_BARS and trend_ok and adx_ok:
                halted = False
            allow_now = trend_ok and adx_ok and not halted
        event_close.append(close_time)
        event_allow.append(allow_now)
        event_halted.append(halted)

    allow_map: dict[datetime, bool] = {}
    exit_map: dict[datetime, bool] = {}
    allowed = 0
    blocked = 0
    exits = 0
    event_idx = -1
    prev_halted = False
    for row in sorted(rows, key=lambda r: r["time"]):
        bar_time = row["time"]
        while event_idx + 1 < len(event_close) and event_close[event_idx + 1] <= bar_time:
            event_idx += 1
        if event_idx < 0:
            continue
        allow_now = event_allow[event_idx]
        halted_now = event_halted[event_idx]
        allow_map[bar_time] = allow_now
        if halted_now and not prev_halted:
            exit_map[bar_time] = True
            exits += 1
        prev_halted = halted_now
        if allow_now:
            allowed += 1
        else:
            blocked += 1

    return Gate(
        allow=allow_map,
        exit=exit_map,
        allowed_bars=allowed,
        blocked_bars=blocked,
        exits=exits,
    )
