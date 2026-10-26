"""Incremental grid strategy shared by execution adapters."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import numpy as np

from apps.engine.grid import GridParams, SimulationConfig, build_grid_levels, intrabar_path
from apps.trading.execution import ExecutionAdapter


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt_timezone.utc)
    return parsed.astimezone(dt_timezone.utc)


class IncrementalGridStrategy:
    """Stateful long-only grid used by all execution adapters.

    State is JSON-serializable so it survives worker restarts and can be shown
    in the dashboard. Order fills are delegated to the adapter; the strategy
    never talks to an exchange directly.
    """

    def __init__(
        self,
        params: GridParams,
        config: SimulationConfig,
        adapter: ExecutionAdapter,
        state: dict[str, Any] | None = None,
    ) -> None:
        self.params = params
        self.config = config
        self.adapter = adapter
        state = state or {}
        self.levels: np.ndarray = np.array(state.get("levels") or [], dtype=float)
        self.range_low = float(state.get("range_low") or 0.0)
        self.range_high = float(state.get("range_high") or 0.0)
        self.orders: dict[int, dict[str, Any]] = {}
        for key, order in (state.get("orders") or {}).items():
            try:
                self.orders[int(key)] = {
                    "side": str(order["side"]),
                    "qty": float(order["qty"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        self.prev_close = float(state["prev_close"]) if state.get("prev_close") else 0.0
        self.last_bar_time = _parse_dt(state.get("last_bar_time"))
        self.last_rebalance_at = _parse_dt(state.get("last_rebalance_at"))
        self.stopped = bool(state.get("stopped"))
        self.stop_reason = str(state.get("stop_reason") or "")

    @property
    def initialized(self) -> bool:
        return self.levels.size >= 3

    def to_state(self) -> dict[str, Any]:
        return {
            "levels": [float(x) for x in self.levels],
            "range_low": self.range_low,
            "range_high": self.range_high,
            "orders": {
                str(idx): {"side": order["side"], "qty": round(float(order["qty"]), 12)}
                for idx, order in self.orders.items()
            },
            "prev_close": self.prev_close,
            "last_bar_time": self.last_bar_time.isoformat() if self.last_bar_time else None,
            "last_rebalance_at": (
                self.last_rebalance_at.isoformat() if self.last_rebalance_at else None
            ),
            "stopped": self.stopped,
            "stop_reason": self.stop_reason,
        }

    # ------------------------------------------------------------ lifecycle
    def initialize(
        self, hist_rows: list[dict[str, Any]], price: float, now: datetime, *, reset_clock: bool = True
    ) -> bool:
        if price <= 0:
            return False
        if hist_rows:
            highs = np.array([r["high"] for r in hist_rows], dtype=float)
            lows = np.array([r["low"] for r in hist_rows], dtype=float)
            closes = np.array([r["close"] for r in hist_rows], dtype=float)
        else:
            highs = np.array([price * 1.05, price * 1.02, price])
            lows = np.array([price * 0.95, price * 0.98, price])
            closes = np.array([price, price, price])
        self.levels, self.range_low, self.range_high = build_grid_levels(
            highs,
            lows,
            closes,
            price,
            self.params,
            self.config.bars_per_day,
        )
        self.orders.clear()
        self._rebuild_orders(price)
        if reset_clock or self.prev_close <= 0:
            self.prev_close = price
            self.last_bar_time = now
        self.last_rebalance_at = now
        return True

    def maybe_rebalance(
        self, now: datetime, price: float, hist_rows: list[dict[str, Any]]
    ) -> bool:
        if self.stopped:
            return False
        due = self.last_rebalance_at is None or now >= (
            self.last_rebalance_at + timedelta(days=self.params.rebalance_days)
        )
        if not self.initialized or due:
            return self.initialize(hist_rows, price, now, reset_clock=not self.initialized)
        return False

    def _rebuild_orders(self, price: float) -> None:
        self.orders.clear()
        if not self.initialized or self.stopped:
            return
        price_idx = int(np.searchsorted(self.levels, price, side="right") - 1)
        price_idx = max(0, min(len(self.levels) - 1, price_idx))
        eq = self.adapter.equity(price)
        notional = self.adapter.position_qty * price
        used_margin = notional / self.config.leverage
        max_notional = max(0.0, eq * self.config.leverage * self.params.max_utilization)
        buy_budget = max(
            0.0,
            min(max_notional - notional, max(0.0, eq - used_margin) * self.config.leverage * 0.98),
        )
        buy_indices = list(range(0, price_idx))
        sell_indices = list(range(price_idx + 1, len(self.levels)))
        if buy_indices and buy_budget > 0:
            slot_notional = buy_budget / len(buy_indices)
            for idx in buy_indices:
                qty = slot_notional / max(float(self.levels[idx]), 1e-12)
                if qty > 0:
                    self.orders[idx] = {"side": "buy", "qty": qty}
        if sell_indices and self.adapter.position_qty > 0:
            qty_each = self.adapter.position_qty / len(sell_indices)
            for idx in sell_indices:
                self.orders[idx] = {"side": "sell", "qty": qty_each}

    def _queue(self, queue: list[tuple[int, str, float]], idx: int, side: str, qty: float) -> None:
        if 0 <= idx < len(self.levels) and qty > 0:
            queue.append((idx, side, float(qty)))

    def _apply_gap(
        self,
        bar: dict[str, Any],
        queue: list[tuple[int, str, float]],
    ) -> None:
        bar_open = float(bar["open"])
        if self.prev_close <= 0:
            return
        if self.prev_close > bar_open:
            for idx in list(self.orders):
                order = self.orders.get(idx)
                level = float(self.levels[idx])
                if order and order["side"] == "buy" and bar_open <= level <= self.prev_close:
                    fill = self.adapter.buy(
                        bar_open, order["qty"], bar["time"], "gap", idx
                    )
                    if fill:
                        remaining = order["qty"] - fill.qty
                        if remaining <= 1e-12:
                            self.orders.pop(idx, None)
                        else:
                            order["qty"] = remaining
                        self._queue(queue, idx + 1, "sell", fill.qty)
        elif self.prev_close < bar_open:
            for idx in list(self.orders):
                order = self.orders.get(idx)
                level = float(self.levels[idx])
                if order and order["side"] == "sell" and self.prev_close <= level <= bar_open:
                    fill = self.adapter.sell(
                        bar_open, min(order["qty"], self.adapter.position_qty), bar["time"], "gap", idx
                    )
                    if fill:
                        remaining = order["qty"] - fill.qty
                        if remaining <= 1e-12:
                            self.orders.pop(idx, None)
                        else:
                            order["qty"] = remaining
                        self._queue(queue, idx - 1, "buy", fill.qty)

    def _process_path(
        self, bar: dict[str, Any], queue: list[tuple[int, str, float]]
    ) -> None:
        path = intrabar_path(
            float(bar["open"]), float(bar["high"]), float(bar["low"]), float(bar["close"])
        )
        pending = self.orders.copy()
        for p0, p1 in zip(path, path[1:]):
            if p1 < p0:
                for idx in sorted(pending, reverse=True):
                    order = pending.get(idx)
                    if not order or order["side"] != "buy":
                        continue
                    level = float(self.levels[idx])
                    if p1 <= level <= p0:
                        fill = self.adapter.buy(level, order["qty"], bar["time"], "grid", idx)
                        if fill:
                            remaining = order["qty"] - fill.qty
                            if remaining <= 1e-12:
                                self.orders.pop(idx, None)
                            else:
                                self.orders[idx]["qty"] = remaining
                            self._queue(queue, idx + 1, "sell", fill.qty)
                        pending.pop(idx, None)
            elif p1 > p0:
                for idx in sorted(pending):
                    order = pending.get(idx)
                    if not order or order["side"] != "sell":
                        continue
                    level = float(self.levels[idx])
                    if p0 <= level <= p1:
                        fill = self.adapter.sell(
                            level,
                            min(order["qty"], self.adapter.position_qty),
                            bar["time"],
                            "grid",
                            idx,
                        )
                        if fill:
                            remaining = order["qty"] - fill.qty
                            if remaining <= 1e-12:
                                self.orders.pop(idx, None)
                            else:
                                self.orders[idx]["qty"] = remaining
                            self._queue(queue, idx - 1, "buy", fill.qty)
                        pending.pop(idx, None)

    def process_bar(self, bar: dict[str, Any]) -> dict[str, Any]:
        if self.stopped:
            return {"stopped": True, "reason": self.stop_reason}
        if not self.initialized:
            return {"stopped": False, "reason": ""}
        queue: list[tuple[int, str, float]] = []
        self._apply_gap(bar, queue)
        self._process_path(bar, queue)
        self.prev_close = float(bar["close"])
        self.last_bar_time = bar["time"]

        for idx, side, qty in queue:
            existing = self.orders.get(idx)
            if existing is None:
                self.orders[idx] = {"side": side, "qty": qty}
            elif existing["side"] == side:
                existing["qty"] += qty
            # Opposite-side conflict: leave the existing slot untouched.

        if self.adapter.position_qty > 0:
            liq_price = self.adapter.avg_entry * (
                1.0 - 1.0 / self.config.leverage + self.config.maintenance_margin_rate
            )
            eq_low = self.adapter.equity(float(bar["low"]))
            stop_equity = self.adapter.capital * (1.0 - self.params.stop_loss_pct)
            if float(bar["low"]) <= liq_price or eq_low <= stop_equity:
                reason = "liquidation" if float(bar["low"]) <= liq_price else "stop_loss"
                trigger = min(float(bar["low"]), liq_price) if reason == "liquidation" else float(bar["low"])
                self.adapter.close_position(max(trigger, 1e-12), bar["time"], reason)
                self.adapter.mark(float(bar["close"]))
                self.stopped = True
                self.stop_reason = reason
                self.orders.clear()
                return {"stopped": True, "reason": reason}
        self.adapter.mark(float(bar["close"]))
        return {"stopped": False, "reason": ""}

    def close(self, price: float, now: datetime, reason: str = "manual_stop") -> None:
        if self.adapter.position_qty > 0:
            self.adapter.close_position(price, now, reason)
        self.adapter.mark(price)
        self.orders.clear()
        self.stopped = True
        self.stop_reason = reason
