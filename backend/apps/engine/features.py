"""Coin-level market microstructure and regime features.

All functions use only observations available up to the last candle in the
supplied dataset. Features describe the regime used for selection; backtest
returns are produced separately by the grid simulator.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from apps.engine.grid import BARS_PER_DAY, GridParams, SimulationConfig, atr_scale


def _clean(value: float | None, digits: int = 6) -> float | None:
    if value is None:
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(value, digits)


def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce([high - low, np.abs(high - prev), np.abs(low - prev)])


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int) -> float:
    tr = true_range(high, low, close)
    window = min(period, tr.size)
    return float(np.mean(tr[-window:])) if window else 0.0


def hurst_exponent(closes: np.ndarray, lags: tuple[int, ...] = (2, 4, 8, 16, 32)) -> float | None:
    if closes.size < max(lags) * 3:
        return None
    log_price = np.log(np.maximum(closes, 1e-12))
    taus: list[float] = []
    valid_lags: list[int] = []
    for lag in lags:
        diff = log_price[lag:] - log_price[:-lag]
        if diff.size < 8:
            continue
        tau = float(np.std(diff, ddof=1))
        if tau > 1e-12:
            taus.append(tau)
            valid_lags.append(lag)
    if len(taus) < 3:
        return None
    slope = float(np.polyfit(np.log(valid_lags), np.log(taus), 1)[0])
    return _clamp(slope, 0.0, 1.0)


def variance_ratio(returns: np.ndarray, q: int = 8) -> float | None:
    if returns.size < q * 12:
        return None
    var_1 = float(np.var(returns, ddof=1))
    if var_1 <= 1e-15:
        return None
    n = (returns.size // q) * q
    agg = returns[:n].reshape(-1, q).sum(axis=1)
    var_q = float(np.var(agg, ddof=1))
    return var_q / (q * var_1)


def adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> float | None:
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
    tr = true_range(high, low, close)
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
    if n <= start:
        return None
    adx_s = np.zeros(n)
    adx_s[start] = float(np.mean(dx[period + 1 : start + 1]))
    for i in range(start + 1, n):
        adx_s[i] = (adx_s[i - 1] * (period - 1) + dx[i]) / period
    return float(adx_s[-1])


def range_containment(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, window: int, step: int
) -> float | None:
    if close.size < window * 3:
        return None
    contained = 0
    total = 0
    for start in range(0, close.size - window * 2 + 1, step):
        prior_high = float(np.max(high[start : start + window]))
        prior_low = float(np.min(low[start : start + window]))
        future = close[start + window : start + 2 * window]
        contained += int(np.sum((future >= prior_low) & (future <= prior_high)))
        total += future.size
    return contained / total if total else None


def trend_fit(closes: np.ndarray, lookback: int) -> tuple[float | None, float | None]:
    if closes.size < 20:
        return None, None
    y = np.log(np.maximum(closes[-lookback:], 1e-12))
    x = np.arange(y.size, dtype=float)
    if y.size < 5:
        return None, None
    slope, intercept = np.polyfit(x, y, 1)
    predicted = slope * x + intercept
    ss_res = float(np.sum((y - predicted) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 1e-15 else 0.0
    return float(slope), r2


def max_drawdown(values: np.ndarray) -> float:
    if values.size == 0:
        return 0.0
    peak = np.maximum.accumulate(values)
    dd = 1.0 - values / np.maximum(peak, 1e-12)
    return float(np.max(dd))


def compute_features(
    rows: list[dict[str, Any]],
    funding_rows: list[dict[str, Any]],
    params: GridParams,
    config: SimulationConfig,
) -> dict[str, Any]:
    if len(rows) < 50:
        return {"bars": len(rows), "insufficient_data": True}
    bpd = config.bars_per_day
    o = np.array([r["open"] for r in rows], dtype=float)
    h = np.array([r["high"] for r in rows], dtype=float)
    low = np.array([r["low"] for r in rows], dtype=float)
    c = np.array([r["close"] for r in rows], dtype=float)
    quote_volume = np.array([r.get("quote_volume", 0.0) for r in rows], dtype=float)
    trades = np.array([r.get("trades", 0) for r in rows], dtype=float)
    n = c.size
    log_ret = np.diff(np.log(np.maximum(c, 1e-12)))
    price = float(c[-1])
    ppy = config.periods_per_year

    realized_vol = float(np.std(log_ret, ddof=1) * math.sqrt(ppy)) if log_ret.size > 2 else None
    downside = log_ret[log_ret < 0]
    downside_vol = (
        float(np.std(downside, ddof=1) * math.sqrt(ppy)) if downside.size > 2 else None
    )
    # 4h-equivalent, so the ATR% gate means the same thing on every interval.
    atr_value = atr(h, low, c, 14 * bpd) * atr_scale(bpd)
    atr_pct = atr_value / price if price > 0 else None

    daily_windows = max(1, min(60, n // bpd))
    daily_volumes = [
        float(np.sum(quote_volume[n - bpd * (i + 1) : n - bpd * i]))
        for i in range(daily_windows)
        if n - bpd * (i + 1) >= 0
    ]
    adv = float(np.median(daily_volumes)) if daily_volumes else 0.0
    min_adv = float(np.min(daily_volumes)) if daily_volumes else 0.0
    volume_cv = (
        float(np.std(daily_volumes, ddof=1) / np.mean(daily_volumes))
        if len(daily_volumes) > 2 and np.mean(daily_volumes) > 0
        else None
    )
    daily_ranges = [
        float((np.max(h[n - bpd * (i + 1) : n - bpd * i]) - np.min(low[n - bpd * (i + 1) : n - bpd * i]))
              / max(price, 1e-9))
        for i in range(daily_windows)
        if n - bpd * (i + 1) >= 0
    ]
    daily_trades = [
        float(np.mean(trades[n - bpd * (i + 1) : n - bpd * i]))
        for i in range(daily_windows)
        if n - bpd * (i + 1) >= 0
    ]

    hurst = hurst_exponent(c[-min(n, 180 * bpd) :])
    vr = variance_ratio(log_ret[-min(log_ret.size, 120 * bpd) :])
    ac1 = float(np.corrcoef(log_ret[:-1], log_ret[1:])[0, 1]) if log_ret.size > 10 else None
    adx_value = adx(h[-min(n, 120 * bpd) :], low[-min(n, 120 * bpd) :], c[-min(n, 120 * bpd) :])
    slope, r2 = trend_fit(c, min(n, 90 * bpd))
    annual_slope = (
        (math.exp(slope * bpd * 365) - 1.0) if slope is not None and abs(slope) < 5 else None
    )

    containment = range_containment(h, low, c, window=14 * bpd, step=7 * bpd)
    rolling_widths: list[float] = []
    for end in range(14 * bpd, n + 1, 7 * bpd):
        window_high = float(np.max(h[end - 14 * bpd : end]))
        window_low = float(np.min(low[end - 14 * bpd : end]))
        rolling_widths.append((window_high - window_low) / max(price, 1e-9))
    width_cv = (
        float(np.std(rolling_widths, ddof=1) / np.mean(rolling_widths))
        if len(rolling_widths) > 2 and np.mean(rolling_widths) > 0
        else None
    )

    rates = np.array([float(r["rate"]) for r in funding_rows], dtype=float)
    funding_mean = float(np.mean(rates)) if rates.size else None
    funding_abs_mean = float(np.mean(np.abs(rates))) if rates.size else None
    funding_pct_positive = float(np.mean(rates > 0)) if rates.size else None
    funding_annual_cost = funding_mean * 3 * 365 if funding_mean is not None else None

    range_high = float(np.percentile(h[-min(n, 30 * bpd) :], 95))
    range_low = float(np.percentile(low[-min(n, 30 * bpd) :], 5))
    range_width = (range_high - range_low) / max(price, 1e-9)
    levels = max(3, params.grid_levels)
    if params.spacing_mode == "arithmetic":
        grid_step_bps = ((range_high - range_low) / (levels - 1)) / max(price, 1e-9) * 10_000
    else:
        ratio = range_high / max(range_low, 1e-12)
        grid_step_bps = (ratio ** (1.0 / (levels - 1)) - 1.0) * 10_000 if ratio > 0 else 0.0
    round_trip_cost_bps = 2 * (config.maker_fee_rate * 10_000 + config.slippage_bps)
    cost_to_edge = round_trip_cost_bps / grid_step_bps if grid_step_bps > 1e-9 else None

    max_gap = float(np.max(np.abs(o[1:] - c[:-1]) / np.maximum(c[:-1], 1e-12))) if n > 2 else None
    q01 = float(np.percentile(log_ret, 1)) if log_ret.size > 20 else None
    buy_hold = (price / float(c[0]) - 1.0) if c[0] > 0 else None
    buy_hold_dd = max_drawdown(c)

    hurst_signal = _clamp((0.5 - hurst) / 0.15) if hurst is not None else 0.0
    vr_signal = _clamp((1.0 - vr) / 0.5) if vr is not None else 0.0
    ac_signal = _clamp(-ac1 / 0.15) if ac1 is not None else 0.0
    mean_reversion_score = max(
        0.0,
        min(100.0, 50.0 + 50.0 * (0.4 * hurst_signal + 0.35 * vr_signal + 0.25 * ac_signal)),
    )

    return {
        "bars": n,
        "data_days": round(n / bpd, 2),
        "price": _clean(price, 8),
        "realized_vol_annual": _clean(realized_vol),
        "downside_vol_annual": _clean(downside_vol),
        "atr_pct": _clean(atr_pct),
        "daily_range_pct_median": _clean(float(np.median(daily_ranges)) if daily_ranges else None),
        "adv_usd_median": _clean(adv, 2),
        "adv_usd_min": _clean(min_adv, 2),
        "volume_cv": _clean(volume_cv),
        "avg_trades_per_day": _clean(float(np.median(daily_trades)) if daily_trades else None, 2),
        "hurst": _clean(hurst),
        "variance_ratio": _clean(vr),
        "autocorr_lag1": _clean(ac1),
        "mean_reversion_score": _clean(mean_reversion_score, 2),
        "adx": _clean(adx_value, 2),
        "trend_r2": _clean(r2),
        "trend_slope_annual": _clean(annual_slope),
        "range_containment": _clean(containment),
        "range_width_pct": _clean(range_width),
        "range_width_cv": _clean(width_cv),
        "range_stability_score": _clean(
            (0.65 * containment + 0.35 * max(0.0, 1.0 - min(1.0, width_cv or 1.0))) * 100
            if containment is not None
            else None,
            2,
        ),
        "funding_mean": _clean(funding_mean, 8),
        "funding_abs_mean": _clean(funding_abs_mean, 8),
        "funding_pct_positive": _clean(funding_pct_positive),
        "funding_long_annual_cost": _clean(funding_annual_cost, 8),
        "max_gap_pct": _clean(max_gap),
        "tail_loss_1pct": _clean(abs(q01) if q01 is not None else None),
        "grid_step_bps": _clean(grid_step_bps, 4),
        "round_trip_cost_bps": _clean(round_trip_cost_bps, 4),
        "cost_to_edge": _clean(cost_to_edge),
        "buy_hold_return": _clean(buy_hold),
        "buy_hold_max_drawdown": _clean(buy_hold_dd),
        "range_low_30d": _clean(range_low, 8),
        "range_high_30d": _clean(range_high, 8),
    }
