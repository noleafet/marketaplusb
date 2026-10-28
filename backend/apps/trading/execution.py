"""Execution adapters.

The same incremental grid strategy is used by paper trading and any future live
runner. Only the adapter changes. Live order placement is double-gated and is
never called by scheduled jobs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from django.conf import settings
from django.utils import timezone

from apps.common.binance import BinanceFuturesClient
from apps.engine.grid import GridParams, SimulationConfig, max_position_notional
from apps.trading.models import LiveOrderAudit, LiveTradingState

logger = logging.getLogger("engine")


@dataclass
class Fill:
    time: datetime
    side: str
    price: float
    qty: float
    notional: float
    fee: float
    realized_pnl: float
    level: int | None
    reason: str


class ExecutionAdapter(Protocol):
    position_qty: float
    avg_entry: float
    capital: float

    def equity(self, price: float) -> float: ...
    def max_buy_qty(self, price: float) -> float: ...
    def buy(self, requested_price: float, qty: float, time: datetime, reason: str, level: int | None) -> Fill | None: ...
    def sell(self, requested_price: float, qty: float, time: datetime, reason: str, level: int | None) -> Fill | None: ...
    def close_position(self, price: float, time: datetime, reason: str) -> Fill | None: ...
    def apply_funding(self, rate: float, mark_price: float, time: datetime) -> float: ...
    def mark(self, price: float) -> None: ...


class PaperExecutionAdapter:
    """Simulated fills with maker/taker fees, slippage, and margin constraints."""

    def __init__(
        self,
        session,
        *,
        config: SimulationConfig,
        params: GridParams,
    ) -> None:
        self.session = session
        self.config = config
        self.params = params
        self.capital = float(session.capital)
        self.position_qty = float(session.position_qty)
        self.avg_entry = float(session.avg_entry)
        self.realized_pnl = float(session.realized_pnl)
        self.fees_paid = float(session.fees_paid)
        self.funding_paid = float(session.funding_paid)
        self.mark_price = float(session.mark_price or 0.0)
        self.fills: list[Fill] = []

    def equity(self, price: float) -> float:
        unreal = (price - self.avg_entry) * self.position_qty if self.position_qty > 0 else 0.0
        return self.capital + self.realized_pnl - self.fees_paid - self.funding_paid + unreal

    def max_buy_qty(self, price: float) -> float:
        eq = self.equity(price)
        notional = self.position_qty * price
        used_margin = notional / self.config.leverage
        free_margin = max(0.0, eq - used_margin)
        max_notional = max_position_notional(eq, self.config, self.params)
        budget = min(
            max_notional - notional,
            free_margin * self.config.leverage * 0.98,
        )
        return max(0.0, budget / price) if price > 0 else 0.0

    def buy(
        self,
        requested_price: float,
        qty: float,
        time: datetime,
        reason: str,
        level: int | None,
    ) -> Fill | None:
        qty = min(float(qty), self.max_buy_qty(requested_price))
        if qty <= 1e-12:
            return None
        fill_price = requested_price * (1.0 + self.config.slip)
        notional = fill_price * qty
        fee_rate = (
            self.config.maker_fee_rate if reason in {"grid", "gap"} else self.config.taker_fee_rate
        )
        fee = notional * fee_rate
        new_qty = self.position_qty + qty
        self.avg_entry = (
            (self.avg_entry * self.position_qty + fill_price * qty) / new_qty
            if new_qty > 0
            else 0.0
        )
        self.position_qty = new_qty
        self.fees_paid += fee
        fill = Fill(
            time=time,
            side="buy",
            price=fill_price,
            qty=qty,
            notional=notional,
            fee=fee,
            realized_pnl=0.0,
            level=level,
            reason=reason,
        )
        self.fills.append(fill)
        self._sync()
        return fill

    def sell(
        self,
        requested_price: float,
        qty: float,
        time: datetime,
        reason: str,
        level: int | None,
    ) -> Fill | None:
        qty = min(float(qty), self.position_qty)
        if qty <= 1e-12:
            return None
        fill_price = requested_price * (1.0 - self.config.slip)
        notional = fill_price * qty
        fee_rate = (
            self.config.maker_fee_rate if reason in {"grid", "gap"} else self.config.taker_fee_rate
        )
        fee = notional * fee_rate
        pnl = (fill_price - self.avg_entry) * qty
        self.realized_pnl += pnl
        self.fees_paid += fee
        self.position_qty -= qty
        if self.position_qty <= 1e-12:
            self.position_qty = 0.0
            self.avg_entry = 0.0
        fill = Fill(
            time=time,
            side="sell",
            price=fill_price,
            qty=qty,
            notional=notional,
            fee=fee,
            realized_pnl=pnl - fee,
            level=level,
            reason=reason,
        )
        self.fills.append(fill)
        if pnl - fee >= 0:
            self.session.wins += 1
        else:
            self.session.losses += 1
        self._sync()
        return fill

    def close_position(self, price: float, time: datetime, reason: str) -> Fill | None:
        if self.position_qty <= 0:
            return None
        return self.sell(price, self.position_qty, time, reason, None)

    def apply_funding(self, rate: float, mark_price: float, time: datetime) -> float:
        if self.position_qty <= 0 or mark_price <= 0:
            return 0.0
        cost = self.position_qty * mark_price * rate
        self.funding_paid += cost
        self._sync()
        return cost

    def mark(self, price: float) -> None:
        if price <= 0:
            return
        self.mark_price = price
        equity = self.equity(price)
        self.session.mark_price = price
        self.session.equity = equity
        self.session.peak_equity = max(self.session.peak_equity, equity)
        if self.session.peak_equity > 0:
            self.session.max_drawdown = max(
                self.session.max_drawdown, 1.0 - equity / self.session.peak_equity
            )
        self.session.fees_paid = self.fees_paid
        self.session.funding_paid = self.funding_paid
        self.session.realized_pnl = self.realized_pnl

    def _sync(self) -> None:
        self.session.position_qty = self.position_qty
        self.session.avg_entry = self.avg_entry
        self.session.realized_pnl = self.realized_pnl
        self.session.fees_paid = self.fees_paid
        self.session.funding_paid = self.funding_paid

    def flush(self) -> None:
        self.session.position_qty = self.position_qty
        self.session.avg_entry = self.avg_entry
        self.session.realized_pnl = self.realized_pnl
        self.session.fees_paid = self.fees_paid
        self.session.funding_paid = self.funding_paid
        if self.mark_price > 0:
            self.mark(self.mark_price)


class LiveGateError(RuntimeError):
    pass


def live_status() -> dict[str, Any]:
    state = LiveTradingState.load()
    approval = state.active_approval
    enabled = bool(settings.LIVE["ENABLED"])
    keys_present = bool(settings.LIVE["API_KEY"] and settings.LIVE["API_SECRET"])
    approval_ok = bool(approval and approval.status == "approved")
    return {
        "env_enabled": enabled,
        "keys_present": keys_present,
        "mode": state.mode,
        "hybrid": {
            "ema_period": 100,
            "adx_period": 14,
            "adx_entry": 25.0,
            "adx_exit": 20.0,
            "confirm_bars": 2,
            "adx_enabled": bool((state.limits or {}).get("hybrid_adx")),
        },
        "kill_switch": state.kill_switch,
        "kill_reason": state.kill_reason,
        "approval": (
            {
                "id": approval.id,
                "status": approval.status,
                "reviewed_by": approval.reviewed_by,
                "reviewed_at": approval.reviewed_at,
            }
            if approval
            else None
        ),
        "live_ready": enabled and keys_present and approval_ok and not state.kill_switch,
        "testnet": bool(settings.LIVE["TESTNET"]),
        "limits": {
            "max_notional_usd": settings.LIVE["MAX_NOTIONAL_USD"],
            "max_daily_loss_usd": settings.LIVE["MAX_DAILY_LOSS_USD"],
            "max_leverage": settings.LIVE["MAX_LEVERAGE"],
            "symbol_allowlist": settings.LIVE["SYMBOL_ALLOWLIST"],
        },
    }


def assert_live_allowed(symbol: str, notional: float, leverage: float) -> LiveTradingState:
    state = LiveTradingState.load()
    status = live_status()
    reason = ""
    if not status["env_enabled"]:
        reason = "LIVE_TRADING_ENABLED is false"
    elif not status["keys_present"]:
        reason = "Binance API keys are not configured"
    elif state.kill_switch:
        reason = f"Kill switch engaged: {state.kill_reason}"
    elif not status["approval"] or status["approval"]["status"] != "approved":
        reason = "No approved live-trading approval record"
    elif state.mode == LiveTradingState.Mode.HYBRID:
        reason = (
            "Hybrid mode: new live orders are disabled until open positions are filled"
        )
    elif settings.LIVE["SYMBOL_ALLOWLIST"] and symbol not in settings.LIVE["SYMBOL_ALLOWLIST"]:
        reason = f"{symbol} is not in LIVE_SYMBOL_ALLOWLIST"
    elif notional > settings.LIVE["MAX_NOTIONAL_USD"]:
        reason = f"Order notional exceeds ${settings.LIVE['MAX_NOTIONAL_USD']:,.0f}"
    elif leverage > settings.LIVE["MAX_LEVERAGE"]:
        reason = f"Leverage exceeds {settings.LIVE['MAX_LEVERAGE']}x"
    if reason:
        LiveOrderAudit.objects.create(
            symbol=symbol,
            side="",
            order_type="guard",
            notional=notional,
            allowed=False,
            reason=reason,
        )
        raise LiveGateError(reason)
    return state


class LiveExecutionAdapter:
    """Guarded Binance USDⓈ-M order primitive.

    This adapter is not wired to any scheduled job or automatic runner. It
    exists so that a future explicitly-approved live runner can share the grid
    strategy. Every order requires environment enablement, configured API keys,
    an approved approval record, a released kill switch, symbol allowlisting,
    and notional/leverage limits.
    """

    def __init__(self) -> None:
        self.client = BinanceFuturesClient(
            base_url="https://testnet.binancefuture.com"
            if settings.LIVE["TESTNET"]
            else "https://fapi.binance.com"
        )

    def place_limit_order(
        self, symbol: str, side: str, qty: float, price: float, *, leverage: float
    ) -> dict[str, Any]:
        notional = qty * price
        assert_live_allowed(symbol, notional, leverage)
        payload = self.client.signed(
            "POST",
            "/fapi/v1/order",
            {
                "symbol": symbol,
                "side": side.upper(),
                "type": "LIMIT",
                "timeInForce": "GTC",
                "quantity": qty,
                "price": price,
            },
        )
        LiveOrderAudit.objects.create(
            symbol=symbol,
            side=side.upper(),
            order_type="LIMIT",
            qty=qty,
            price=price,
            notional=notional,
            allowed=True,
            reason="gates passed",
            response=payload if isinstance(payload, dict) else {},
        )
        return payload

    def cancel_order(self, symbol: str, order_id: int) -> dict[str, Any]:
        state = LiveTradingState.load()
        if state.kill_switch:
            raise LiveGateError(f"Kill switch engaged: {state.kill_reason}")
        return self.client.signed(
            "DELETE", "/fapi/v1/order", {"symbol": symbol, "orderId": order_id}
        )

    def account(self) -> dict[str, Any]:
        return self.client.signed("GET", "/fapi/v2/account", {})


def set_kill_switch(engaged: bool, reason: str = "") -> LiveTradingState:
    state = LiveTradingState.load()
    state.kill_switch = engaged
    state.kill_reason = reason or ("Manual kill switch" if engaged else "Released manually")
    state.save(update_fields=["kill_switch", "kill_reason", "updated_at"])
    if engaged:
        logger.warning("LIVE KILL SWITCH ENGAGED: %s", state.kill_reason)
    return state
