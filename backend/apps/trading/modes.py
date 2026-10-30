"""Normal/Hybrid trading mode switching.

Switching to hybrid is destructive for paper state by design: open grid orders
and inventory are deleted so profits already realised stay untouched while the
new trend rules take over a clean book. Realised P&L, fees, funding, and trade
counters are never modified. Live orders are never cancelled; the live gate
simply refuses new orders until open positions are filled.
"""

from __future__ import annotations

import logging
from typing import Any

from django.db import transaction
from django.utils import timezone

from apps.engine.models import AnalysisRun, DecisionLog, log_decision
from apps.trading.models import LiveTradingState, PaperSession

logger = logging.getLogger("engine")


class ModeConfirmationRequired(RuntimeError):
    pass


def _latest_universe_run(mode: str) -> AnalysisRun | None:
    return (
        AnalysisRun.objects.filter(
            status=AnalysisRun.Status.SUCCEEDED,
            scope=AnalysisRun.Scope.UNIVERSE,
            mode=mode,
        )
        .order_by("-started_at")
        .first()
    )


def mode_analysis_status(mode: str) -> dict[str, Any]:
    """Latest universe ranking for ``mode`` and whether it is stale.

    It is stale when there is none, when it is a hybrid run from before the
    full-window gate (its summary has no ``hybrid_coverage``), or when the
    other mode has since driven paper sessions. A comparison run made after
    the primary in the same job is a full ranking and never makes the
    primary stale.
    """

    own = _latest_universe_run(mode)
    stale = own is None
    if own is not None:
        legacy_hybrid = own.mode == AnalysisRun.Mode.HYBRID and "hybrid_coverage" not in (
            own.summary or {}
        )
        overtaken = (
            AnalysisRun.objects.filter(
                status=AnalysisRun.Status.SUCCEEDED,
                scope=AnalysisRun.Scope.UNIVERSE,
                drives_sessions=True,
                started_at__gt=own.started_at,
            )
            .exclude(mode=own.mode)
            .exists()
        )
        stale = legacy_hybrid or overtaken
    return {
        "latest_analysis": (
            {"id": str(own.id), "started_at": own.started_at, "mode": own.mode} if own else None
        ),
        "analysis_stale": stale,
    }


def mode_payload(state: LiveTradingState) -> dict[str, Any]:
    return {
        "mode": state.mode,
        **mode_analysis_status(state.mode),
        "adx_enabled": bool((state.limits or {}).get("hybrid_adx")),
        "updated_at": state.updated_at,
    }


def purge_open_paper_state() -> dict[str, int]:
    """Delete open grid orders and positions without touching realised P&L."""

    now = timezone.now()
    sessions = list(
        PaperSession.objects.exclude(status=PaperSession.Status.STOPPED).select_related("coin")
    )
    positions = 0
    for session in sessions:
        if session.position_qty > 0:
            positions += 1
        # Equity drops the unrealised component; realised profit, fees, and
        # funding stay as booked so history is unaffected by the switch.
        equity = (
            session.capital + session.realized_pnl - session.fees_paid - session.funding_paid
        )
        session.position_qty = 0.0
        session.avg_entry = 0.0
        session.equity = equity
        session.grid_state = {}
        session.save(
            update_fields=[
                "position_qty",
                "avg_entry",
                "equity",
                "grid_state",
                "updated_at",
            ]
        )
        log_decision(
            "paper",
            f"{session.coin.symbol} open orders and position cleared for hybrid mode.",
            severity=DecisionLog.Severity.WARNING,
            coin=session.coin,
            payload={
                "session_id": str(session.id),
                "mode": LiveTradingState.Mode.HYBRID,
                "equity": round(equity, 4),
                "cleared_at": now.isoformat(),
            },
        )
    if sessions:
        logger.info("Hybrid mode cleared %d paper session(s)", len(sessions))
    return {"sessions": len(sessions), "positions": positions}


def set_trading_mode(
    *,
    mode: str,
    adx_enabled: bool | None = None,
    confirm: bool = False,
) -> tuple[LiveTradingState, dict[str, int]]:
    if mode not in LiveTradingState.Mode.values:
        raise ValueError(f"Unknown trading mode: {mode}")
    state = LiveTradingState.load()
    if mode == state.mode and adx_enabled is None:
        # Nothing to change (e.g. the rerun button re-posting the active mode).
        return state, {"sessions": 0, "positions": 0}
    limits = dict(state.limits or {})
    if adx_enabled is not None:
        limits["hybrid_adx"] = bool(adx_enabled)
    switching_to_hybrid = mode == LiveTradingState.Mode.HYBRID and state.mode != mode
    if switching_to_hybrid and not confirm:
        raise ModeConfirmationRequired(
            "Switching to hybrid deletes open paper orders and positions. "
            "Resend with confirm=true to proceed."
        )
    purge = {"sessions": 0, "positions": 0}
    with transaction.atomic():
        if switching_to_hybrid:
            purge = purge_open_paper_state()
        state.mode = mode
        state.limits = limits
        state.save(update_fields=["mode", "limits", "updated_at"])
        log_decision(
            "live",
            f"Trading mode set to {mode}.",
            severity=(
                DecisionLog.Severity.WARNING
                if switching_to_hybrid
                else DecisionLog.Severity.INFO
            ),
            payload={
                "mode": mode,
                "adx_enabled": bool(limits.get("hybrid_adx")),
                "purged_sessions": purge["sessions"],
                "purged_positions": purge["positions"],
            },
        )
    return state, purge
