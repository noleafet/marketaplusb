"""Paper-trading orchestration on live Binance public market data."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.binance import INTERVAL_MS, BinanceError, BinanceFuturesClient, floor_to_interval
from apps.engine.grid import GridParams, SimulationConfig
from apps.engine.models import AnalysisRun, DecisionLog, log_decision
from apps.engine.timescale import insert_equity_points
from apps.trading import risk
from apps.trading.execution import PaperExecutionAdapter
from apps.trading.models import PaperEvaluation, PaperFill, PaperSession
from apps.trading.strategy import IncrementalGridStrategy

logger = logging.getLogger("engine")

OPEN_STATUSES = (PaperSession.Status.ACTIVE,)


def paper_config_snapshot() -> dict[str, Any]:
    cfg = settings.ENGINE
    return {
        "capital": float(cfg["PAPER_CAPITAL"]),
        "leverage": float(cfg["LEVERAGE"]),
        "maker_fee_rate": float(cfg["MAKER_FEE_RATE"]),
        "taker_fee_rate": float(cfg["TAKER_FEE_RATE"]),
        "slippage_bps": float(cfg["SLIPPAGE_BPS"]),
        "interval": str(cfg["INTERVAL"]),
    }


def _sim_config(session: PaperSession) -> SimulationConfig:
    cfg = session.config or {}
    return SimulationConfig(
        capital=float(session.capital),
        leverage=float(session.leverage),
        maker_fee_rate=float(cfg.get("maker_fee_rate", settings.ENGINE["MAKER_FEE_RATE"])),
        taker_fee_rate=float(cfg.get("taker_fee_rate", settings.ENGINE["TAKER_FEE_RATE"])),
        slippage_bps=float(cfg.get("slippage_bps", settings.ENGINE["SLIPPAGE_BPS"])),
        interval=str(cfg.get("interval", settings.ENGINE["INTERVAL"])),
    )


def ensure_paper_sessions(
    analysis: AnalysisRun, selected: list[dict[str, Any]]
) -> list[PaperSession]:
    created: list[PaperSession] = []
    now = timezone.now()
    config = paper_config_snapshot()
    for item in selected:
        coin = item["coin"]
        exists = PaperSession.objects.filter(
            coin=coin, status__in=[PaperSession.Status.ACTIVE, PaperSession.Status.PAUSED]
        ).exists()
        if exists:
            continue
        session = PaperSession.objects.create(
            coin=coin,
            analysis=analysis,
            status=PaperSession.Status.ACTIVE,
            params=item.get("params") or {},
            config=config,
            grid_state={},
            capital=config["capital"],
            leverage=config["leverage"],
            equity=config["capital"],
            peak_equity=config["capital"],
            started_at=now,
            last_tick_at=now,
            last_funding_at=now,
        )
        created.append(session)
        log_decision(
            "paper",
            f"Paper session opened for {coin.symbol} from analysis {analysis.id}.",
            analysis=analysis,
            coin=coin,
            payload={
                "session_id": str(session.id),
                "params": session.params,
                "score": item.get("score"),
                "suitability": item.get("suitability"),
            },
        )
    return created


def _fetch_hist(
    client: BinanceFuturesClient, symbol: str, now: datetime, params: GridParams
) -> list[dict[str, Any]]:
    lookback_days = max(45, params.range_lookback_days + 21)
    start = now - timedelta(days=lookback_days)
    end = floor_to_interval(now, "4h") - timedelta(milliseconds=1)
    return client.klines_range(symbol, "4h", start, end, max_requests=10)


def _fetch_price(client: BinanceFuturesClient, symbol: str) -> float:
    payload = client.ticker_price(symbol)
    try:
        return float(payload["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BinanceError(f"Invalid price response for {symbol}") from exc


def _apply_funding_events(
    adapter: PaperExecutionAdapter,
    events: list[dict[str, Any]],
    start_idx: int,
    up_to: datetime,
    fallback_price: float,
) -> tuple[int, float]:
    idx = start_idx
    paid = 0.0
    while idx < len(events) and events[idx]["time"] <= up_to:
        event = events[idx]
        mark = event.get("mark_price") or fallback_price
        paid += adapter.apply_funding(float(event["rate"]), float(mark), event["time"])
        idx += 1
    return idx, paid


def _pause_for_overbought(
    session: PaperSession,
    strategy: IncrementalGridStrategy,
    adapter: PaperExecutionAdapter,
    snapshot: risk.RiskSnapshot,
    decision: risk.RiskDecision,
    now: datetime,
) -> None:
    symbol = session.coin.binance_symbol
    position_qty = adapter.position_qty
    price = snapshot.price or session.mark_price
    if price > 0:
        strategy.flatten(price, now, f"overbought_{decision.reason}")
        adapter.flush()
    session.status = PaperSession.Status.PAUSED
    metrics = (decision.state.get("last_metrics") or {}).copy()
    payload = {
        "event": "overbought_pause",
        "symbol": symbol,
        "coin": session.coin.symbol,
        "session_id": str(session.id),
        "reason": decision.reason,
        "price": round(price, 10),
        "position_qty": round(position_qty, 10),
        "equity": round(session.equity, 4),
        "paused_at": now.isoformat(),
        **{
            key: metrics.get(key)
            for key in (
                "rsi_15m",
                "macd_hist_15m",
                "vwap_15m",
                "peak_high",
                "drawdown_pct",
                "oi_change_pct",
            )
        },
    }
    log_decision(
        "risk",
        f"{session.coin.symbol} paused by overbought guard ({decision.reason}); "
        f"inventory sold to USDT until recovery confirms.",
        severity="warning",
        coin=session.coin,
        payload=payload,
    )


def _apply_overbought_guard(
    session: PaperSession,
    strategy: IncrementalGridStrategy,
    adapter: PaperExecutionAdapter,
    client: BinanceFuturesClient,
    now: datetime,
    price: float,
) -> dict[str, Any] | None:
    symbol = session.coin.binance_symbol
    if not symbol or not settings.RISK.get("ENABLED", True):
        return None
    try:
        snapshot = risk.fetch_snapshot(
            client, symbol, now, settings.RISK, price=price or None
        )
    except BinanceError as exc:
        logger.warning("Risk snapshot failed for %s: %s", symbol, exc)
        return None
    decision = risk.evaluate(session.risk_state or {}, snapshot, settings.RISK, now)
    session.risk_state = decision.state
    if decision.action == "pause":
        _pause_for_overbought(session, strategy, adapter, snapshot, decision, now)
    return {"action": decision.action, "reason": decision.reason}


def _tick_session(
    session: PaperSession,
    client: BinanceFuturesClient,
    now: datetime,
) -> dict[str, Any]:
    params = GridParams.from_dict(session.params or {})
    config = _sim_config(session)
    adapter = PaperExecutionAdapter(session, config=config, params=params)
    strategy = IncrementalGridStrategy(params, config, adapter, session.grid_state or {})
    symbol = session.coin.binance_symbol
    if not symbol:
        return {"ok": False, "error": "no Binance symbol"}

    # A bearish tag keeps the session live for marking/funding but locks the
    # grid: no new orders are armed and no fills are simulated.
    locked = session.bias == PaperSession.Bias.BEARISH
    if locked:
        strategy.orders.clear()

    bars_processed = 0
    funding_paid = 0.0
    funding_until = session.last_funding_at or session.started_at
    funding_fetched = True
    last_price = 0.0

    # First tick: build the grid around the live price and wait for future bars.
    if not strategy.initialized:
        price = _fetch_price(client, symbol)
        last_price = price
        hist = _fetch_hist(client, symbol, now, params)
        strategy.initialize(hist, price, now)
        if locked:
            strategy.orders.clear()
        adapter.mark(price)
        session.last_funding_at = now
        funding_fetched = True
    else:
        # Bring the adapter up to the session's current position.
        adapter.mark(session.mark_price or 0.0)
        start = strategy.last_bar_time or session.last_tick_at or session.started_at
        end = floor_to_interval(now, "1m") - timedelta(milliseconds=1)
        bars: list[dict[str, Any]] = []
        if start < end:
            gap_minutes = (end - start).total_seconds() / 60.0
            bar_interval = "1m" if gap_minutes <= 2 * 24 * 60 else "15m"
            bars = client.klines_range(
                symbol, bar_interval, start + timedelta(seconds=1), end, max_requests=12
            )

        funding_events: list[dict[str, Any]] = []
        if session.position_qty > 0:
            try:
                funding_events = client.funding_rates(symbol, funding_until, now)
            except BinanceError as exc:
                logger.warning("Funding sync failed for %s: %s", symbol, exc)
                funding_fetched = False
        funding_idx = 0

        stopped = False
        stop_reason = ""
        for bar in bars:
            funding_idx, paid = _apply_funding_events(
                adapter, funding_events, funding_idx, bar["time"], float(bar["open"])
            )
            funding_paid += paid
            result = strategy.process_bar(bar)
            bars_processed += 1
            if result.get("stopped"):
                stopped = True
                stop_reason = result.get("reason", "")
                break

        if not stopped and session.position_qty > 0:
            funding_idx, paid = _apply_funding_events(
                adapter, funding_events, funding_idx, now, session.mark_price or session.avg_entry
            )
            funding_paid += paid

        last_price = float(bars[-1]["close"]) if bars else 0.0
        if last_price <= 0:
            try:
                last_price = _fetch_price(client, symbol)
            except BinanceError:
                last_price = session.mark_price
        if last_price > 0:
            adapter.mark(last_price)

        if stopped:
            session.status = PaperSession.Status.STOPPED
            session.stop_reason = stop_reason
            session.promotion_ready = False

        # Re-center after processing bars so new orders only apply to future data.
        if session.status == PaperSession.Status.ACTIVE and not locked:
            due = strategy.last_rebalance_at is None or now >= (
                strategy.last_rebalance_at + timedelta(days=params.rebalance_days)
            )
            if due:
                try:
                    hist = _fetch_hist(client, symbol, now, params)
                    if last_price > 0:
                        strategy.maybe_rebalance(now, last_price, hist)
                        adapter.mark(last_price)
                except BinanceError as exc:
                    logger.warning("Grid rebalance data failed for %s: %s", symbol, exc)

        if funding_fetched and funding_events:
            funding_until = funding_events[-1]["time"]

    # Overbought guard: arm on RSI(15m) > threshold, pause on exhaustion.
    # A bearish session is already locked and holds no ladder to protect.
    risk_result = None
    if session.status == PaperSession.Status.ACTIVE and not locked:
        risk_result = _apply_overbought_guard(
            session, strategy, adapter, client, now, last_price
        )

    # Persist fills, state, and equity.
    adapter.flush()
    fills = adapter.fills
    if fills:
        PaperFill.objects.bulk_create(
            [
                PaperFill(
                    session=session,
                    time=fill.time,
                    side=fill.side,
                    price=fill.price,
                    qty=fill.qty,
                    notional=fill.notional,
                    fee=fill.fee,
                    realized_pnl=fill.realized_pnl,
                    level=fill.level,
                    reason=fill.reason,
                )
                for fill in fills
            ]
        )
        session.fill_count += len(fills)
        session.closed_lots += sum(1 for fill in fills if fill.side == "sell")

    # Hard portfolio stop, independent of the per-bar stop in the strategy.
    if (
        session.status == PaperSession.Status.ACTIVE
        and session.max_drawdown >= settings.PAPER["HARD_STOP_DRAWDOWN"]
    ):
        price = session.mark_price or 0.0
        if price > 0:
            strategy.close(price, now, "risk_stop")
            adapter.flush()
        session.status = PaperSession.Status.STOPPED
        session.stop_reason = (
            f"hard drawdown stop at {session.max_drawdown:.1%}"
        )
        session.promotion_ready = False
        log_decision(
            "paper",
            f"{session.coin.symbol} paper session stopped: {session.stop_reason}.",
            severity="warning",
            coin=session.coin,
            payload={"session_id": str(session.id), "equity": session.equity},
        )

    session.grid_state = strategy.to_state()
    session.last_tick_at = now
    if funding_fetched:
        session.last_funding_at = now
    session.save()

    insert_equity_points(
        [
            {
                "time": now,
                "session_id": str(session.id),
                "equity": session.equity,
                "price": session.mark_price,
                "position_qty": session.position_qty,
                "drawdown": session.max_drawdown,
            }
        ]
    )
    evaluation = evaluate_session(session, now=now)
    return {
        "ok": True,
        "session_id": str(session.id),
        "symbol": session.coin.symbol,
        "bars": bars_processed,
        "fills": len(fills),
        "equity": round(session.equity, 4),
        "status": session.status,
        "promotion_ready": session.promotion_ready,
        "risk": risk_result,
        "evaluation": evaluation,
    }


def evaluate_session(
    session: PaperSession, *, now: datetime | None = None, force: bool = False
) -> dict[str, Any] | None:
    now = now or timezone.now()
    thresholds = {
        "min_days": settings.PAPER["MIN_DAYS"],
        "min_net_return": settings.PAPER["MIN_NET_RETURN"],
        "max_drawdown": settings.PAPER["MAX_DRAWDOWN"],
        "min_trades": settings.PAPER["MIN_TRADES"],
    }
    days = max(0.0, (now - session.started_at).total_seconds() / 86400.0)
    metrics = {
        "days": round(days, 2),
        "net_return": round(session.net_return, 6),
        "max_drawdown": round(session.max_drawdown, 6),
        "fill_count": session.fill_count,
        "closed_lots": session.closed_lots,
        "wins": session.wins,
        "losses": session.losses,
        "fees_paid": round(session.fees_paid, 4),
        "funding_paid": round(session.funding_paid, 4),
        "equity": round(session.equity, 4),
    }
    failures: list[str] = []
    if days < thresholds["min_days"]:
        failures.append(f"paper history {days:.1f}d < {thresholds['min_days']}d")
    if session.net_return < thresholds["min_net_return"]:
        failures.append(
            f"net return {session.net_return:.2%} < {thresholds['min_net_return']:.2%}"
        )
    if session.max_drawdown > thresholds["max_drawdown"]:
        failures.append(
            f"drawdown {session.max_drawdown:.2%} > {thresholds['max_drawdown']:.2%}"
        )
    if session.fill_count < thresholds["min_trades"]:
        failures.append(f"fills {session.fill_count} < {thresholds['min_trades']}")
    if session.status == PaperSession.Status.STOPPED:
        failures.append("session stopped")
    passed = not failures

    interval = timedelta(hours=settings.PAPER["EVAL_INTERVAL_HOURS"])
    should_store = force or session.last_evaluated_at is None or (
        now - session.last_evaluated_at >= interval
    )
    if passed and not session.promotion_ready:
        should_store = True
    if not should_store:
        return None

    PaperEvaluation.objects.create(
        session=session,
        evaluated_at=now,
        passed=passed,
        metrics=metrics,
        failures=failures,
        thresholds=thresholds,
    )
    was_ready = session.promotion_ready
    session.promotion_ready = passed
    session.last_evaluated_at = now
    session.save(update_fields=["promotion_ready", "last_evaluated_at", "updated_at"])
    if passed and not was_ready:
        log_decision(
            "paper",
            f"{session.coin.symbol} paper session passed profitability/risk thresholds; "
            "promotion review is allowed. Live trading remains disabled.",
            coin=session.coin,
            payload={"session_id": str(session.id), "metrics": metrics},
        )
    return {"passed": passed, "failures": failures, "metrics": metrics}


def tick_paper_sessions(
    *, session_ids: list[str] | None = None, job_run_id=None
) -> dict[str, Any]:
    now = timezone.now()
    sessions = PaperSession.objects.filter(status=PaperSession.Status.ACTIVE).select_related("coin")
    if session_ids:
        sessions = sessions.filter(id__in=session_ids)
    session_list = list(sessions)
    client = BinanceFuturesClient()
    results: list[dict[str, Any]] = []
    errors = 0
    total_fills = 0
    for session in session_list:
        try:
            result = _tick_session(session, client, now)
        except Exception as exc:  # noqa: BLE001 - one session must not stop the tick
            logger.exception("Paper tick failed for %s", session.coin.symbol)
            errors += 1
            result = {
                "ok": False,
                "session_id": str(session.id),
                "symbol": session.coin.symbol,
                "error": str(exc),
            }
        results.append(result)
        total_fills += int(result.get("fills") or 0)
    summary = {
        "ok": errors == 0,
        "sessions": len(session_list),
        "errors": errors,
        "fills": total_fills,
        "results": results,
    }
    if errors:
        log_decision(
            "paper",
            f"Paper tick completed with {errors} session error(s).",
            severity="warning",
            job_run_id=job_run_id,
            payload=summary,
        )
    return summary


def run_risk_guard(*, job_run_id=None) -> dict[str, Any]:
    """Evaluate paused sessions and resume them once recovery confirms.

    Active sessions are evaluated inside the paper tick, which runs on the same
    interval; this job only watches the paused queue, because paused sessions
    are not ticked and would otherwise never see a recovery check.
    """

    if not settings.RISK.get("ENABLED", True):
        return {"ok": True, "enabled": False, "sessions": 0, "resumed": 0, "results": []}
    now = timezone.now()
    sessions = [
        session
        for session in PaperSession.objects.filter(
            status=PaperSession.Status.PAUSED
        ).select_related("coin")
        # A manual pause is the operator's call; the guard must not resume it.
        if (session.risk_state or {}).get("phase") == "paused"
    ]
    client = BinanceFuturesClient()
    results: list[dict[str, Any]] = []
    errors = 0
    resumed = 0
    for session in sessions:
        symbol = session.coin.binance_symbol
        if not symbol:
            results.append(
                {
                    "session_id": str(session.id),
                    "symbol": session.coin.symbol,
                    "error": "no Binance symbol",
                }
            )
            continue
        try:
            snapshot = risk.fetch_snapshot(client, symbol, now, settings.RISK)
            decision = risk.evaluate(session.risk_state or {}, snapshot, settings.RISK, now)
            session.risk_state = decision.state
            if decision.action == "resume":
                resume_session(session)
                resumed += 1
                log_decision(
                    "risk",
                    f"{session.coin.symbol} resumed: RSI recovered above "
                    f"{settings.RISK['RSI_RECOVERY']:.0f}, 15m close above VWAP, "
                    "open interest turning up.",
                    coin=session.coin,
                    payload={
                        "session_id": str(session.id),
                        "metrics": decision.state.get("last_metrics"),
                    },
                )
            session.save(update_fields=["risk_state", "updated_at"])
            results.append(
                {
                    "session_id": str(session.id),
                    "symbol": session.coin.symbol,
                    "action": decision.action,
                    "reason": decision.reason,
                }
            )
        except Exception:  # noqa: BLE001 - one session must not stop the guard
            logger.exception("Risk guard failed for %s", session.coin.symbol)
            errors += 1
            results.append(
                {
                    "session_id": str(session.id),
                    "symbol": session.coin.symbol,
                    "error": "risk guard failed",
                }
            )
    summary = {
        "ok": errors == 0,
        "enabled": True,
        "sessions": len(sessions),
        "resumed": resumed,
        "errors": errors,
        "results": results,
    }
    if resumed or errors:
        log_decision(
            "risk",
            f"Risk guard resumed {resumed} session(s) with {errors} error(s).",
            severity="warning" if errors else DecisionLog.Severity.INFO,
            job_run_id=job_run_id,
            payload=summary,
        )
    return summary


def set_bias(session: PaperSession, bias: str) -> PaperSession:
    if bias not in PaperSession.Bias.values:
        raise ValueError(f"Unknown bias: {bias}")
    if session.bias == bias:
        return session
    session.bias = bias
    # Drop the ladder on either transition: a bearish session stays locked, and
    # flipping back to bullish must re-center around the live price.
    session.grid_state = {}
    session.save(update_fields=["bias", "grid_state", "updated_at"])
    log_decision(
        "paper",
        (
            f"{session.coin.symbol} marked bearish; grid trading locked, inventory held."
            if bias == PaperSession.Bias.BEARISH
            else f"{session.coin.symbol} marked bullish; grid trading unlocked."
        ),
        coin=session.coin,
        payload={"session_id": str(session.id), "bias": bias},
    )
    return session


def pause_session(session: PaperSession) -> PaperSession:
    if session.status == PaperSession.Status.ACTIVE:
        session.status = PaperSession.Status.PAUSED
        session.save(update_fields=["status", "updated_at"])
        log_decision(
            "paper",
            f"{session.coin.symbol} paper session paused.",
            coin=session.coin,
            payload={"session_id": str(session.id)},
        )
    return session


def resume_session(session: PaperSession) -> PaperSession:
    if session.status == PaperSession.Status.PAUSED:
        session.status = PaperSession.Status.ACTIVE
        session.last_tick_at = timezone.now()
        # Drop the stale ladder: the next tick rebuilds around the live price
        # instead of replaying bars from before the pause.
        session.grid_state = {}
        session.save(update_fields=["status", "last_tick_at", "grid_state", "updated_at"])
        log_decision(
            "paper",
            f"{session.coin.symbol} paper session resumed.",
            coin=session.coin,
            payload={"session_id": str(session.id)},
        )
    return session


def stop_session(session: PaperSession, reason: str = "manual_stop") -> PaperSession:
    if session.status != PaperSession.Status.STOPPED:
        session.status = PaperSession.Status.STOPPED
        session.stop_reason = reason
        session.promotion_ready = False
        session.save(update_fields=["status", "stop_reason", "promotion_ready", "updated_at"])
        log_decision(
            "paper",
            f"{session.coin.symbol} paper session stopped: {reason}.",
            severity="warning",
            coin=session.coin,
            payload={"session_id": str(session.id)},
        )
    return session
