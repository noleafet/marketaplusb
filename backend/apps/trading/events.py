"""Helpers that record grid lifecycle events and session snapshots."""

from datetime import datetime

from django.utils import timezone

from .models import GridEvent, PaperSession, SessionSnapshot

_GRID_KEYS = ("range_low", "range_high", "levels", "spacing")


def session_state(session: PaperSession) -> dict:
    grid_state = session.grid_state or {}
    return {
        "status": session.status,
        "bias": session.bias,
        "capital": session.capital,
        "leverage": session.leverage,
        "position_qty": session.position_qty,
        "avg_entry": session.avg_entry,
        "mark_price": session.mark_price,
        "equity": session.equity,
        "peak_equity": session.peak_equity,
        "max_drawdown": session.max_drawdown,
        "realized_pnl": session.realized_pnl,
        "unrealized_pnl": session.position_qty * (session.mark_price - session.avg_entry),
        "fees_paid": session.fees_paid,
        "funding_paid": session.funding_paid,
        "carry_cost": session.carry_cost,
        "fill_count": session.fill_count,
        "closed_lots": session.closed_lots,
        "wins": session.wins,
        "losses": session.losses,
        "net_return": session.net_return,
        "grid": {k: grid_state.get(k) for k in _GRID_KEYS},
        "stop_reason": session.stop_reason,
    }


def record_event(
    type: str,
    *,
    session: PaperSession | None = None,
    symbol: str = "",
    reason: str = "",
    before: dict | None = None,
    after: dict | None = None,
    strategy_version=None,
    config_version=None,
    time: datetime | None = None,
    environment: str = "paper",
    backfilled: bool = False,
) -> GridEvent:
    if session is not None:
        symbol = symbol or session.coin.binance_symbol
        strategy_version = strategy_version or session.strategy_version
        config_version = config_version or session.config_version
    return GridEvent.objects.create(
        time=time or timezone.now(),
        type=type,
        environment=environment,
        symbol=symbol,
        session=session,
        reason=reason,
        before=before or {},
        after=after or {},
        strategy_version=strategy_version,
        config_version=config_version,
        backfilled=backfilled,
    )


def snapshot_session(
    session: PaperSession,
    trigger: str,
    *,
    time: datetime | None = None,
    backfilled: bool = False,
) -> SessionSnapshot:
    return SessionSnapshot.objects.create(
        session=session,
        time=time or timezone.now(),
        trigger=trigger,
        state=session_state(session),
        strategy_version=session.strategy_version,
        config_version=session.config_version,
        backfilled=backfilled,
    )
