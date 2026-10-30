"""Book-level risk for paper trading.

Each paper session sizes itself, but nothing used to bound the book as a
whole. This module adds the portfolio layer configured on the admin
strategies page:

* fixed capital per session (book capital / max sessions),
* a cap on total open notional relative to book capital,
* a daily loss limit measured from each session's equity at the UTC day start,
  which halts new buys for the rest of the day.

Limits only ever block new buys. Stops and exits keep working, so a halted
book can still de-risk.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from apps.engine.models import log_decision
from apps.trading.models import PaperSession, StrategySettings

OPEN = (PaperSession.Status.ACTIVE, PaperSession.Status.PAUSED)


def open_sessions():
    return PaperSession.objects.filter(status__in=OPEN).select_related("coin")


def open_symbols() -> list[str]:
    return [session.coin.symbol for session in open_sessions()]


def refresh_book_state(settings_row: StrategySettings, now: datetime) -> dict[str, Any]:
    """Recompute exposure and today's P&L, and latch the daily loss halt."""

    sessions = list(open_sessions())
    day = now.date().isoformat()
    state = dict(settings_row.book_state or {})
    if state.get("day") != day:
        state = {"day": day, "start": {}, "halted": False, "halted_at": None}
    start: dict[str, float] = dict(state.get("start") or {})
    for session in sessions:
        # A session opened today starts from its capital; one carried over
        # starts from its equity at the first tick of the day.
        start.setdefault(str(session.id), float(session.equity))
    state["start"] = start

    tracked = PaperSession.objects.filter(id__in=list(start)).only("id", "equity")
    pnl_today = sum(float(row.equity) - float(start[str(row.id)]) for row in tracked)
    book_capital = max(1e-9, float(settings_row.book_capital))
    notional = sum(
        float(session.position_qty) * float(session.mark_price or 0.0)
        for session in sessions
        if session.status == PaperSession.Status.ACTIVE
    )
    equity = sum(float(session.equity) for session in sessions)
    loss_pct = max(0.0, -pnl_today) / book_capital
    limit = float(settings_row.daily_loss_limit_pct)
    if not state.get("halted") and limit > 0 and loss_pct >= limit:
        state["halted"] = True
        state["halted_at"] = now.isoformat()
        log_decision(
            "risk",
            f"Paper book halted new buys: daily loss {loss_pct:.2%} reached the "
            f"{limit:.2%} limit. Stops and exits stay active; buys resume tomorrow (UTC).",
            severity="warning",
            payload={"pnl_today": round(pnl_today, 4), "loss_pct": round(loss_pct, 6)},
        )
    state.update(
        {
            "updated_at": now.isoformat(),
            "sessions": len(sessions),
            "equity": round(equity, 4),
            "notional": round(notional, 4),
            "exposure": round(notional / book_capital, 6),
            "pnl_today": round(pnl_today, 4),
            "loss_today_pct": round(loss_pct, 6),
        }
    )
    settings_row.book_state = state
    settings_row.save(update_fields=["book_state", "updated_at"])
    return state


def buy_block_reason(
    state: dict[str, Any], settings_row: StrategySettings, regime_phase: str = "normal"
) -> str:
    if state.get("halted"):
        return "daily_loss_halt"
    cap = float(settings_row.max_book_exposure)
    if cap > 0 and float(state.get("exposure") or 0.0) >= cap:
        return "book_exposure_cap"
    # The BTC market regime blocks new buys on every coin while not normal.
    if regime_phase in ("warning", "bearish"):
        return f"btc_regime_{regime_phase}"
    return ""
