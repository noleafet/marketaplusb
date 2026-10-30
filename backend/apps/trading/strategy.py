"""Incremental grid strategy shared by execution adapters."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

import numpy as np

from apps.engine.grid import (
    TP_COOLDOWN_DAYS,
    GridParams,
    SimulationConfig,
    build_grid_levels,
    buy_crosses,
    intrabar_path,
    inventory_in_profit,
    market_context,
    max_position_notional,
    range_width_cap,
    sell_crosses,
)
from apps.trading.execution import ExecutionAdapter
from apps.trading.hybrid import TrendSignal


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

        # Adaptive overlay state. Defaults keep sessions created before the
        # overlays existed fully functional.
        self.atr = float(state.get("atr") or 0.0)
        self.support = float(state.get("support") or 0.0)
        self.ctx_range_high = float(state.get("ctx_range_high") or 0.0)
        self.ctx_range_low = float(state.get("ctx_range_low") or 0.0)
        self.ctx_range_pos = state.get("ctx_range_pos")
        self.trend_up = bool(state.get("trend_up"))
        self.momentum = float(state.get("momentum") or 0.0)
        self.funding_z = float(state.get("funding_z") or 0.0)
        self.buy_scale = (
            float(state["buy_scale"]) if state.get("buy_scale") is not None else 1.0
        )
        self.allow_buys = bool(state.get("allow_buys", True))
        self.crowded = False
        self.buy_blocked_until = _parse_dt(state.get("buy_blocked_until"))
        self.tp_active = bool(state.get("tp_active"))
        self.peak_price = float(state.get("peak_price") or 0.0)
        self.epoch_stop = float(state.get("epoch_stop") or 0.0)
        self.trail_up_shifts = int(state.get("trail_up_shifts") or 0)
        self.trailing_tp_exits = int(state.get("trailing_tp_exits") or 0)
        self.funding_trims = int(state.get("funding_trims") or 0)
        self.funding_z_max = float(state.get("funding_z_max") or 0.0)
        self.buy_blocks = int(state.get("buy_blocks") or 0)
        # Audit trail and adaptive rebalancing state.
        self.level_plan: list[dict[str, Any]] = [
            dict(item) for item in (state.get("level_plan") or []) if isinstance(item, dict)
        ]
        self.last_review_at = _parse_dt(state.get("last_review_at"))
        self.baseline_atr = float(state.get("baseline_atr") or 0.0)
        self.baseline_trend_up = bool(state.get("baseline_trend_up"))
        self.baseline_momentum = float(state.get("baseline_momentum") or 0.0)
        self.last_rebalance_reason = str(state.get("last_rebalance_reason") or "")
        self.rebalances = int(state.get("rebalances") or 0)
        self.adaptive_rebalances = int(state.get("adaptive_rebalances") or 0)
        # Hybrid trend filter state. Only consulted while the engine is in
        # hybrid mode; normal mode clears it on every tick.
        self.hybrid_halted = bool(state.get("hybrid_halted"))
        self.trend_blocked = bool(state.get("trend_blocked"))
        self.trend_reason = str(state.get("trend_reason") or "")
        self.ema_100 = float(state.get("ema_100") or 0.0)
        self.adx_14 = float(state["adx_14"]) if state.get("adx_14") is not None else None
        # Book-level risk (exposure cap or daily loss halt), set by the paper
        # runner on every tick. It only ever blocks new buys.
        self.portfolio_blocked = bool(state.get("portfolio_blocked"))

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
            "atr": self.atr,
            "support": self.support,
            "ctx_range_high": self.ctx_range_high,
            "ctx_range_low": self.ctx_range_low,
            "ctx_range_pos": self.ctx_range_pos,
            "trend_up": self.trend_up,
            "momentum": self.momentum,
            "funding_z": self.funding_z,
            "buy_scale": self.buy_scale,
            "allow_buys": self.allow_buys,
            "buy_blocked_until": (
                self.buy_blocked_until.isoformat() if self.buy_blocked_until else None
            ),
            "tp_active": self.tp_active,
            "peak_price": self.peak_price,
            "epoch_stop": self.epoch_stop,
            "trail_up_shifts": self.trail_up_shifts,
            "trailing_tp_exits": self.trailing_tp_exits,
            "funding_trims": self.funding_trims,
            "funding_z_max": self.funding_z_max,
            "buy_blocks": self.buy_blocks,
            "level_plan": self.level_plan,
            "last_review_at": (
                self.last_review_at.isoformat() if self.last_review_at else None
            ),
            "baseline_atr": self.baseline_atr,
            "baseline_trend_up": self.baseline_trend_up,
            "baseline_momentum": self.baseline_momentum,
            "last_rebalance_reason": self.last_rebalance_reason,
            "rebalances": self.rebalances,
            "adaptive_rebalances": self.adaptive_rebalances,
            "hybrid_halted": self.hybrid_halted,
            "trend_blocked": self.trend_blocked,
            "trend_reason": self.trend_reason,
            "ema_100": self.ema_100,
            "adx_14": self.adx_14,
            "portfolio_blocked": self.portfolio_blocked,
        }

    # ------------------------------------------------------- adaptive inputs
    def refresh_context(
        self, hist_rows: list[dict[str, Any]], price: float | None = None
    ) -> None:
        """Update ATR, structural support, 30d range and trend from 4h history."""

        if not hist_rows:
            return
        highs = np.array([row["high"] for row in hist_rows], dtype=float)
        lows = np.array([row["low"] for row in hist_rows], dtype=float)
        closes = np.array([row["close"] for row in hist_rows], dtype=float)
        reference = float(price if price and price > 0 else closes[-1])
        ctx = market_context(
            highs, lows, closes, reference, self.params, self.config.bars_per_day
        )
        self.atr = ctx.atr
        self.support = ctx.support
        self.ctx_range_high = ctx.range_high
        self.ctx_range_low = ctx.range_low
        self.ctx_range_pos = ctx.range_pos
        self.trend_up = ctx.trend_up
        self.momentum = ctx.momentum

    def set_funding_context(self, z: float | None) -> None:
        if z is None:
            return
        self.funding_z = float(z)
        self.funding_z_max = max(self.funding_z_max, self.funding_z)

    def _refresh_gates(self, price: float, now: datetime) -> None:
        """Risk overlays may only reduce exposure; they never raise it."""

        self.crowded = (
            self.params.funding_z_threshold > 0
            and self.funding_z >= self.params.funding_z_threshold
            and (self.momentum < 0 or not self.trend_up)
        )
        self.buy_scale = (
            max(0.0, min(1.0, self.params.funding_cut_scale)) if self.crowded else 1.0
        )
        range_pos = None
        width = self.ctx_range_high - self.ctx_range_low
        if width > 1e-12 and price > 0:
            range_pos = min(
                1.0, max(0.0, (price - self.ctx_range_low) / width)
            )
        self.ctx_range_pos = range_pos
        below_support = (
            self.support > 0
            and price > 0
            and price < self.support - self.params.support_buffer_atr * self.atr
        )
        upper_risk = (
            range_pos is not None
            and range_pos >= self.params.range_pos_high
            and (not self.trend_up or self.momentum < 0)
        )
        cooldown = self.buy_blocked_until is not None and now < self.buy_blocked_until
        self.allow_buys = not (
            cooldown
            or self.crowded
            or below_support
            or upper_risk
            or self.trend_blocked
            or self.portfolio_blocked
        )

    def _reset_trailing(self) -> None:
        self.tp_active = False
        self.peak_price = 0.0

    def apply_trend_signal(
        self, signal: TrendSignal, price: float, now: datetime
    ) -> str:
        """Apply the hybrid gate from closed candles.

        Returns ``halted`` when a confirmed 15m close below the 1h EMA(100)
        forced a flatten, ``resumed`` when the halt cleared, or ``""``.
        """

        was_halted = self.hybrid_halted
        was_blocked = self.trend_blocked
        self.hybrid_halted = signal.halted
        self.trend_blocked = not signal.allow_buys
        self.trend_reason = signal.reason
        self.ema_100 = signal.ema_100
        self.adx_14 = signal.adx
        event = ""
        if signal.halted and not was_halted:
            if self.initialized and price > 0:
                self.flatten(price, now, "hybrid_trend_exit")
            event = "halted"
        elif was_halted and not signal.halted and signal.allow_buys:
            event = "resumed"
        if self.initialized and (event or was_blocked != self.trend_blocked):
            self._refresh_gates(price, now)
            self._rebuild_orders(price)
        return event

    def clear_trend_filter(self) -> None:
        self.hybrid_halted = False
        self.trend_blocked = False
        self.trend_reason = ""

    # ------------------------------------------------------------ lifecycle
    def initialize(
        self,
        hist_rows: list[dict[str, Any]],
        price: float,
        now: datetime,
        *,
        reset_clock: bool = True,
        reason: str = "initialize",
    ) -> bool:
        if price <= 0:
            return False
        if hist_rows:
            highs = np.array([r["high"] for r in hist_rows], dtype=float)
            lows = np.array([r["low"] for r in hist_rows], dtype=float)
            closes = np.array([r["close"] for r in hist_rows], dtype=float)
            self.refresh_context(hist_rows, price)
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
        # The stop sits stop_atr_multiple ATRs below the grid floor, which the
        # grid builder keeps above structural support. It may only ratchet up
        # while inventory is open, so a decline cannot drag it down.
        structure_stop = (
            self.range_low - self.params.stop_atr_multiple * self.atr
            if self.atr > 0 and self.range_low > 0
            else 0.0
        )
        self.epoch_stop = (
            max(self.epoch_stop, structure_stop)
            if self.adapter.position_qty > 0
            else structure_stop
        )
        # Gates use the last completed close, as the backtest does; the live
        # price only stands in on the very first initialisation.
        self._refresh_gates(self.prev_close if self.prev_close > 0 else price, now)
        self.orders.clear()
        self._rebuild_orders(price)
        if reset_clock or self.prev_close <= 0:
            self.prev_close = price
            self.last_bar_time = now
        self.last_rebalance_at = now
        self.last_review_at = now
        self.baseline_atr = self.atr
        self.baseline_trend_up = self.trend_up
        self.baseline_momentum = self.momentum
        self.last_rebalance_reason = reason
        return True

    def rebalance_reason(self, now: datetime, price: float) -> str:
        """Return why the ladder should be rebuilt now, or "" for none."""

        if self.stopped:
            return ""
        if not self.initialized:
            return "initialize"
        if self.last_rebalance_at is None:
            return "scheduled"
        scheduled = now >= (
            self.last_rebalance_at + timedelta(days=self.params.rebalance_days)
        )
        if self.params.rebalance_mode != "adaptive":
            return "scheduled" if scheduled else ""
        cooldown = timedelta(hours=max(0.0, self.params.rebalance_cooldown_hours))
        if now < self.last_rebalance_at + cooldown:
            return ""
        review_interval = timedelta(hours=max(0.0, self.params.range_review_hours))
        review_due = self.last_review_at is None or now >= self.last_review_at + review_interval
        if review_due:
            self.last_review_at = now
            if (
                self.baseline_atr > 0
                and self.atr > 0
                and abs(self.atr - self.baseline_atr) / self.baseline_atr
                >= self.params.volatility_shift_ratio
            ):
                return "volatility_shift"
            if (
                self.range_high > 0
                and self.atr > 0
                and (
                    price > self.range_high + self.params.breakout_atr_multiple * self.atr
                    or price < self.range_low - self.params.breakout_atr_multiple * self.atr
                )
            ):
                return "range_breakout"
            if self.trend_up != self.baseline_trend_up:
                return "regime_shift"
        return "scheduled" if scheduled else ""

    def maybe_rebalance(
        self, now: datetime, price: float, hist_rows: list[dict[str, Any]]
    ) -> bool:
        if self.stopped:
            return False
        reason = self.rebalance_reason(now, price)
        if not reason:
            return False
        was_initialized = self.initialized
        if not self.initialize(
            hist_rows,
            price,
            now,
            reset_clock=not was_initialized,
            reason=reason,
        ):
            return False
        if was_initialized:
            self.rebalances += 1
            if reason in {"volatility_shift", "range_breakout", "regime_shift"}:
                self.adaptive_rebalances += 1
        return True

    def _rebuild_orders(self, price: float) -> None:
        self.orders.clear()
        self.level_plan = []
        if not self.initialized or self.stopped:
            return
        lower_idx = int(np.searchsorted(self.levels, price, side="left"))
        upper_idx = int(np.searchsorted(self.levels, price, side="right"))
        eq = self.adapter.equity(price)
        notional = self.adapter.position_qty * price
        used_margin = notional / self.config.leverage
        max_notional = max_position_notional(eq, self.config, self.params)
        buy_budget = max(
            0.0,
            min(max_notional - notional, max(0.0, eq - used_margin) * self.config.leverage * 0.98),
        ) * self.buy_scale
        buy_indices = list(range(0, lower_idx)) if self.allow_buys else []
        sell_indices = list(range(upper_idx, len(self.levels)))
        buy_slot = buy_budget / len(buy_indices) if buy_indices and buy_budget > 0 else 0.0
        sell_qty = (
            self.adapter.position_qty / len(sell_indices)
            if sell_indices and self.adapter.position_qty > 0
            else 0.0
        )
        for idx, level in enumerate(self.levels):
            level_price = float(level)
            side = "buy" if idx < lower_idx else ("sell" if idx >= upper_idx else "")
            qty = 0.0
            reason = ""
            if side == "buy":
                if not self.allow_buys:
                    reason = "risk_gate"
                elif buy_slot <= 0:
                    reason = "no_capacity"
                else:
                    qty = buy_slot / max(level_price, 1e-12)
                    reason = "active"
            elif side == "sell":
                if sell_qty <= 0:
                    reason = "no_inventory"
                else:
                    qty = sell_qty
                    reason = "active"
            else:
                reason = "at_price"
            if reason == "active" and qty > 0:
                self.orders[idx] = {"side": side, "qty": qty}
            self.level_plan.append(
                {
                    "level": idx,
                    "price": round(level_price, 8),
                    "side": side,
                    "qty": round(qty, 10) if reason == "active" else 0.0,
                    "reason": reason,
                }
            )

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
        trade_through = self.config.trade_through
        pending = self.orders.copy()
        for p0, p1 in zip(path, path[1:]):
            if p1 < p0:
                for idx in sorted(pending, reverse=True):
                    order = pending.get(idx)
                    if not order or order["side"] != "buy":
                        continue
                    level = float(self.levels[idx])
                    if buy_crosses(level, p0, p1, trade_through):
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
                    if sell_crosses(level, p0, p1, trade_through):
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

    def open_initial_inventory(self, price: float, now: datetime) -> None:
        """Optional starting inventory, sized exactly like the backtest."""

        if (
            self.params.initial_inventory_pct <= 0
            or self.adapter.position_qty > 0
            or self.trend_blocked
            or self.portfolio_blocked
            or price <= 0
        ):
            return
        budget = (
            self.adapter.capital
            * self.config.leverage
            * self.params.max_utilization
            * self.params.initial_inventory_pct
        )
        qty = budget / (price * (1.0 + self.config.slip))
        self.adapter.buy(price, qty, now, "initial", None)

    def begin_bar(self, bar: dict[str, Any]) -> bool:
        """Evaluate the risk gates for ``bar`` from completed data.

        Returns whether buys were allowed before this bar, which ``finish_bar``
        needs to re-arm the ladder once the gates clear.
        """

        previous_allowed = self.allow_buys
        self._refresh_gates(self.prev_close or float(bar["open"]), bar["time"])
        if not self.allow_buys:
            for idx in [key for key, order in self.orders.items() if order["side"] == "buy"]:
                self.orders.pop(idx, None)
            self.buy_blocks += 1
            # Keep the audit trail in step with the cancelled buys. Sell rungs
            # are untouched, exactly as in the backtest.
            for item in self.level_plan:
                if item.get("side") == "buy" and item.get("reason") == "active":
                    item["reason"] = "risk_gate"
                    item["qty"] = 0.0
        return previous_allowed

    def step(
        self,
        bar: dict[str, Any],
        hist_before: list[dict[str, Any]],
        *,
        funding_z: float | None = None,
    ) -> dict[str, Any]:
        """Advance one completed candle exactly as ``simulate_grid`` does.

        ``hist_before`` holds every completed candle strictly before ``bar``;
        context, gates, and any rebalance are computed from it, so a bar can
        never influence its own decisions.
        """

        if self.stopped:
            return {"stopped": True, "reason": self.stop_reason}
        bar_open = float(bar["open"])
        if self.prev_close <= 0 and hist_before:
            self.prev_close = float(hist_before[-1]["close"])
        if hist_before:
            self.refresh_context(hist_before, self.prev_close or None)
        self.set_funding_context(funding_z)
        if not self.initialized:
            self.open_initial_inventory(bar_open, bar["time"])
        previous_allowed = self.begin_bar(bar)
        prev_close = self.prev_close
        self.maybe_rebalance(bar["time"], bar_open, hist_before)
        self.prev_close = prev_close
        return self.finish_bar(bar, previous_allowed)

    def process_bar(self, bar: dict[str, Any]) -> dict[str, Any]:
        """Advance one bar on the current ladder (no context refresh or rebalance)."""

        if self.stopped:
            return {"stopped": True, "reason": self.stop_reason}
        if not self.initialized:
            return {"stopped": False, "reason": ""}
        previous_allowed = self.begin_bar(bar)
        return self.finish_bar(bar, previous_allowed)

    def finish_bar(self, bar: dict[str, Any], previous_allowed: bool) -> dict[str, Any]:
        if self.stopped:
            return {"stopped": True, "reason": self.stop_reason}
        if not self.initialized:
            return {"stopped": False, "reason": ""}

        bar_time = bar["time"]
        bar_open = float(bar["open"])
        bar_high = float(bar["high"])
        bar_low = float(bar["low"])
        bar_close = float(bar["close"])

        queue: list[tuple[int, str, float]] = []
        self._apply_gap(bar, queue)
        self._process_path(bar, queue)
        self.prev_close = bar_close
        self.last_bar_time = bar_time
        skip_queue = False

        # 1. Hard stop: ATR distance plus support-backed grid floor, the
        #    capital stop, and the optional portfolio drawdown stop.
        if self.adapter.position_qty > 0:
            liq_price = self.adapter.avg_entry * (
                1.0 - 1.0 / self.config.leverage + self.config.maintenance_margin_rate
            )
            eq_low = self.adapter.equity(bar_low)
            stop_equity = self.adapter.capital * (1.0 - self.params.stop_loss_pct)
            price_stop = self.epoch_stop if self.epoch_stop > 0 else -math.inf
            drawdown_stop = False
            if self.params.max_total_drawdown > 0:
                peak = max(self.adapter.peak_equity, self.adapter.equity(bar_open))
                drawdown_low = 1.0 - eq_low / peak if peak > 0 else 0.0
                drawdown_stop = drawdown_low >= self.params.max_total_drawdown
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
                trigger = min(bar_low, liq_price) if reason == "liquidation" else bar_low
                self.adapter.close_position(max(trigger, 1e-12), bar_time, reason)
                self.adapter.mark(bar_close)
                self.stopped = True
                self.stop_reason = reason
                self.orders.clear()
                self._reset_trailing()
                return {"stopped": True, "reason": reason}

        # 2. Trailing take-profit: armed by inventory profit, exits on an ATR retrace.
        tp_triggered = False
        if (
            self.tp_active
            and self.adapter.position_qty > 0
            and self.peak_price > 0
            and self.atr > 0
        ):
            trail_trigger = self.peak_price - self.params.tp_atr_multiple * self.atr
            if bar_low <= trail_trigger:
                fill_price = max(min(bar_open, trail_trigger), 1e-12)
                self.adapter.sell(
                    fill_price,
                    self.adapter.position_qty,
                    bar_time,
                    "trailing_take_profit",
                    None,
                )
                self.trailing_tp_exits += 1
                self._reset_trailing()
                self.orders.clear()
                self.buy_blocked_until = bar_time + timedelta(days=TP_COOLDOWN_DAYS)
                skip_queue = True
                tp_triggered = True

        # 3. Crowded-long funding: trim exposure when momentum confirms weakness.
        if not tp_triggered and self.crowded and self.adapter.position_qty > 0:
            eq_close = self.adapter.equity(bar_close)
            cap_notional = (
                eq_close
                * self.config.leverage
                * self.params.max_utilization
                * self.buy_scale
            )
            excess = self.adapter.position_qty * bar_close - cap_notional
            if excess > max(1.0, cap_notional * 0.02):
                self.adapter.sell(
                    bar_close,
                    excess / max(bar_close, 1e-12),
                    bar_time,
                    "funding_trim",
                    None,
                )
                self.funding_trims += 1
                self.orders.clear()
                self._rebuild_orders(bar_close)
                skip_queue = True

        # 4. Arm / update the trailing take-profit once inventory is in profit.
        if not tp_triggered:
            if self.adapter.position_qty > 0:
                if not self.tp_active:
                    if inventory_in_profit(bar_close, self.adapter.avg_entry, self.params):
                        self.tp_active = True
                        self.peak_price = bar_high
                else:
                    self.peak_price = max(self.peak_price, bar_high)
            else:
                self._reset_trailing()

        # 5. Trailing up: a confirmed breakout shifts the ladder, width-capped.
        if (
            not tp_triggered
            and self.atr > 0
            and self.range_high > 0
            and self.trend_up
            and bar_close
            > self.ctx_range_high + self.params.trail_up_atr_multiple * self.atr
        ):
            delta = bar_close - self.range_high
            new_low = float(self.levels[0]) + delta
            new_high = float(self.levels[-1]) + delta
            max_width = range_width_cap(self.params, self.atr)
            if max_width > 0 and new_high - new_low > max_width:
                new_low = new_high - max_width
            if new_high > new_low > 0:
                if self.params.spacing_mode == "arithmetic":
                    self.levels = np.linspace(new_low, new_high, len(self.levels))
                else:
                    self.levels = np.geomspace(new_low, new_high, len(self.levels))
                self.range_low, self.range_high = new_low, new_high
                self.epoch_stop = max(
                    self.epoch_stop,
                    new_low - self.params.stop_atr_multiple * self.atr,
                )
                self.trail_up_shifts += 1
                self._rebuild_orders(bar_close)
                skip_queue = True

        # Re-arm buys as soon as the risk gates clear again.
        if not skip_queue and not previous_allowed and self.allow_buys:
            self._rebuild_orders(bar_close)
            skip_queue = True

        if not skip_queue:
            for idx, side, qty in queue:
                existing = self.orders.get(idx)
                if existing is None:
                    self.orders[idx] = {"side": side, "qty": qty}
                elif existing["side"] == side:
                    existing["qty"] += qty
                # Opposite-side conflict: leave the existing slot untouched.

        self.adapter.mark(bar_close)
        return {"stopped": False, "reason": ""}

    def close(self, price: float, now: datetime, reason: str = "manual_stop") -> None:
        if self.adapter.position_qty > 0:
            self.adapter.close_position(price, now, reason)
        self.adapter.mark(price)
        self.orders.clear()
        self._reset_trailing()
        self.buy_blocked_until = None
        self.stopped = True
        self.stop_reason = reason

    def flatten(self, price: float, now: datetime, reason: str = "risk_pause") -> None:
        """Sell inventory and disarm orders without ending the session.

        Used by the overbought guard: the session is paused, so the strategy
        must stay resumable instead of flipping the permanent ``stopped`` flag.
        """
        if self.adapter.position_qty > 0:
            self.adapter.close_position(price, now, reason)
        self.adapter.mark(price)
        self.orders.clear()
        self._reset_trailing()
        self.buy_blocked_until = None
        self.prev_close = price
        self.last_bar_time = now
