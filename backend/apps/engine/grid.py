"""Realistic long-only grid simulator.

Design notes
------------
* Grid orders are limit orders; fills use maker fees and an adverse slippage
  assumption. Forced exits (stop / end of test) use taker fees.
* Only one pass over each OHLC bar is simulated. Within a bar the price is
  assumed to visit the nearest extreme first, then the farther extreme, then
  close. Orders created by a fill are not eligible again until the next bar.
  This avoids the optimistic "buy and sell the same swing instantly" bug.
* Gaps are handled at the open: orders already crossed by the opening print
  fill at the open (worse for the trader), not at their limit price.
* Positions are isolated-margin futures: margin used is notional / leverage,
  funding is charged on open inventory, and a stop-out/liquidation ends the run.
* The grid starts flat by default so measured returns come from grid cycling,
  not from buy-and-hold beta. `initial_inventory_pct` can change that, but the
  ranking engine deliberately defaults to zero.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from apps.engine.hybrid_gate import Gate

BARS_PER_DAY = {
    "1m": 1440,
    "5m": 288,
    "15m": 96,
    "30m": 48,
    "1h": 24,
    "2h": 12,
    "4h": 6,
    "6h": 4,
    "8h": 3,
    "12h": 2,
    "1d": 1,
}

TRADING_DAYS = 365
ATR_DAYS = 14
TREND_FAST_DAYS = 20
TREND_SLOW_DAYS = 50
MOMENTUM_DAYS = 10
MIN_FUNDING_HISTORY = 8
TP_COOLDOWN_DAYS = 1.0
# ATR multiples (stop, take-profit trail, range pad, widths) were calibrated on
# 4h bars. Per-bar true range grows with roughly the square root of the bar
# length (measured 4h/1h ratio across the top-100 universe: 2.05), so ATR is
# expressed in 4h-equivalent units on every interval.
REFERENCE_BARS_PER_DAY = 6
# Sell levels used while a gate asks to de-risk (inventory sold on the first bounce).
DERISK_SELL_LEVELS = 2


@dataclass(frozen=True)
class GridParams:
    grid_levels: int = 20
    range_lookback_days: int = 14
    rebalance_days: int = 7
    spacing_mode: str = "geometric"
    atr_multiplier: float = 1.0
    initial_inventory_pct: float = 0.0
    stop_loss_pct: float = 0.30
    max_utilization: float = 0.90
    # Adaptive overlays. Stop distance and the trailing take-profit multiple are
    # re-tuned per walk-forward fold; the remaining thresholds are structural.
    stop_atr_multiple: float = 2.0
    support_lookback_days: int = 30
    support_buffer_atr: float = 0.25
    max_range_atr: float = 12.0
    trail_up_atr_multiple: float = 1.0
    tp_activate_net_return: float = 0.02
    tp_atr_multiple: float = 2.0
    range_position_days: int = 30
    range_pos_high: float = 0.90
    range_pos_low: float = 0.10
    funding_z_threshold: float = 2.0
    funding_z_lookback: int = 90
    funding_cut_scale: float = 0.50
    # Exposure cap as a multiple of equity. It is always at least as tight as
    # leverage * max_utilization, so a single rung can never exceed capital.
    max_inventory_pct: float = 1.0
    # Portfolio peak-to-trough stop. Zero keeps the per-position stop as the
    # only hard stop so historical walk-forward results stay comparable.
    max_total_drawdown: float = 0.0
    # Range construction and rebalancing. "adaptive" reviews the range once
    # per range_review_hours and rebalances on volatility, breakout, or regime
    # shifts; every rebalance respects rebalance_cooldown_hours.
    range_mode: str = "percentile"
    rebalance_mode: str = "fixed"
    range_review_hours: float = 24.0
    rebalance_cooldown_hours: float = 12.0
    volatility_shift_ratio: float = 0.5
    breakout_atr_multiple: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "grid_levels": self.grid_levels,
            "range_lookback_days": self.range_lookback_days,
            "rebalance_days": self.rebalance_days,
            "spacing_mode": self.spacing_mode,
            "atr_multiplier": self.atr_multiplier,
            "initial_inventory_pct": self.initial_inventory_pct,
            "stop_loss_pct": self.stop_loss_pct,
            "max_utilization": self.max_utilization,
            "stop_atr_multiple": self.stop_atr_multiple,
            "support_lookback_days": self.support_lookback_days,
            "support_buffer_atr": self.support_buffer_atr,
            "max_range_atr": self.max_range_atr,
            "trail_up_atr_multiple": self.trail_up_atr_multiple,
            "tp_activate_net_return": self.tp_activate_net_return,
            "tp_atr_multiple": self.tp_atr_multiple,
            "range_position_days": self.range_position_days,
            "range_pos_high": self.range_pos_high,
            "range_pos_low": self.range_pos_low,
            "funding_z_threshold": self.funding_z_threshold,
            "funding_z_lookback": self.funding_z_lookback,
            "funding_cut_scale": self.funding_cut_scale,
            "max_inventory_pct": self.max_inventory_pct,
            "max_total_drawdown": self.max_total_drawdown,
            "range_mode": self.range_mode,
            "rebalance_mode": self.rebalance_mode,
            "range_review_hours": self.range_review_hours,
            "rebalance_cooldown_hours": self.rebalance_cooldown_hours,
            "volatility_shift_ratio": self.volatility_shift_ratio,
            "breakout_atr_multiple": self.breakout_atr_multiple,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GridParams":
        allowed = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in data.items() if k in allowed})


@dataclass(frozen=True)
class SimulationConfig:
    capital: float = 1000.0
    leverage: float = 2.0
    maker_fee_rate: float = 0.0002
    taker_fee_rate: float = 0.0005
    slippage_bps: float = 1.0
    maintenance_margin_rate: float = 0.005
    interval: str = "4h"
    # Fill model. Zero fills a resting limit order when the bar merely touches
    # it; a positive value requires price to trade through the level by this
    # many basis points, which approximates losing queue priority.
    trade_through_bps: float = 0.0

    @property
    def bars_per_day(self) -> int:
        return BARS_PER_DAY.get(self.interval, 6)

    @property
    def periods_per_year(self) -> float:
        return self.bars_per_day * TRADING_DAYS

    @property
    def slip(self) -> float:
        return self.slippage_bps / 10_000.0

    @property
    def trade_through(self) -> float:
        return max(0.0, self.trade_through_bps) / 10_000.0


def buy_crosses(level: float, p0: float, p1: float, trade_through: float) -> bool:
    """A resting buy fills when the falling segment p0 -> p1 trades through it."""

    return level <= p0 and p1 <= level * (1.0 - trade_through)


def sell_crosses(level: float, p0: float, p1: float, trade_through: float) -> bool:
    """A resting sell fills when the rising segment p0 -> p1 trades through it."""

    return level >= p0 and p1 >= level * (1.0 + trade_through)


def max_position_notional(
    equity: float, config: SimulationConfig, params: GridParams
) -> float:
    """Largest allowed position notional for the current equity.

    The leverage/utilization cap and the inventory cap both apply; the tighter
    one wins. ``max_inventory_pct`` <= 0 disables the inventory cap.
    """

    equity = max(0.0, equity)
    cap = equity * config.leverage * params.max_utilization
    if params.max_inventory_pct > 0:
        cap = min(cap, equity * params.max_inventory_pct)
    return max(0.0, cap)


def inventory_in_profit(price: float, avg_entry: float, params: GridParams) -> bool:
    """Trailing take-profit arms once inventory is up ``tp_activate_net_return``."""

    return avg_entry > 0 and price >= avg_entry * (1.0 + params.tp_activate_net_return)


def range_width_cap(params: GridParams, atr: float) -> float:
    """Maximum ladder width in price units.

    The cap scales with the ATR multiplier so widening the pad (2x/3x/4x)
    remains a real width lever instead of being recentred to the same band.
    """

    if atr <= 0:
        return 0.0
    return max(params.max_range_atr, 6.0 * params.atr_multiplier) * atr


@dataclass
class SimulationResult:
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    grid_levels: list[float]
    stopped: bool
    liquidated: bool
    stop_reason: str = ""
    error: str = ""
    # Last equity of each UTC day, used for strategy-return correlation.
    daily_equity: dict[str, float] = field(default_factory=dict)


@dataclass
class _State:
    capital: float
    position_qty: float = 0.0
    avg_entry: float = 0.0
    realized_pnl: float = 0.0
    fees_paid: float = 0.0
    funding_paid: float = 0.0
    turnover: float = 0.0
    peak_equity: float = 0.0
    max_drawdown: float = 0.0
    max_position_notional: float = 0.0
    stopped: bool = False
    liquidated: bool = False
    stop_reason: str = ""
    funding_events: int = 0
    wins: int = 0
    losses: int = 0
    gross_profit: float = 0.0
    gross_loss: float = 0.0
    total_fills: int = 0
    closed_lots: int = 0
    bars_in_market: int = 0
    bars_in_range: int = 0
    utilization_sum: float = 0.0
    max_exposure_ratio: float = 0.0
    min_unrealized_pnl: float = 0.0
    max_unrealized_pnl: float = 0.0
    buy_fills: int = 0
    sell_fills: int = 0
    slippage_paid: float = 0.0
    bars: int = 0
    trail_up_shifts: int = 0
    trailing_tp_exits: int = 0
    funding_trims: int = 0
    funding_z_max: float = 0.0
    buy_blocks: int = 0
    hybrid_blocked_bars: int = 0
    hybrid_exits: int = 0
    rebalances: int = 0
    adaptive_rebalances: int = 0
    # Buy fees and funding carried by open inventory. Each sell releases its
    # pro-rata share, so per-trade results reconcile with net PnL.
    carry_cost: float = 0.0
    trades: list[dict[str, Any]] = field(default_factory=list)


def atr_scale(bars_per_day: int) -> float:
    """Factor that converts a per-bar ATR into 4h-equivalent units."""

    return math.sqrt(max(1, int(bars_per_day)) / REFERENCE_BARS_PER_DAY)


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce(
        [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
    )


def _mean(values: np.ndarray) -> float:
    return float(np.mean(values)) if values.size else 0.0


def _window(bars_per_day: int, days: float) -> int:
    return max(2, int(round(days * bars_per_day)))


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    if values.size == 0:
        return values.copy()
    cumulative = np.concatenate(([0.0], np.cumsum(values)))
    ends = np.arange(1, values.size + 1)
    starts = np.maximum(0, ends - window)
    return (cumulative[ends] - cumulative[starts]) / (ends - starts)


def _rolling_extreme(values: np.ndarray, window: int, want_max: bool) -> np.ndarray:
    """Causal rolling max/min in O(n) with a monotonic deque."""

    out = np.empty_like(values)
    indices: deque[int] = deque()
    for i, value in enumerate(values):
        while indices and (
            values[indices[-1]] <= value if want_max else values[indices[-1]] >= value
        ):
            indices.pop()
        indices.append(i)
        if indices[0] <= i - window:
            indices.popleft()
        out[i] = values[indices[0]]
    return out


def _ema(values: np.ndarray, span: int) -> np.ndarray:
    if values.size == 0:
        return values.copy()
    alpha = 2.0 / (span + 1.0)
    out = np.empty_like(values)
    out[0] = values[0]
    for i in range(1, values.size):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


@dataclass(frozen=True)
class MarketContext:
    atr: float = 0.0
    support: float = 0.0
    range_high: float = 0.0
    range_low: float = 0.0
    range_pos: float | None = None
    trend_up: bool = False
    momentum: float = 0.0


def _indicator_series(
    highs: np.ndarray,
    lows: np.ndarray,
    closes: np.ndarray,
    params: GridParams,
    bars_per_day: int,
) -> dict[str, np.ndarray]:
    tr = _true_range(highs, lows, closes)
    fast_span = _window(bars_per_day, TREND_FAST_DAYS)
    slow_span = _window(bars_per_day, TREND_SLOW_DAYS)
    fast = _ema(closes, fast_span)
    slow = _ema(closes, slow_span)
    lag = min(fast_span, closes.size)
    slow_lag = (
        np.concatenate((np.full(lag, slow[0]), slow[:-lag])) if lag < closes.size else slow.copy()
    )
    momentum_bars = _window(bars_per_day, MOMENTUM_DAYS)
    momentum = np.zeros(closes.size)
    if closes.size > momentum_bars:
        momentum[momentum_bars:] = (
            closes[momentum_bars:] / np.maximum(closes[:-momentum_bars], 1e-12) - 1.0
        )
    support = _rolling_extreme(lows, _window(bars_per_day, params.support_lookback_days), False)
    # Support measured before the latest bar. A close can never sit below a
    # minimum that includes its own low, so the breakdown test must compare the
    # close against the support that existed before that bar printed.
    support_prior = (
        np.concatenate(([support[0]], support[:-1])) if support.size else support.copy()
    )
    return {
        "atr": _rolling_mean(tr, _window(bars_per_day, ATR_DAYS)) * atr_scale(bars_per_day),
        "support": support,
        "support_prior": support_prior,
        "range_high": _rolling_extreme(highs, _window(bars_per_day, params.range_position_days), True),
        "range_low": _rolling_extreme(lows, _window(bars_per_day, params.range_position_days), False),
        "trend_up": ((fast > slow) & (fast >= slow_lag)).astype(bool),
        "momentum": momentum,
    }


def _range_position(price: float, range_low: float, range_high: float) -> float | None:
    width = range_high - range_low
    if width <= 1e-12:
        return None
    return min(1.0, max(0.0, (price - range_low) / width))


def market_context(
    hist_high: np.ndarray,
    hist_low: np.ndarray,
    hist_close: np.ndarray,
    price: float,
    params: GridParams,
    bars_per_day: int,
) -> MarketContext:
    """Causal market state used by the adaptive overlays and paper strategy."""

    if hist_close.size == 0:
        return MarketContext()
    series = _indicator_series(hist_high, hist_low, hist_close, params, bars_per_day)
    last = hist_close.size - 1
    range_high = float(series["range_high"][last])
    range_low = float(series["range_low"][last])
    return MarketContext(
        atr=float(series["atr"][last]),
        support=float(series["support_prior"][last]),
        range_high=range_high,
        range_low=range_low,
        range_pos=_range_position(price, range_low, range_high),
        trend_up=bool(series["trend_up"][last]),
        momentum=float(series["momentum"][last]),
    )


def _funding_z_series(rates: np.ndarray, lookback: int) -> np.ndarray:
    """Causal z-score of each funding print versus the previous ``lookback``."""

    z = np.zeros(rates.size)
    if rates.size == 0:
        return z
    lookback = max(1, int(lookback))
    # Rolling sums over the ``lookback`` prints strictly before each index.
    sums = np.concatenate(([0.0], np.cumsum(rates)))
    squares = np.concatenate(([0.0], np.cumsum(rates * rates)))
    ends = np.arange(rates.size)
    starts = np.maximum(0, ends - lookback)
    counts = ends - starts
    valid = counts >= MIN_FUNDING_HISTORY
    if not np.any(valid):
        return z
    n = counts[valid].astype(float)
    total = sums[ends[valid]] - sums[starts[valid]]
    total_sq = squares[ends[valid]] - squares[starts[valid]]
    mean = total / n
    variance = np.maximum(0.0, (total_sq - n * mean * mean) / (n - 1.0))
    std = np.sqrt(variance)
    scores = np.where(std <= 1e-12, 0.0, (rates[valid] - mean) / np.where(std <= 1e-12, 1.0, std))
    z[valid] = scores
    return z


def funding_zscore(events: list[dict[str, Any]], lookback: int) -> float | None:
    """Latest funding z-score, or ``None`` when history is too short."""

    rates = np.array([float(event["rate"]) for event in events], dtype=float)
    if rates.size < MIN_FUNDING_HISTORY + 1:
        return None
    return float(_funding_z_series(rates, lookback)[-1])


def _series_key(params: GridParams) -> tuple[int, int, int]:
    return (
        int(params.support_lookback_days),
        int(params.range_position_days),
        int(params.funding_z_lookback),
    )


@dataclass
class PreparedMarket:
    """Price arrays and causal indicator series shared by many simulations.

    Every indicator is causal, so a series computed over the full history is
    identical to one computed over any prefix of it. Walk-forward candidates
    therefore slice these arrays instead of recomputing them, which removes
    most of the per-simulation overhead on 1h data.
    """

    times: list[datetime]
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    series: dict[str, np.ndarray]
    funding_rows: list[dict[str, Any]]
    funding_z: np.ndarray
    key: tuple[int, int, int]
    bars_per_day: int

    def covers(self, rows: list[dict[str, Any]], params: GridParams, bars_per_day: int) -> bool:
        count = len(rows)
        return (
            self.key == _series_key(params)
            and self.bars_per_day == bars_per_day
            and 0 < count <= len(self.times)
            and rows[-1]["time"] == self.times[count - 1]
        )


def prepare_market(
    rows: list[dict[str, Any]],
    funding_rows: list[dict[str, Any]],
    params: GridParams,
    bars_per_day: int,
) -> PreparedMarket:
    times = [row["time"] for row in rows]
    high = np.array([row["high"] for row in rows], dtype=float)
    low = np.array([row["low"] for row in rows], dtype=float)
    close = np.array([row["close"] for row in rows], dtype=float)
    ordered_funding = sorted(funding_rows, key=lambda r: r["time"])
    rates = np.array([float(event["rate"]) for event in ordered_funding], dtype=float)
    return PreparedMarket(
        times=times,
        open=np.array([row["open"] for row in rows], dtype=float),
        high=high,
        low=low,
        close=close,
        series=_indicator_series(high, low, close, params, bars_per_day),
        funding_rows=ordered_funding,
        funding_z=_funding_z_series(rates, params.funding_z_lookback),
        key=_series_key(params),
        bars_per_day=bars_per_day,
    )


def _build_grid(
    hist_high: np.ndarray,
    hist_low: np.ndarray,
    hist_close: np.ndarray,
    current_price: float,
    params: GridParams,
    bars_per_day: int,
) -> tuple[np.ndarray, float, float]:
    lookback = max(20, min(len(hist_close), params.range_lookback_days * bars_per_day))
    highs = hist_high[-lookback:]
    lows = hist_low[-lookback:]
    closes = hist_close[-lookback:]

    tr = _true_range(highs, lows, closes)
    atr_window = min(len(tr), ATR_DAYS * bars_per_day)
    atr = _mean(tr[-atr_window:]) * atr_scale(bars_per_day)

    support = 0.0
    if hist_low.size:
        support_window = min(len(hist_low), _window(bars_per_day, params.support_lookback_days))
        support = float(np.min(hist_low[-support_window:]))
    min_width = max(4.0 * atr, current_price * 0.03)

    if params.range_mode == "price_centered":
        # Symmetric window around the live price: no dependence on where the
        # recent percentile band happened to sit.
        half_width = max(params.atr_multiplier * atr * 3.0, current_price * 0.015)
        lower = current_price - half_width
        upper = current_price + half_width
    elif params.range_mode == "atr_support":
        # Floor at structural support, ceiling at the recent swing high plus an
        # ATR pad so breakouts still have sell rungs to fill.
        resistance = float(np.max(highs)) if highs.size else current_price
        floor = (
            support + params.support_buffer_atr * atr
            if support > 0
            else current_price - min_width / 2.0
        )
        lower = min(floor, current_price)
        upper = max(resistance, current_price) + max(
            params.atr_multiplier * atr, current_price * 0.001
        )
    else:
        raw_low = float(np.percentile(lows, 5))
        raw_high = float(np.percentile(highs, 95))
        pad = max(atr * params.atr_multiplier, current_price * 0.001)
        lower = raw_low - pad
        upper = raw_high + pad

    if upper - lower < min_width:
        center = (upper + lower) / 2.0
        lower, upper = center - min_width / 2.0, center + min_width / 2.0

    if current_price < lower:
        shift = lower - current_price
        lower -= shift
        upper -= shift
    elif current_price > upper:
        shift = current_price - upper
        lower += shift
        upper += shift

    # Keep the grid floor above structural support so the ladder does not buy
    # into a breakdown. If price already sits below support the floor is left
    # alone; buy gating in the simulator/strategy handles that regime.
    if support > 0:
        support_floor = support + params.support_buffer_atr * atr
        if lower < support_floor < current_price:
            lower = support_floor

    # Safety cap in ATR units (scaled by the ATR multiplier; see range_width_cap).
    max_width = range_width_cap(params, atr)
    if max_width > 0 and upper - lower > max_width:
        if params.range_mode == "atr_support":
            # Clip the top, never the structural floor, and always keep the
            # live price inside the band.
            upper = max(lower + max_width, current_price)
        else:
            lower = current_price - max_width / 2.0
            upper = current_price + max_width / 2.0

    lower = max(lower, current_price * 0.05)
    if upper <= lower:
        upper = lower * 1.03

    n = max(3, int(params.grid_levels))
    if params.spacing_mode == "arithmetic":
        levels = np.linspace(lower, upper, n)
    else:
        levels = np.geomspace(lower, upper, n)
    return levels, float(levels[0]), float(levels[-1])


def build_grid_levels(
    hist_high: np.ndarray,
    hist_low: np.ndarray,
    hist_close: np.ndarray,
    current_price: float,
    params: GridParams,
    bars_per_day: int,
) -> tuple[np.ndarray, float, float]:
    """Public entry point used by paper/live strategy state machines."""
    return _build_grid(hist_high, hist_low, hist_close, current_price, params, bars_per_day)


def intrabar_path(
    bar_open: float, bar_high: float, bar_low: float, bar_close: float
) -> list[float]:
    """Conservative OHLC path used by backtest and paper execution.

    Visit the nearer extreme first, then the farther extreme, then the close.
    This avoids assuming the most favorable ordering of intrabar moves.
    """
    first_extreme = bar_low if (bar_open - bar_low) <= (bar_high - bar_open) else bar_high
    second_extreme = bar_high if first_extreme == bar_low else bar_low
    path = [bar_open]
    for point in (first_extreme, second_extreme, bar_close):
        if not math.isclose(point, path[-1], rel_tol=0, abs_tol=1e-12):
            path.append(point)
    return path


def _sampled_curve(
    times: list[datetime],
    equity: list[float],
    price: list[float],
    drawdown: list[float],
    range_low: list[float],
    range_high: list[float],
    max_points: int = 320,
) -> list[dict[str, Any]]:
    count = len(equity)
    if count == 0:
        return []
    if count <= max_points:
        indices = range(count)
    else:
        indices = np.linspace(0, count - 1, max_points).astype(int)
    return [
        {
            "t": times[i].isoformat(),
            "equity": round(equity[i], 4),
            "price": round(price[i], 8),
            "dd": round(drawdown[i], 6),
            "low": round(range_low[i], 8),
            "high": round(range_high[i], 8),
        }
        for i in indices
    ]


def _recovery_stats(
    equity: np.ndarray, bars_per_day: int
) -> dict[str, Any]:
    """How long the worst drawdown took to recover, if it did."""

    if equity.size < 3:
        return {
            "recovery_bars": 0,
            "recovery_days": 0.0,
            "max_drawdown_duration_days": 0.0,
            "recovered": True,
        }
    peaks = np.maximum.accumulate(equity)
    drawdown = 1.0 - equity / np.maximum(peaks, 1e-12)
    trough = int(np.argmax(drawdown))
    if drawdown[trough] <= 1e-9:
        return {
            "recovery_bars": 0,
            "recovery_days": 0.0,
            "max_drawdown_duration_days": 0.0,
            "recovered": True,
        }
    peak_idx = int(np.argmax(equity[: trough + 1]))
    recovery_idx = next(
        (i for i in range(trough + 1, equity.size) if equity[i] >= peaks[trough]),
        None,
    )
    recovered = recovery_idx is not None
    recovery_bars = (recovery_idx - trough) if recovered else equity.size - 1 - trough
    duration_bars = (recovery_idx if recovered else equity.size - 1) - peak_idx
    return {
        "recovery_bars": int(recovery_bars),
        "recovery_days": round(recovery_bars / max(1, bars_per_day), 2),
        "max_drawdown_duration_days": round(duration_bars / max(1, bars_per_day), 2),
        "recovered": recovered,
    }


def simulate_grid(
    rows: list[dict[str, Any]],
    funding_rows: list[dict[str, Any]],
    trade_start: datetime,
    params: GridParams,
    config: SimulationConfig,
    *,
    gate: Gate | None = None,
    prepared: PreparedMarket | None = None,
) -> SimulationResult:
    """Run a single grid path from ``trade_start`` through the end of ``rows``.

    ``gate`` optionally applies the hybrid trend filter: new buys are disarmed
    on blocked bars and inventory is flattened at the open of a bar that
    follows a confirmed exit.

    ``prepared`` may hold arrays and indicators for a longer history of which
    ``rows`` is a prefix (see ``prepare_market``); it is only an optimisation
    and never changes the result.
    """

    if len(rows) < 10:
        return SimulationResult({}, [], [], [], False, False, error="insufficient candles")

    bpd = config.bars_per_day
    if prepared is None or not prepared.covers(rows, params, bpd):
        prepared = prepare_market(rows, funding_rows, params, bpd)
    count = len(rows)
    times = prepared.times[:count]
    o = prepared.open[:count]
    h = prepared.high[:count]
    low = prepared.low[:count]
    c = prepared.close[:count]

    start_idx = bisect_left(times, trade_start)
    if start_idx >= count:
        start_idx = None
    if start_idx is None or start_idx < 20:
        return SimulationResult({}, [], [], [], False, False, error="not enough warmup data")

    warmup = max(params.range_lookback_days * bpd, ATR_DAYS * bpd)
    if start_idx < min(warmup // 2, 20):
        return SimulationResult({}, [], [], [], False, False, error="warmup window too short")

    state = _State(capital=config.capital, peak_equity=config.capital)
    rebalance_bars = max(1, int(params.rebalance_days * bpd))
    fee_buy = config.maker_fee_rate
    fee_sell = config.maker_fee_rate
    slip = config.slip

    orders: dict[int, dict[str, Any]] = {}
    levels = np.array([])
    active_low = active_high = 0.0
    curve_t: list[datetime] = []
    curve_eq: list[float] = []
    curve_px: list[float] = []
    curve_dd: list[float] = []
    curve_low: list[float] = []
    curve_high: list[float] = []
    funding_idx = 0
    funding_rows = prepared.funding_rows
    funding_z = prepared.funding_z
    current_funding_z = 0.0
    prev_close = float(c[start_idx - 1]) if start_idx > 0 else float(o[start_idx])
    trade_through = config.trade_through

    series = prepared.series
    atr_series = series["atr"].tolist()
    support_series = series["support_prior"].tolist()
    range_high_series = series["range_high"].tolist()
    range_low_series = series["range_low"].tolist()
    trend_series = series["trend_up"].tolist()
    momentum_series = series["momentum"].tolist()
    o_list = o.tolist()
    h_list = h.tolist()
    low_list = low.tolist()
    c_list = c.tolist()
    buy_scale = 1.0
    allow_buys = True
    buy_blocked_until = -1
    tp_active = False
    peak_price = 0.0
    stop_reference = 0.0
    epoch_stop = 0.0
    tp_cooldown_bars = max(1, int(TP_COOLDOWN_DAYS * bpd))
    review_bars = max(1, int(params.range_review_hours * bpd / 24.0))
    cooldown_bars = max(1, int(params.rebalance_cooldown_hours * bpd / 24.0))
    baseline_idx = max(0, start_idx - 1)
    baseline_atr = float(series["atr"][baseline_idx])
    baseline_trend = bool(series["trend_up"][baseline_idx])
    baseline_momentum = float(series["momentum"][baseline_idx])
    last_rebalance_i = start_idx - 1
    last_review_i = start_idx - 1

    def equity_at(price: float, position_qty: float, avg_entry: float) -> float:
        unreal = (price - avg_entry) * position_qty if position_qty > 0 else 0.0
        return (
            state.capital
            + state.realized_pnl
            - state.fees_paid
            - state.funding_paid
            + unreal
        )

    def record_equity(price: float, timestamp: datetime) -> None:
        equity = equity_at(price, state.position_qty, state.avg_entry)
        state.peak_equity = max(state.peak_equity, equity)
        dd = 0.0 if state.peak_equity <= 0 else 1.0 - equity / state.peak_equity
        state.max_drawdown = max(state.max_drawdown, dd)
        notional = state.position_qty * price
        state.utilization_sum += notional / max(equity * config.leverage, 1e-9)
        state.max_exposure_ratio = max(state.max_exposure_ratio, notional / max(equity, 1e-9))
        if state.position_qty > 0:
            unrealized = (price - state.avg_entry) * state.position_qty
            state.min_unrealized_pnl = min(state.min_unrealized_pnl, unrealized)
            state.max_unrealized_pnl = max(state.max_unrealized_pnl, unrealized)
        state.bars_in_market += 1 if state.position_qty > 0 else 0
        state.bars_in_range += 1 if active_low <= price <= active_high else 0
        curve_t.append(timestamp)
        curve_eq.append(equity)
        curve_px.append(price)
        curve_dd.append(dd)
        curve_low.append(active_low)
        curve_high.append(active_high)

    def release_sell(
        qty: float,
        fill: float,
        fee_rate: float,
        timestamp: datetime,
        level: int | None,
        reason: str,
    ) -> None:
        """Book a sell of ``qty`` at ``fill`` and its share of carried costs."""

        notional = fill * qty
        fee = notional * fee_rate
        pnl = (fill - state.avg_entry) * qty
        share = qty / state.position_qty if state.position_qty > 1e-12 else 1.0
        carried = state.carry_cost * min(1.0, share)
        state.carry_cost -= carried
        state.realized_pnl += pnl
        state.fees_paid += fee
        state.turnover += notional
        state.slippage_paid += notional * slip
        state.sell_fills += 1
        state.position_qty -= qty
        if state.position_qty <= 1e-12:
            state.position_qty = 0.0
            state.avg_entry = 0.0
            state.carry_cost = 0.0
        state.total_fills += 1
        state.closed_lots += 1
        # Trade result includes the sell fee plus the buy fees and funding the
        # sold inventory carried, so gross profit - gross loss equals net PnL.
        net = pnl - fee - carried
        if net >= 0:
            state.wins += 1
            state.gross_profit += net
        else:
            state.losses += 1
            state.gross_loss += -net
        state.trades.append(
            {
                "t": timestamp.isoformat(),
                "side": "sell",
                "price": round(fill, 8),
                "qty": round(qty, 8),
                "fee": round(fee, 6),
                "realized_pnl": round(pnl - fee, 6),
                "net_pnl": round(net, 6),
                "level": level,
                "reason": reason,
            }
        )

    def close_all(price: float, timestamp: datetime, reason: str) -> None:
        qty = state.position_qty
        if qty <= 0:
            return
        release_sell(qty, price * (1.0 - slip), config.taker_fee_rate, timestamp, None, reason)

    def position_notional(price: float) -> float:
        return state.position_qty * price

    def max_buy_qty(price: float) -> float:
        # Mirrors PaperExecutionAdapter.max_buy_qty: the funding-crowding scale
        # is applied once, when the ladder is sized, not again per fill.
        eq = equity_at(price, state.position_qty, state.avg_entry)
        used_margin = position_notional(price) / config.leverage
        free_margin = max(0.0, eq - used_margin)
        max_notional = max_position_notional(eq, config, params)
        budget = min(
            max_notional - position_notional(price),
            free_margin * config.leverage * 0.98,
        )
        return max(0.0, budget / price) if price > 0 else 0.0

    def execute_buy(level_idx: int | None, price: float, timestamp: datetime, reason: str) -> bool:
        if not allow_buys:
            return False
        order = orders.get(level_idx) if level_idx is not None else None
        qty = order["qty"] if order else 0.0
        if qty <= 0:
            return False
        qty = min(qty, max_buy_qty(price))
        if qty <= 1e-12:
            return False
        fill = price * (1.0 + slip)
        notional = fill * qty
        fee = notional * (
            config.maker_fee_rate if reason in {"grid", "gap"} else config.taker_fee_rate
        )
        new_qty = state.position_qty + qty
        state.avg_entry = (
            (state.avg_entry * state.position_qty + fill * qty) / new_qty if new_qty > 0 else 0.0
        )
        state.position_qty = new_qty
        state.fees_paid += fee
        state.carry_cost += fee
        state.turnover += notional
        state.slippage_paid += notional * slip
        state.buy_fills += 1
        state.total_fills += 1
        state.max_position_notional = max(state.max_position_notional, position_notional(fill))
        state.trades.append(
            {
                "t": timestamp.isoformat(),
                "side": "buy",
                "price": round(fill, 8),
                "qty": round(qty, 8),
                "fee": round(fee, 6),
                "realized_pnl": 0.0,
                "level": level_idx,
                "reason": reason,
            }
        )
        if order:
            if order["qty"] - qty <= 1e-12:
                orders.pop(level_idx, None)
            else:
                order["qty"] -= qty
        return True

    def execute_sell(level_idx: int, price: float, timestamp: datetime, reason: str) -> bool:
        order = orders.get(level_idx)
        qty = min(order["qty"], state.position_qty) if order else 0.0
        if qty <= 1e-12:
            return False
        release_sell(qty, price * (1.0 - slip), config.maker_fee_rate, timestamp, level_idx, reason)
        if order["qty"] - qty <= 1e-12:
            orders.pop(level_idx, None)
        else:
            order["qty"] -= qty
        return True

    def reduce_position(price: float, qty: float, timestamp: datetime, reason: str) -> None:
        """Trim inventory without a grid slot (funding / range risk cuts)."""

        qty = min(qty, state.position_qty)
        if qty <= 1e-12:
            return
        release_sell(qty, price * (1.0 - slip), config.taker_fee_rate, timestamp, None, reason)

    def rebuild_orders(timestamp: datetime, price: float) -> None:
        nonlocal prev_close
        orders.clear()
        if state.stopped:
            return
        lower_idx = int(np.searchsorted(levels, price, side="left"))
        upper_idx = int(np.searchsorted(levels, price, side="right"))
        eq = equity_at(price, state.position_qty, state.avg_entry)
        notional = state.position_qty * price
        used_margin = notional / config.leverage
        max_notional = max_position_notional(eq, config, params)
        buy_budget = max(
            0.0,
            min(max_notional - notional, max(0.0, eq - used_margin) * config.leverage * 0.98),
        ) * buy_scale
        buy_indices = list(range(0, lower_idx)) if allow_buys else []
        sell_indices = list(range(upper_idx, len(levels)))
        if derisking:
            sell_indices = sell_indices[:DERISK_SELL_LEVELS]
        if buy_indices and buy_budget > 0:
            slot_notional = buy_budget / len(buy_indices)
            for i in buy_indices:
                qty = slot_notional / max(levels[i], 1e-12)
                if qty > 0:
                    orders[i] = {"side": "buy", "qty": qty}
        if sell_indices and state.position_qty > 0:
            per_level = state.position_qty / len(sell_indices)
            for i in sell_indices:
                if per_level > 0:
                    orders[i] = {"side": "sell", "qty": per_level}

    def rebalance_trigger(
        bar_index: int, atr_now: float, trend_now: bool, price: float
    ) -> str:
        """Why the ladder should be rebuilt at this bar, or "" for none.

        Adaptive mode reviews at most once per ``range_review_hours`` and never
        within ``rebalance_cooldown_hours`` of the previous rebalance. The
        weekly schedule remains a fallback so quiet ranges still refresh.
        """

        if params.rebalance_mode != "adaptive":
            return "scheduled" if (bar_index - start_idx) % rebalance_bars == 0 else ""
        if bar_index == start_idx:
            return "initialize"
        if bar_index - last_rebalance_i < cooldown_bars:
            return ""
        if bar_index - last_review_i < review_bars:
            return ""
        if (
            baseline_atr > 0
            and atr_now > 0
            and abs(atr_now - baseline_atr) / baseline_atr
            >= params.volatility_shift_ratio
        ):
            return "volatility_shift"
        if (
            active_high > 0
            and atr_now > 0
            and (
                price > active_high + params.breakout_atr_multiple * atr_now
                or price < active_low - params.breakout_atr_multiple * atr_now
            )
        ):
            return "range_breakout"
        if bool(trend_now) != baseline_trend:
            return "regime_shift"
        return "scheduled" if bar_index - last_rebalance_i >= rebalance_bars else ""

    # Initial inventory (disabled by default in the ranking engine). It also
    # waits for the first bar the hybrid gate allows, so a gated backtest never
    # starts long against a disarmed entry.
    first = start_idx
    first_allowed = gate is None or gate.allow.get(times[first], True)
    if params.initial_inventory_pct > 0 and first_allowed:
        price = float(o[first]) * (1.0 + slip)
        eq = config.capital
        budget = eq * config.leverage * params.max_utilization * params.initial_inventory_pct
        qty = min(budget / price, max_buy_qty(float(o[first])))
        budget = qty * price
        if qty > 0:
            fee = budget * config.taker_fee_rate
            state.position_qty = qty
            state.avg_entry = price
            state.fees_paid += fee
            state.carry_cost += fee
            state.turnover += budget
            state.slippage_paid += budget * slip
            state.buy_fills += 1
            state.max_position_notional = budget
            state.total_fills += 1
            state.trades.append(
                {
                    "t": times[first].isoformat(),
                    "side": "buy",
                    "price": round(price, 8),
                    "qty": round(qty, 8),
                    "fee": round(fee, 6),
                    "realized_pnl": 0.0,
                    "level": None,
                    "reason": "initial",
                }
            )

    stop_equity = config.capital * (1.0 - params.stop_loss_pct)
    hybrid_blocked = False
    derisking = False

    for i in range(start_idx, count):
        if state.stopped:
            break
        bar_open = o_list[i]
        bar_high = h_list[i]
        bar_low = low_list[i]
        bar_close = c_list[i]
        timestamp = times[i]

        # Funding events that occurred since the previous bar.
        while funding_idx < len(funding_rows) and funding_rows[funding_idx]["time"] <= timestamp:
            event = funding_rows[funding_idx]
            funding_idx += 1
            current_funding_z = float(funding_z[funding_idx - 1])
            state.funding_z_max = max(state.funding_z_max, current_funding_z)
            # Prints at the start instant settle before the opening inventory
            # exists, so only later prints are charged.
            if state.position_qty > 0 and event["time"] > times[start_idx]:
                mark = event.get("mark_price") or bar_open
                cost = state.position_qty * float(mark) * float(event["rate"])
                state.funding_paid += cost
                state.carry_cost += cost
                state.funding_events += 1

        # Adaptive regime gates, computed from completed data only (bar i-1).
        ctx_i = max(0, i - 1)
        atr_now = atr_series[ctx_i]
        support_now = support_series[ctx_i]
        range_high_now = range_high_series[ctx_i]
        range_low_now = range_low_series[ctx_i]
        trend_up = bool(trend_series[ctx_i])
        momentum = momentum_series[ctx_i]
        previous_allowed = allow_buys
        range_pos = _range_position(prev_close, range_low_now, range_high_now)
        crowded = (
            params.funding_z_threshold > 0
            and current_funding_z >= params.funding_z_threshold
            and (momentum < 0 or not trend_up)
        )
        buy_scale = (
            max(0.0, min(1.0, params.funding_cut_scale)) if crowded else 1.0
        )
        below_support = (
            support_now > 0
            and prev_close < support_now - params.support_buffer_atr * atr_now
        )
        upper_risk = (
            range_pos is not None
            and range_pos >= params.range_pos_high
            and (not trend_up or momentum < 0)
        )
        gate_allow = gate.allow.get(timestamp) if gate is not None else None
        allow_buys = not (
            i < buy_blocked_until or crowded or below_support or upper_risk
        ) and gate_allow is not False
        if gate_allow is False:
            state.hybrid_blocked_bars += 1
        if not allow_buys:
            for idx in [key for key, order in orders.items() if order["side"] == "buy"]:
                orders.pop(idx, None)
            state.buy_blocks += 1

        # Hybrid exit: a confirmed 15m close below the 1h EMA flattens at the
        # bar open, and buys stay disarmed until the gate re-arms.
        exit_flag = gate.exit.get(timestamp) if gate is not None else None
        if exit_flag:
            close_all(
                bar_open,
                timestamp,
                exit_flag if isinstance(exit_flag, str) else "hybrid_exit",
            )
            state.hybrid_exits += 1
            orders.clear()
            rebuild_orders(timestamp, bar_open)

        # De-risk: offer open inventory on the nearest sell levels while the
        # gate asks for it, and restore the full ladder when it stops.
        wants_derisk = bool(gate is not None and gate.derisk.get(timestamp))
        if wants_derisk != derisking:
            derisking = wants_derisk
            rebuild_orders(timestamp, bar_open)

        if gate_allow is not None:
            if gate_allow:
                if hybrid_blocked:
                    # Re-arm the ladder on the first gate-allowed bar.
                    hybrid_blocked = False
                    rebuild_orders(timestamp, bar_open)
            else:
                hybrid_blocked = True

        reason = rebalance_trigger(i, atr_now, trend_up, bar_open)
        if (
            params.rebalance_mode == "adaptive"
            and i - last_review_i >= review_bars
            and i - last_rebalance_i >= cooldown_bars
        ):
            # The daily review happened on this bar; don't review again today.
            last_review_i = i
        if reason:
            levels, active_low, active_high = _build_grid(
                h[:i], low[:i], c[:i], bar_open, params, bpd
            )
            rebuild_orders(timestamp, bar_open)
            structure_stop = (
                active_low - params.stop_atr_multiple * atr_now
                if atr_now > 0 and active_low > 0
                else 0.0
            )
            # While inventory is open a rebalance may only raise the stop.
            epoch_stop = (
                max(epoch_stop, structure_stop)
                if state.position_qty > 0
                else structure_stop
            )
            last_rebalance_i = i
            last_review_i = i
            baseline_atr = atr_now
            baseline_trend = trend_up
            baseline_momentum = momentum
            state.rebalances += 1
            if reason in {"volatility_shift", "range_breakout", "regime_shift"}:
                state.adaptive_rebalances += 1

        # Opening gap: orders crossed before the bar's path begins.
        new_orders_this_bar: list[tuple[int, str, float]] = []

        def queue_order(level_idx: int, side: str, qty: float) -> None:
            if 0 <= level_idx < len(levels) and qty > 0:
                new_orders_this_bar.append((level_idx, side, qty))

        if prev_close > bar_open:
            for idx in list(orders):
                order = orders.get(idx)
                if (
                    order
                    and order["side"] == "buy"
                    and levels[idx] >= bar_open
                    and levels[idx] <= prev_close
                ):
                    before = state.position_qty
                    if execute_buy(idx, bar_open, timestamp, "gap"):
                        bought = state.position_qty - before
                        if bought > 0:
                            queue_order(idx + 1, "sell", bought)
        elif prev_close < bar_open:
            for idx in list(orders):
                order = orders.get(idx)
                if (
                    order
                    and order["side"] == "sell"
                    and levels[idx] <= bar_open
                    and levels[idx] >= prev_close
                ):
                    before = state.position_qty
                    if execute_sell(idx, bar_open, timestamp, "gap"):
                        sold = before - state.position_qty
                        if sold > 0:
                            queue_order(idx - 1, "buy", sold)

        # Conservative intrabar path: open -> nearer extreme -> farther extreme -> close.
        path = intrabar_path(bar_open, bar_high, bar_low, bar_close)

        # Captured so orders created by fills wait for the next bar.
        pending_orders = orders.copy()

        for p0, p1 in zip(path, path[1:]):
            if p1 < p0:
                for idx in sorted(pending_orders, reverse=True):
                    order = pending_orders.get(idx)
                    if not order or order["side"] != "buy":
                        continue
                    level_price = float(levels[idx])
                    if buy_crosses(level_price, p0, p1, trade_through):
                        before = state.position_qty
                        if execute_buy(idx, level_price, timestamp, "grid"):
                            new_qty = state.position_qty - before
                            if new_qty > 0 and idx + 1 < len(levels):
                                queue_order(idx + 1, "sell", new_qty)
                        pending_orders.pop(idx, None)
            elif p1 > p0:
                for idx in sorted(pending_orders):
                    order = pending_orders.get(idx)
                    if not order or order["side"] != "sell":
                        continue
                    level_price = float(levels[idx])
                    if sell_crosses(level_price, p0, p1, trade_through):
                        before = state.position_qty
                        if execute_sell(idx, level_price, timestamp, "grid"):
                            sold = before - state.position_qty
                            if sold > 0 and idx - 1 >= 0:
                                queue_order(idx - 1, "buy", sold)
                        pending_orders.pop(idx, None)

        prev_close = bar_close
        # Exits and ladder shifts use the ATR known before this bar (the same
        # value the paper strategy holds), never the bar's own range.
        atr_bar = atr_now
        skip_queue = False

        # Hard stop: ATR distance plus structural support invalidation. The
        # risk stop is evaluated before any profit-taking adjustment.
        if state.position_qty > 0:
            eq_low = equity_at(bar_low, state.position_qty, state.avg_entry)
            liq_price = state.avg_entry * (
                1.0 - 1.0 / config.leverage + config.maintenance_margin_rate
            )
            price_stop = -math.inf
            if epoch_stop > 0:
                # The stop sits stop_atr_multiple ATRs below the grid floor,
                # which `_build_grid` keeps above structural support. Breaking
                # that floor invalidates the range. The reference only ratchets
                # upward while inventory is open, so a decline cannot drag the
                # stop down with it.
                stop_reference = max(stop_reference, epoch_stop)
                price_stop = stop_reference
            drawdown_stop = False
            if params.max_total_drawdown > 0:
                peak = max(
                    state.peak_equity,
                    equity_at(bar_open, state.position_qty, state.avg_entry),
                )
                drawdown_low = 1.0 - eq_low / peak if peak > 0 else 0.0
                drawdown_stop = drawdown_low >= params.max_total_drawdown
            if (
                bar_low <= liq_price
                or eq_low <= stop_equity
                or bar_low <= price_stop
                or drawdown_stop
            ):
                if bar_low <= liq_price:
                    reason = "liquidation"
                elif drawdown_stop:
                    reason = "drawdown_stop"
                else:
                    reason = "stop_loss"
                trigger_price = (
                    min(bar_low, liq_price)
                    if reason == "liquidation"
                    else bar_low
                )
                close_all(max(trigger_price, 1e-12), timestamp, reason)
                state.stopped = True
                state.liquidated = reason == "liquidation"
                state.stop_reason = reason
                record_equity(bar_close, timestamp)
                break
        else:
            stop_reference = 0.0

        # Trailing take-profit: active only after the net-profit threshold is
        # reached, then exits on an ATR-sized retrace from the peak price.
        tp_triggered = False
        if tp_active and state.position_qty > 0 and peak_price > 0 and atr_bar > 0:
            trail_trigger = peak_price - params.tp_atr_multiple * atr_bar
            if bar_low <= trail_trigger:
                fill_price = max(min(bar_open, trail_trigger), 1e-12)
                close_all(fill_price, timestamp, "trailing_take_profit")
                state.trailing_tp_exits += 1
                tp_active = False
                peak_price = 0.0
                stop_reference = 0.0
                orders.clear()
                buy_blocked_until = i + tp_cooldown_bars
                skip_queue = True
                tp_triggered = True

        # Crowded-long funding: cut exposure when momentum confirms weakness.
        if not tp_triggered and crowded and state.position_qty > 0:
            eq_close = equity_at(bar_close, state.position_qty, state.avg_entry)
            cap_notional = (
                eq_close * config.leverage * params.max_utilization * buy_scale
            )
            excess = state.position_qty * bar_close - cap_notional
            if excess > max(1.0, cap_notional * 0.02):
                reduce_position(
                    bar_close, excess / max(bar_close, 1e-12), timestamp, "funding_trim"
                )
                state.funding_trims += 1
                orders.clear()
                rebuild_orders(timestamp, bar_close)
                skip_queue = True

        # Arm / update the trailing take-profit once the open inventory itself
        # is in profit. Arming on account equity would leave it permanently
        # armed after early gains and turn the grid into a trend-exit system.
        if not tp_triggered:
            if state.position_qty > 0:
                if not tp_active:
                    if inventory_in_profit(bar_close, state.avg_entry, params):
                        tp_active = True
                        peak_price = bar_high
                else:
                    peak_price = max(peak_price, bar_high)
            else:
                tp_active = False
                peak_price = 0.0

        # Trailing up: a confirmed breakout shifts the ladder, capped in width.
        if (
            not tp_triggered
            and atr_bar > 0
            and active_high > 0
            and trend_up
            and bar_close > range_high_now + params.trail_up_atr_multiple * atr_bar
        ):
            delta = bar_close - active_high
            new_low = float(levels[0]) + delta
            new_high = float(levels[-1]) + delta
            max_width = range_width_cap(params, atr_bar)
            if max_width > 0 and new_high - new_low > max_width:
                new_low = new_high - max_width
            if new_high > new_low > 0:
                if params.spacing_mode == "arithmetic":
                    levels = np.linspace(new_low, new_high, len(levels))
                else:
                    levels = np.geomspace(new_low, new_high, len(levels))
                active_low, active_high = new_low, new_high
                if atr_bar > 0 and active_low > 0:
                    epoch_stop = max(
                        epoch_stop,
                        active_low - params.stop_atr_multiple * atr_bar,
                    )
                state.trail_up_shifts += 1
                rebuild_orders(timestamp, bar_close)
                skip_queue = True

        # Re-arm the buy side as soon as the risk gates clear. Without this the
        # ladder stays empty until the next scheduled rebalance.
        if not skip_queue and not previous_allowed and allow_buys:
            rebuild_orders(timestamp, bar_close)
            skip_queue = True

        record_equity(bar_close, timestamp)
        state.bars += 1

        if skip_queue:
            continue

        for level_idx, side, qty in new_orders_this_bar:
            existing = orders.get(level_idx)
            if existing is None:
                orders[level_idx] = {"side": side, "qty": qty}
            elif existing["side"] == side:
                existing["qty"] += qty
            # Opposite-side collisions are intentionally ignored: the slot is
            # already occupied and creating a self-crossing order is invalid.

    # Close residual inventory so net return is fully realized.
    if state.position_qty > 0 and not state.stopped:
        close_all(float(c[-1]), times[-1], "end_of_test")
        record_equity(float(c[-1]), times[-1])

    equity_curve_raw = np.array(curve_eq, dtype=float)
    if equity_curve_raw.size == 0:
        return SimulationResult({}, [], [], list(levels), False, False, error="no equity data")

    final_equity = float(equity_curve_raw[-1])
    net_return = final_equity / config.capital - 1.0
    if equity_curve_raw.size >= 2:
        rets = np.diff(equity_curve_raw) / np.maximum(equity_curve_raw[:-1], 1e-9)
        sharpe = (
            float(np.mean(rets) / np.std(rets, ddof=1) * math.sqrt(config.periods_per_year))
            if np.std(rets, ddof=1) > 1e-12
            else 0.0
        )
        downside = rets[rets < 0]
        sortino = (
            float(np.mean(rets) / np.std(downside, ddof=1) * math.sqrt(config.periods_per_year))
            if downside.size > 1 and np.std(downside, ddof=1) > 1e-12
            else 0.0
        )
    else:
        sharpe = sortino = 0.0
    max_dd = float(state.max_drawdown)
    years = max(state.bars / (bpd * TRADING_DAYS), 1e-9)
    calmar = (net_return / years) / max_dd if max_dd > 1e-6 else 0.0
    profit_factor = (
        state.gross_profit / state.gross_loss
        if state.gross_loss > 1e-9
        else (float("inf") if state.gross_profit > 0 else 0.0)
    )
    if math.isinf(profit_factor):
        profit_factor = 99.0
    win_rate = state.wins / max(1, state.closed_lots)
    exposure = state.bars_in_market / max(1, state.bars)
    utilization = state.utilization_sum / max(1, state.bars)
    time_in_range = state.bars_in_range / max(1, state.bars)
    recovery = _recovery_stats(equity_curve_raw, bpd)
    closed_pnls = sorted(
        (
            float(trade.get("realized_pnl") or 0.0)
            for trade in state.trades
            if trade.get("side") == "sell"
        ),
        reverse=True,
    )
    positive_pnls = [pnl for pnl in closed_pnls if pnl > 0]
    positive_total = sum(positive_pnls)
    top_trade_share = positive_pnls[0] / positive_total if positive_total > 0 else 0.0
    top5_trade_share = (
        sum(positive_pnls[:5]) / positive_total if positive_total > 0 else 0.0
    )
    max_unrealized_loss = max(0.0, -state.min_unrealized_pnl)

    metrics = {
        "net_return": round(net_return, 6),
        "final_equity": round(final_equity, 4),
        "max_drawdown": round(max_dd, 6),
        "calmar": round(calmar, 4),
        "sharpe": round(sharpe, 4),
        "sortino": round(sortino, 4),
        "profit_factor": round(profit_factor, 4),
        "win_rate": round(win_rate, 6),
        "trades": state.total_fills,
        "closed_lots": state.closed_lots,
        "fees_paid": round(state.fees_paid, 4),
        "funding_paid": round(state.funding_paid, 4),
        "funding_events": state.funding_events,
        "realized_pnl": round(state.realized_pnl, 4),
        "gross_profit": round(state.gross_profit, 6),
        "gross_loss": round(state.gross_loss, 6),
        "turnover": round(state.turnover, 2),
        "slippage_paid": round(state.slippage_paid, 4),
        "exposure": round(exposure, 6),
        "avg_utilization": round(utilization, 6),
        "avg_exposure_ratio": round(utilization * config.leverage, 6),
        "max_exposure_ratio": round(state.max_exposure_ratio, 6),
        "max_unrealized_loss": round(max_unrealized_loss, 4),
        "max_unrealized_loss_pct": round(max_unrealized_loss / max(config.capital, 1e-9), 6),
        "time_in_range": round(time_in_range, 6),
        "time_outside_range": round(1.0 - time_in_range, 6),
        "max_position_notional": round(state.max_position_notional, 4),
        "max_position_pct": round(
            state.max_position_notional / max(config.capital, 1e-9), 6
        ),
        "buy_fills": state.buy_fills,
        "sell_fills": state.sell_fills,
        "cycle_completion_ratio": round(
            state.closed_lots / max(1, state.buy_fills), 6
        ),
        "top_trade_share": round(top_trade_share, 6),
        "top5_trade_share": round(top5_trade_share, 6),
        "recovery_bars": recovery["recovery_bars"],
        "recovery_days": recovery["recovery_days"],
        "max_drawdown_duration_days": recovery["max_drawdown_duration_days"],
        "recovered": recovery["recovered"],
        "stopped": state.stopped,
        "liquidated": state.liquidated,
        "stop_reason": state.stop_reason,
        "bars": state.bars,
        "grid_resets": max(1, state.rebalances),
        "rebalances": state.rebalances,
        "adaptive_rebalances": state.adaptive_rebalances,
        "trail_up_shifts": state.trail_up_shifts,
        "trailing_tp_exits": state.trailing_tp_exits,
        "funding_trims": state.funding_trims,
        "funding_z_max": round(state.funding_z_max, 4),
        "buy_blocks": state.buy_blocks,
        "hybrid_blocked_bars": state.hybrid_blocked_bars,
        "hybrid_exits": state.hybrid_exits,
    }

    curve = _sampled_curve(
        curve_t, curve_eq, curve_px, curve_dd, curve_low, curve_high
    )
    daily_equity: dict[str, float] = {}
    for moment, value in zip(curve_t, curve_eq):
        daily_equity[moment.date().isoformat()] = round(float(value), 6)
    return SimulationResult(
        metrics=metrics,
        equity_curve=curve,
        trades=state.trades[-200:],
        grid_levels=[float(x) for x in levels],
        stopped=state.stopped,
        liquidated=state.liquidated,
        stop_reason=state.stop_reason,
        daily_equity=daily_equity,
    )
