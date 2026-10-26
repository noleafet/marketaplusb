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
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

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

    @property
    def bars_per_day(self) -> int:
        return BARS_PER_DAY.get(self.interval, 6)

    @property
    def periods_per_year(self) -> float:
        return self.bars_per_day * TRADING_DAYS

    @property
    def slip(self) -> float:
        return self.slippage_bps / 10_000.0


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
    bars: int = 0
    trades: list[dict[str, Any]] = field(default_factory=list)


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce(
        [high - low, np.abs(high - prev_close), np.abs(low - prev_close)]
    )


def _mean(values: np.ndarray) -> float:
    return float(np.mean(values)) if values.size else 0.0


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
    atr = _mean(tr[-atr_window:])

    raw_low = float(np.percentile(lows, 5))
    raw_high = float(np.percentile(highs, 95))
    pad = max(atr * params.atr_multiplier, current_price * 0.001)
    lower = raw_low - pad
    upper = raw_high + pad

    min_width = max(4.0 * atr, current_price * 0.03)
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


def simulate_grid(
    rows: list[dict[str, Any]],
    funding_rows: list[dict[str, Any]],
    trade_start: datetime,
    params: GridParams,
    config: SimulationConfig,
) -> SimulationResult:
    """Run a single grid path from ``trade_start`` through the end of ``rows``."""

    if len(rows) < 10:
        return SimulationResult({}, [], [], [], False, False, error="insufficient candles")

    times = [row["time"] for row in rows]
    o = np.array([row["open"] for row in rows], dtype=float)
    h = np.array([row["high"] for row in rows], dtype=float)
    low = np.array([row["low"] for row in rows], dtype=float)
    c = np.array([row["close"] for row in rows], dtype=float)

    start_idx = next((i for i, t in enumerate(times) if t >= trade_start), None)
    if start_idx is None or start_idx < 20:
        return SimulationResult({}, [], [], [], False, False, error="not enough warmup data")

    bpd = config.bars_per_day
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
    funding_rows = sorted(funding_rows, key=lambda r: r["time"])
    prev_close = c[start_idx - 1] if start_idx > 0 else o[start_idx]

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
        state.bars_in_market += 1 if state.position_qty > 0 else 0
        state.bars_in_range += 1 if active_low <= price <= active_high else 0
        curve_t.append(timestamp)
        curve_eq.append(equity)
        curve_px.append(price)
        curve_dd.append(dd)
        curve_low.append(active_low)
        curve_high.append(active_high)

    def close_all(price: float, timestamp: datetime, reason: str) -> None:
        nonlocal prev_close
        qty = state.position_qty
        if qty <= 0:
            return
        fill = price * (1.0 - slip)
        fee = fill * qty * config.taker_fee_rate
        pnl = (fill - state.avg_entry) * qty
        state.realized_pnl += pnl
        state.fees_paid += fee
        state.turnover += fill * qty
        state.position_qty = 0.0
        state.avg_entry = 0.0
        state.total_fills += 1
        state.closed_lots += 1
        if pnl - fee >= 0:
            state.wins += 1
            state.gross_profit += max(0.0, pnl - fee)
        else:
            state.losses += 1
            state.gross_loss += abs(min(0.0, pnl - fee))
        state.trades.append(
            {
                "t": timestamp.isoformat(),
                "side": "sell",
                "price": round(fill, 8),
                "qty": round(qty, 8),
                "fee": round(fee, 6),
                "realized_pnl": round(pnl - fee, 6),
                "level": None,
                "reason": reason,
            }
        )

    def position_notional(price: float) -> float:
        return state.position_qty * price

    def max_buy_qty(price: float) -> float:
        eq = equity_at(price, state.position_qty, state.avg_entry)
        used_margin = position_notional(price) / config.leverage
        free_margin = max(0.0, eq - used_margin)
        max_notional = max(0.0, eq * config.leverage * params.max_utilization)
        budget = min(
            max_notional - position_notional(price),
            free_margin * config.leverage * 0.98,
        )
        return max(0.0, budget / price), max(0.0, budget)

    def execute_buy(level_idx: int | None, price: float, timestamp: datetime, reason: str) -> bool:
        order = orders.get(level_idx) if level_idx is not None else None
        qty = order["qty"] if order else 0.0
        if qty <= 0:
            return False
        fill = price * (1.0 + slip)
        max_qty, _ = max_buy_qty(fill)
        qty = min(qty, max_qty)
        if qty <= 1e-12:
            return False
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
        state.turnover += notional
        state.total_fills += 1
        state.max_position_notional = max(state.max_position_notional, notional + position_notional(fill) - notional)
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
        fill = price * (1.0 - slip)
        notional = fill * qty
        fee = notional * config.maker_fee_rate
        pnl = (fill - state.avg_entry) * qty
        state.realized_pnl += pnl
        state.fees_paid += fee
        state.turnover += notional
        state.position_qty -= qty
        if state.position_qty <= 1e-12:
            state.position_qty = 0.0
            state.avg_entry = 0.0
        state.total_fills += 1
        state.closed_lots += 1
        if pnl - fee >= 0:
            state.wins += 1
            state.gross_profit += max(0.0, pnl - fee)
        else:
            state.losses += 1
            state.gross_loss += abs(min(0.0, pnl - fee))
        state.trades.append(
            {
                "t": timestamp.isoformat(),
                "side": "sell",
                "price": round(fill, 8),
                "qty": round(qty, 8),
                "fee": round(fee, 6),
                "realized_pnl": round(pnl - fee, 6),
                "level": level_idx,
                "reason": reason,
            }
        )
        if order["qty"] - qty <= 1e-12:
            orders.pop(level_idx, None)
        else:
            order["qty"] -= qty
        return True

    def rebuild_orders(timestamp: datetime, price: float) -> None:
        nonlocal prev_close
        orders.clear()
        if state.stopped:
            return
        price_idx = int(np.searchsorted(levels, price, side="right") - 1)
        price_idx = max(0, min(len(levels) - 1, price_idx))
        eq = equity_at(price, state.position_qty, state.avg_entry)
        notional = state.position_qty * price
        used_margin = notional / config.leverage
        max_notional = max(0.0, eq * config.leverage * params.max_utilization)
        buy_budget = max(
            0.0,
            min(max_notional - notional, max(0.0, eq - used_margin) * config.leverage * 0.98),
        )
        buy_indices = list(range(0, price_idx))
        sell_indices = list(range(price_idx + 1, len(levels)))
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

    # Initial inventory (disabled by default in the ranking engine).
    first = start_idx
    if params.initial_inventory_pct > 0:
        price = o[first] * (1.0 + slip)
        eq = config.capital
        budget = eq * config.leverage * params.max_utilization * params.initial_inventory_pct
        qty = budget / price
        if qty > 0:
            fee = budget * config.taker_fee_rate
            state.position_qty = qty
            state.avg_entry = price
            state.fees_paid += fee
            state.turnover += budget
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

    for i in range(start_idx, len(rows)):
        if state.stopped:
            break
        bar_open = float(o[i])
        bar_high = float(h[i])
        bar_low = float(low[i])
        bar_close = float(c[i])
        timestamp = times[i]

        # Funding events that occurred since the previous bar.
        while funding_idx < len(funding_rows) and funding_rows[funding_idx]["time"] <= timestamp:
            event = funding_rows[funding_idx]
            funding_idx += 1
            if state.position_qty > 0 and event["time"] >= times[start_idx]:
                mark = event.get("mark_price") or bar_open
                cost = state.position_qty * float(mark) * float(event["rate"])
                state.funding_paid += cost
                state.funding_events += 1

        if (i - start_idx) % rebalance_bars == 0:
            levels, active_low, active_high = _build_grid(
                h[:i], low[:i], c[:i], bar_open, params, bpd
            )
            rebuild_orders(timestamp, bar_open)

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
        first_extreme = bar_low if (bar_open - bar_low) <= (bar_high - bar_open) else bar_high
        second_extreme = bar_high if first_extreme == bar_low else bar_low
        path = [bar_open]
        for point in (first_extreme, second_extreme, bar_close):
            if not math.isclose(point, path[-1], rel_tol=0, abs_tol=1e-12):
                path.append(point)

        # Captured so orders created by fills wait for the next bar.
        pending_orders = orders.copy()

        for p0, p1 in zip(path, path[1:]):
            if p1 < p0:
                for idx in sorted(pending_orders, reverse=True):
                    order = pending_orders.get(idx)
                    if not order or order["side"] != "buy":
                        continue
                    if p1 <= levels[idx] <= p0:
                        before = state.position_qty
                        if execute_buy(idx, levels[idx], timestamp, "grid"):
                            new_qty = state.position_qty - before
                            if new_qty > 0 and idx + 1 < len(levels):
                                queue_order(idx + 1, "sell", new_qty)
                        pending_orders.pop(idx, None)
            elif p1 > p0:
                for idx in sorted(pending_orders):
                    order = pending_orders.get(idx)
                    if not order or order["side"] != "sell":
                        continue
                    if p0 <= levels[idx] <= p1:
                        before = state.position_qty
                        if execute_sell(idx, levels[idx], timestamp, "grid"):
                            sold = before - state.position_qty
                            if sold > 0 and idx - 1 >= 0:
                                queue_order(idx - 1, "buy", sold)
                        pending_orders.pop(idx, None)

        prev_close = bar_close

        # Stop-out and liquidation checks use the bar's worst print.
        if state.position_qty > 0:
            eq_low = equity_at(bar_low, state.position_qty, state.avg_entry)
            liq_price = state.avg_entry * (
                1.0 - 1.0 / config.leverage + config.maintenance_margin_rate
            )
            if bar_low <= liq_price or eq_low <= stop_equity:
                reason = "liquidation" if bar_low <= liq_price else "stop_loss"
                trigger_price = min(bar_low, liq_price) if reason == "liquidation" else bar_low
                close_all(max(trigger_price, 1e-12), timestamp, reason)
                state.stopped = True
                state.liquidated = reason == "liquidation"
                state.stop_reason = reason
                record_equity(bar_close, timestamp)
                break

        record_equity(bar_close, timestamp)
        state.bars += 1

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
        "turnover": round(state.turnover, 2),
        "exposure": round(exposure, 6),
        "avg_utilization": round(utilization, 6),
        "time_in_range": round(time_in_range, 6),
        "max_position_notional": round(state.max_position_notional, 4),
        "stopped": state.stopped,
        "liquidated": state.liquidated,
        "stop_reason": state.stop_reason,
        "bars": state.bars,
        "grid_resets": max(1, math.ceil(state.bars / max(1, rebalance_bars))),
    }

    curve = _sampled_curve(
        curve_t, curve_eq, curve_px, curve_dd, curve_low, curve_high
    )
    return SimulationResult(
        metrics=metrics,
        equity_curve=curve,
        trades=state.trades[-200:],
        grid_levels=[float(x) for x in levels],
        stopped=state.stopped,
        liquidated=state.liquidated,
        stop_reason=state.stop_reason,
    )
