"""BTC market regime protection: readings and the pure evaluator.

BTC is read once per closed 15m bar and classified into one global phase:

    normal   no warning or bearish signal
    warning  guard armed (RSI overbought) or funding crowding
             -> new buys are blocked on every coin
    bearish  guard exit, trailing stop below the window high, open-interest
             flush near the 24h high, or a 1h close below support
             -> every active session is flattened and paused

``bearish`` is left only when every recovery check passes and the minimum
duration has elapsed. The live controller and the backtest timeline both build
``RegimeReadings`` with ``reading_series`` and classify them with
``evaluate_regime``, so the two paths cannot drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping

import numpy as np

from apps.engine.grid import (
    MIN_FUNDING_HISTORY,
    GridParams,
    _funding_z_series,
    _indicator_series,
    _rolling_extreme,
)
from apps.trading import risk

SYMBOL = "BTCUSDT"
NORMAL = "normal"
WARNING = "warning"
BEARISH = "bearish"

BAR_15M = timedelta(minutes=15)
BAR_1H = timedelta(hours=1)
BARS_15M_PER_DAY = 96
BARS_1H_PER_DAY = 24
# 30-day support plus the 14-day ATR, with a day of slack.
WARMUP_1H_DAYS = 45
OI_READINGS = 6
SUPPORT_BUFFER_ATR = 0.25
FUNDING_LOOKBACK = 90
# Thresholds are compared with a small tolerance so a reading exactly on a
# threshold triggers regardless of float rounding.
EPS = 1e-9


@dataclass(frozen=True)
class RegimeConfig:
    trailing_stop_pct: float
    trailing_window_days: int
    oi_flush_pct: float
    oi_near_high_pct: float
    funding_z: float
    min_bearish_hours: float
    risk: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_settings(cls, row: Any) -> "RegimeConfig":
        from django.conf import settings

        return cls(
            trailing_stop_pct=float(row.btc_trailing_stop_pct),
            trailing_window_days=int(row.btc_trailing_window_days),
            oi_flush_pct=float(row.btc_oi_flush_pct),
            oi_near_high_pct=float(row.btc_oi_near_high_pct),
            funding_z=float(row.btc_funding_z),
            min_bearish_hours=float(row.btc_min_bearish_hours),
            risk=dict(settings.RISK),
        )


@dataclass(frozen=True)
class RegimeReadings:
    """BTC state at the close of one 15m bar (``time`` is the close time)."""

    time: datetime
    price: float
    bar_high: float
    rsi: float | None
    rsi_prev: float | None
    macd_hist: tuple[float, ...]
    vwap: float | None
    window_high: float
    high_24h: float
    close_1h: float
    support: float
    atr: float
    momentum: float
    trend_up: bool
    funding_z: float | None
    oi_values: tuple[float, ...]


@dataclass
class RegimeDecision:
    phase: str
    reasons: list[str]
    metrics: dict[str, Any]
    guard_state: dict[str, Any]
    entered_at: datetime | None
    changed: bool


def _rsi_series(closes: np.ndarray, period: int) -> list[float | None]:
    """``risk.rsi`` of every prefix, computed in one pass."""

    out: list[float | None] = [None] * closes.size
    if closes.size < period + 1:
        return out
    deltas = np.diff(closes)
    gains = np.maximum(deltas, 0.0)
    losses = np.maximum(-deltas, 0.0)
    avg_gain = float(np.mean(gains[:period]))
    avg_loss = float(np.mean(losses[:period]))
    for k in range(period, closes.size):
        if k > period:
            avg_gain = (avg_gain * (period - 1) + gains[k - 1]) / period
            avg_loss = (avg_loss * (period - 1) + losses[k - 1]) / period
        if avg_loss <= 1e-12:
            out[k] = 100.0 if avg_gain > 0 else 50.0
        else:
            out[k] = 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


def _vwap_series(bars: list[dict[str, Any]], close_times: list[datetime]) -> list[float | None]:
    """``risk.anchored_vwap`` anchored at the UTC day of each bar's close."""

    out: list[float | None] = []
    day = None
    total_pv = 0.0
    total_volume = 0.0
    for bar, closed in zip(bars, close_times):
        bar_day = bar["time"].date()
        if bar_day != day:
            day, total_pv, total_volume = bar_day, 0.0, 0.0
        volume = float(bar["volume"])
        if volume > 0:
            total_pv += (bar["high"] + bar["low"] + bar["close"]) / 3.0 * volume
            total_volume += volume
        # A bar closing exactly at midnight belongs to the previous day, so the
        # new day's anchored VWAP has no bars yet.
        if closed.date() != bar_day or total_volume <= 0:
            out.append(None)
        else:
            out.append(total_pv / total_volume)
    return out


def _seconds(values: list[datetime]) -> np.ndarray:
    return np.array([value.timestamp() for value in values], dtype=float)


def reading_series(
    bars_15m: list[dict[str, Any]],
    bars_1h: list[dict[str, Any]],
    funding: list[dict[str, Any]],
    oi_points: list[dict[str, Any]],
    config: RegimeConfig,
) -> list[RegimeReadings]:
    """One reading per closed 15m bar with full warm-up, oldest first.

    Every reading uses only data at or before its bar's close. All series are
    causal, so the readings for a prefix of the inputs equal the leading
    readings for the full inputs.
    """

    window_bars = max(1, int(config.trailing_window_days)) * BARS_15M_PER_DAY
    warmup_1h = WARMUP_1H_DAYS * BARS_1H_PER_DAY
    if len(bars_15m) < window_bars or len(bars_1h) < warmup_1h:
        return []

    cfg = config.risk
    rsi_period = int(cfg["RSI_PERIOD"])
    closes = np.array([bar["close"] for bar in bars_15m], dtype=float)
    highs = np.array([bar["high"] for bar in bars_15m], dtype=float)
    close_times = [bar["time"] + BAR_15M for bar in bars_15m]
    rsi_values = _rsi_series(closes, rsi_period)
    macd_line = risk._ema(closes, int(cfg["MACD_FAST"])) - risk._ema(closes, int(cfg["MACD_SLOW"]))
    hist = macd_line - risk._ema(macd_line, int(cfg["MACD_SIGNAL"]))
    macd_min_bars = int(cfg["MACD_SLOW"]) + int(cfg["MACD_SIGNAL"])
    vwaps = _vwap_series(bars_15m, close_times)
    window_high = _rolling_extreme(highs, window_bars, True)
    high_24h = _rolling_extreme(highs, BARS_15M_PER_DAY, True)

    h_close = np.array([bar["close"] for bar in bars_1h], dtype=float)
    hourly = _indicator_series(
        np.array([bar["high"] for bar in bars_1h], dtype=float),
        np.array([bar["low"] for bar in bars_1h], dtype=float),
        h_close,
        GridParams(),
        BARS_1H_PER_DAY,
    )
    h_closed_at = _seconds([bar["time"] + BAR_1H for bar in bars_1h])

    ordered_funding = sorted(funding, key=lambda row: row["time"])
    funding_at = _seconds([row["time"] for row in ordered_funding])
    funding_z = _funding_z_series(
        np.array([float(row["rate"]) for row in ordered_funding], dtype=float), FUNDING_LOOKBACK
    )

    ordered_oi = sorted(oi_points, key=lambda row: row["time"])
    oi_at = _seconds([row["time"] for row in ordered_oi])
    oi_values = [float(row["open_interest"]) for row in ordered_oi]
    oi_span = OI_READINGS * BAR_1H.total_seconds()

    readings: list[RegimeReadings] = []
    for i in range(window_bars - 1, len(bars_15m)):
        moment = close_times[i]
        at = moment.timestamp()
        h = int(np.searchsorted(h_closed_at, at, side="right")) - 1
        if h < warmup_1h - 1:
            continue
        f = int(np.searchsorted(funding_at, at, side="right"))
        # Only OI readings inside the lookback window count, so a gap in the
        # stored history reads as unavailable rather than stale.
        o_end = int(np.searchsorted(oi_at, at, side="right"))
        o_start = int(np.searchsorted(oi_at, at - oi_span, side="right"))
        recent_oi = tuple(oi_values[o_start:o_end][-OI_READINGS:])
        readings.append(
            RegimeReadings(
                time=moment,
                price=float(closes[i]),
                bar_high=float(highs[i]),
                rsi=rsi_values[i],
                rsi_prev=rsi_values[i - 1] if i > rsi_period else None,
                macd_hist=(
                    tuple(float(x) for x in hist[max(0, i - 3) : i + 1])
                    if i + 1 >= macd_min_bars
                    else ()
                ),
                vwap=vwaps[i],
                window_high=float(window_high[i]),
                high_24h=float(high_24h[i]),
                close_1h=float(h_close[h]),
                support=float(hourly["support_prior"][h]),
                atr=float(hourly["atr"][h]),
                momentum=float(hourly["momentum"][h]),
                trend_up=bool(hourly["trend_up"][h]),
                funding_z=float(funding_z[f - 1]) if f > MIN_FUNDING_HISTORY else None,
                oi_values=recent_oi if len(recent_oi) >= 2 else (),
            )
        )
    return readings


def _snapshot(reading: RegimeReadings) -> risk.RiskSnapshot:
    return risk.RiskSnapshot(
        price=reading.price,
        bar_time=reading.time - BAR_15M,
        bar_high=reading.bar_high,
        close=reading.price,
        rsi=reading.rsi,
        rsi_prev=reading.rsi_prev,
        macd_hist=list(reading.macd_hist),
        vwap=reading.vwap,
        oi_values=list(reading.oi_values),
        oi_available=bool(reading.oi_values),
    )


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def evaluate_regime(
    phase: str,
    entered_at: datetime | None,
    guard_state: Mapping[str, Any] | None,
    reading: RegimeReadings,
    config: RegimeConfig,
) -> RegimeDecision:
    """Advance the BTC regime by one reading. Pure: no I/O."""

    previous = phase if phase in (NORMAL, WARNING, BEARISH) else NORMAL
    snapshot = _snapshot(reading)
    guard = risk.evaluate(guard_state or {}, snapshot, config.risk, reading.time)

    support_line = reading.support - SUPPORT_BUFFER_ATR * reading.atr
    drawdown = 1.0 - reading.price / reading.window_high if reading.window_high > 0 else 0.0
    oi = reading.oi_values
    oi_change = oi[-1] / oi[0] - 1.0 if len(oi) >= 2 and oi[0] > 0 else None

    trailing = drawdown >= config.trailing_stop_pct - EPS
    oi_flush = (
        oi_change is not None
        and oi_change <= -config.oi_flush_pct + EPS
        and reading.price >= reading.high_24h * (1.0 - config.oi_near_high_pct) - EPS
    )
    breakdown = reading.support > 0 and reading.close_1h < support_line

    bearish: list[str] = []
    if guard.action == "pause":
        bearish.append("guard_exit")
    if trailing:
        bearish.append("trailing_stop")
    if oi_flush:
        bearish.append("oi_flush")
    if breakdown:
        bearish.append("support_breakdown")

    warning: list[str] = []
    if guard.state.get("phase") == "armed":
        warning.append("guard_armed")
    if (
        reading.funding_z is not None
        and reading.funding_z >= config.funding_z - EPS
        and (reading.momentum < 0 or not reading.trend_up)
    ):
        warning.append("funding_crowded")

    metrics: dict[str, Any] = {
        "time": reading.time.isoformat(),
        "price": round(reading.price, 10),
        "window_high": round(reading.window_high, 10),
        "drawdown_pct": round(drawdown, 6),
        "high_24h": round(reading.high_24h, 10),
        "rsi_15m": _round(reading.rsi, 2),
        "vwap_15m": _round(reading.vwap, 10),
        "close_1h": round(reading.close_1h, 10),
        "support": round(reading.support, 10),
        "support_line": round(support_line, 10),
        "atr": round(reading.atr, 10),
        "momentum": round(reading.momentum, 6),
        "trend_up": reading.trend_up,
        "funding_z": _round(reading.funding_z, 4),
        "oi_change_pct": _round(oi_change),
        "oi_available": bool(oi),
        "guard_phase": guard.state.get("phase") or "idle",
    }

    if previous == BEARISH:
        elapsed_ok = entered_at is None or reading.time - entered_at >= timedelta(
            hours=config.min_bearish_hours
        )
        checks = {
            **risk.recovery_checks(snapshot, entered_at, config.risk, reading.time),
            "above_support": not breakdown,
            "below_trailing_stop": not trailing,
            "no_oi_flush": not oi_flush,
            "min_duration": elapsed_ok,
        }
        metrics["recovery"] = checks
        if not (all(checks.values()) and not bearish):
            new_phase = BEARISH
            reasons = bearish or ["recovery_pending"]
        else:
            new_phase = WARNING if warning else NORMAL
            reasons = warning
    elif bearish:
        new_phase, reasons = BEARISH, bearish
    elif warning:
        new_phase, reasons = WARNING, warning
    else:
        new_phase, reasons = NORMAL, []

    changed = new_phase != previous
    return RegimeDecision(
        phase=new_phase,
        reasons=list(reasons),
        metrics=metrics,
        guard_state=dict(guard.state),
        entered_at=reading.time if changed else entered_at,
        changed=changed,
    )
