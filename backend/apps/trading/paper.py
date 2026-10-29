"""Paper-trading orchestration on live Binance public market data."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import Any

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.common.binance import INTERVAL_MS, BinanceError, BinanceFuturesClient, floor_to_interval
from apps.engine.grid import GridParams, SimulationConfig, funding_zscore
from apps.engine.models import AnalysisRun, DecisionLog, log_decision
from apps.engine.timescale import insert_equity_points
from apps.engine.versioning import resolve_config_version, resolve_strategy_version
from apps.trading import portfolio, risk
from apps.trading.execution import PaperExecutionAdapter
from apps.trading.events import record_event, snapshot_session
from apps.trading.hybrid import TrendSignal
from apps.trading.hybrid import evaluate as evaluate_trend
from apps.trading.models import (
    GridEvent,
    LiveTradingState,
    PaperEvaluation,
    PaperFill,
    PaperSession,
    StrategySettings,
)
from apps.trading.strategy import IncrementalGridStrategy

logger = logging.getLogger("engine")

OPEN_STATUSES = (PaperSession.Status.ACTIVE,)


def paper_config_snapshot(settings_row: StrategySettings | None = None) -> dict[str, Any]:
    cfg = settings.ENGINE
    settings_row = settings_row or StrategySettings.load()
    return {
        "capital": float(settings_row.session_capital),
        "leverage": float(cfg["LEVERAGE"]),
        "maker_fee_rate": float(cfg["MAKER_FEE_RATE"]),
        "taker_fee_rate": float(cfg["TAKER_FEE_RATE"]),
        "slippage_bps": float(cfg["SLIPPAGE_BPS"]),
        "interval": settings_row.signal_interval,
        "fill_model": settings_row.fill_model,
        "trade_through_bps": settings_row.effective_trade_through_bps,
    }


def _sim_config(
    session: PaperSession, settings_row: StrategySettings | None = None
) -> SimulationConfig:
    """Session costs plus the live interval and fill model from the settings.

    The interval and fill model follow the admin switch rather than the value
    captured at session creation, so paper always runs what the daily backtest
    measures.
    """

    cfg = session.config or {}
    settings_row = settings_row or StrategySettings.load()
    return SimulationConfig(
        capital=float(session.capital),
        leverage=float(session.leverage),
        maker_fee_rate=float(cfg.get("maker_fee_rate", settings.ENGINE["MAKER_FEE_RATE"])),
        taker_fee_rate=float(cfg.get("taker_fee_rate", settings.ENGINE["TAKER_FEE_RATE"])),
        slippage_bps=float(cfg.get("slippage_bps", settings.ENGINE["SLIPPAGE_BPS"])),
        interval=str(settings_row.signal_interval or cfg.get("interval") or settings.ENGINE["INTERVAL"]),
        trade_through_bps=settings_row.effective_trade_through_bps,
    )


def ensure_paper_sessions(
    analysis: AnalysisRun, selected: list[dict[str, Any]]
) -> list[PaperSession]:
    created: list[PaperSession] = []
    now = timezone.now()
    settings_row = StrategySettings.load()
    config = paper_config_snapshot(settings_row)
    open_count = portfolio.open_sessions().count()
    for item in selected:
        coin = item["coin"]
        if open_count >= int(settings_row.max_sessions):
            break
        exists = PaperSession.objects.filter(
            coin=coin, status__in=[PaperSession.Status.ACTIVE, PaperSession.Status.PAUSED]
        ).exists()
        if exists:
            continue
        open_count += 1
        session = PaperSession.objects.create(
            coin=coin,
            analysis=analysis,
            status=PaperSession.Status.ACTIVE,
            params=item.get("params") or {},
            strategy_version=resolve_strategy_version(settings_row),
            config_version=resolve_config_version(
                coin.binance_symbol, item.get("params") or {}
            ),
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
        snapshot_session(session, "start")
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


def retire_stale_sessions(
    analysis: AnalysisRun,
    qualifying: list[dict[str, Any]],
    settings_row: StrategySettings,
) -> list[str]:
    """Stop sessions whose coin failed to qualify for N consecutive analyses.

    A coin qualifies when it is eligible and rated prime or viable. Without
    retirement the book only ever grows, and sessions keep trading coins the
    engine itself no longer recommends.
    """

    qualifying_ids = {item["coin"].id for item in qualifying}
    limit = max(1, int(settings_row.retire_after_failed_runs))
    retired: list[str] = []
    for session in portfolio.open_sessions():
        if session.coin_id in qualifying_ids:
            if session.failed_runs:
                session.failed_runs = 0
                session.save(update_fields=["failed_runs", "updated_at"])
            continue
        session.failed_runs += 1
        if session.failed_runs < limit:
            session.save(update_fields=["failed_runs", "updated_at"])
            continue
        params = GridParams.from_dict(session.params or {})
        config = _sim_config(session, settings_row)
        adapter = PaperExecutionAdapter(session, config=config, params=params)
        strategy = IncrementalGridStrategy(params, config, adapter, session.grid_state or {})
        price = float(session.mark_price or session.avg_entry or 0.0)
        reason = f"retired: not qualified for {session.failed_runs} analyses"
        if price > 0:
            strategy.close(price, timezone.now(), "retired")
            adapter.flush()
        fills = adapter.fills
        if fills:
            PaperFill.objects.bulk_create(
                [
                    PaperFill(
                        session=session,
                        strategy_version_id=session.strategy_version_id,
                        config_version_id=session.config_version_id,
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
        session.grid_state = strategy.to_state()
        session.status = PaperSession.Status.STOPPED
        session.stop_reason = reason
        session.promotion_ready = False
        session.save()
        record_event(GridEvent.Type.RETIRE, session=session, reason=reason)
        snapshot_session(session, "retire")
        retired.append(session.coin.symbol)
        log_decision(
            "paper",
            f"{session.coin.symbol} paper session {reason}; position closed.",
            severity="warning",
            analysis=analysis,
            coin=session.coin,
            payload={"session_id": str(session.id), "failed_runs": session.failed_runs},
        )
    return retired


def _fetch_hist(
    client: BinanceFuturesClient,
    symbol: str,
    now: datetime,
    params: GridParams,
    interval: str,
) -> list[dict[str, Any]]:
    # 120 days covers the 50-day trend EMA, the 30-day range/support windows
    # and the ATR warmup with room to spare. Only completed candles are used.
    lookback_days = max(120, params.range_lookback_days + 30, params.support_lookback_days + 90)
    start = now - timedelta(days=lookback_days)
    end = floor_to_interval(now, interval) - timedelta(milliseconds=1)
    return client.klines_range(symbol, interval, start, end, max_requests=10)


def _fetch_price(client: BinanceFuturesClient, symbol: str) -> float:
    payload = client.ticker_price(symbol)
    try:
        return float(payload["price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise BinanceError(f"Invalid price response for {symbol}") from exc


def _fetch_hybrid_frames(
    client: BinanceFuturesClient, symbol: str, now: datetime
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Closed 1h candles for EMA/ADX and closed 15m candles for confirmation."""

    end_1h = floor_to_interval(now, "1h") - timedelta(milliseconds=1)
    klines_1h = client.klines_range(
        symbol, "1h", end_1h - timedelta(hours=240), end_1h, max_requests=2
    )
    end_15m = floor_to_interval(now, "15m") - timedelta(milliseconds=1)
    klines_15m = client.klines_range(
        symbol, "15m", end_15m - timedelta(hours=12), end_15m, max_requests=1
    )
    return klines_1h, klines_15m


def _hybrid_confidence(
    client: BinanceFuturesClient,
    symbol: str,
    now: datetime,
    strategy: IncrementalGridStrategy,
    state: LiveTradingState,
) -> TrendSignal | None:
    try:
        klines_1h, klines_15m = _fetch_hybrid_frames(client, symbol, now)
    except BinanceError as exc:
        logger.warning("Hybrid trend sync failed for %s: %s", symbol, exc)
        return None
    return evaluate_trend(
        klines_1h,
        klines_15m,
        adx_enabled=bool((state.limits or {}).get("hybrid_adx")),
        halted=strategy.hybrid_halted,
    )


def _hybrid_payload(session: PaperSession, signal: TrendSignal) -> dict[str, Any]:
    return {
        "session_id": str(session.id),
        "mode": LiveTradingState.Mode.HYBRID,
        "reason": signal.reason,
        "ema_100": round(signal.ema_100, 8),
        "price_1h": round(signal.price_1h, 8),
        "adx_14": round(signal.adx, 4) if signal.adx is not None else None,
        "plus_di": round(signal.plus_di, 4) if signal.plus_di is not None else None,
        "minus_di": round(signal.minus_di, 4) if signal.minus_di is not None else None,
        "above_streak": signal.above_streak,
        "below_streak": signal.below_streak,
    }


def _grid_log_payload(
    session: PaperSession, strategy: IncrementalGridStrategy, price: float
) -> dict[str, Any]:
    """Audit snapshot: every level with its price, side, size, and skip reason."""

    plan = strategy.level_plan
    return {
        "session_id": str(session.id),
        "price": round(price, 10),
        "range_low": round(strategy.range_low, 10),
        "range_high": round(strategy.range_high, 10),
        "allow_buys": strategy.allow_buys,
        "reason": strategy.last_rebalance_reason,
        "active_levels": sum(1 for item in plan if item.get("reason") == "active"),
        "level_plan": plan,
    }


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


def _funding_window(
    client: BinanceFuturesClient,
    symbol: str,
    since: datetime,
    now: datetime,
    params: GridParams,
) -> tuple[list[dict[str, Any]], bool]:
    """Funding prints covering the z-score lookback and every uncharged event."""

    history_days = max(10, params.funding_z_lookback // 3 + 5)
    start = min(since, now - timedelta(days=history_days))
    try:
        events = client.funding_rates(symbol, start, now, max_requests=6)
    except BinanceError as exc:
        logger.warning("Funding sync failed for %s: %s", symbol, exc)
        return [], False
    if not isinstance(events, list):
        return [], False
    return sorted(events, key=lambda event: event["time"]), True


def _funding_z_at(
    events: list[dict[str, Any]], moment: datetime, params: GridParams
) -> float | None:
    upto = [event for event in events if event["time"] <= moment]
    try:
        return funding_zscore(upto, params.funding_z_lookback)
    except (KeyError, TypeError, ValueError):
        return None


def _tick_session(
    session: PaperSession,
    client: BinanceFuturesClient,
    now: datetime,
    settings_row: StrategySettings | None = None,
    portfolio_block: str = "",
) -> dict[str, Any]:
    settings_row = settings_row or StrategySettings.load()
    params = GridParams.from_dict(session.params or {})
    config = _sim_config(session, settings_row)
    adapter = PaperExecutionAdapter(session, config=config, params=params)
    strategy = IncrementalGridStrategy(params, config, adapter, session.grid_state or {})
    symbol = session.coin.binance_symbol
    if not symbol:
        return {"ok": False, "error": "no Binance symbol"}

    rebalance_event: dict[str, Any] | None = None
    trading_state = LiveTradingState.load()
    hybrid = trading_state.mode == LiveTradingState.Mode.HYBRID
    # Book-level limits only ever block new buys.
    strategy.portfolio_blocked = bool(portfolio_block)

    # A bearish tag keeps the session live for marking/funding but locks the
    # grid: no orders are armed and no fills are simulated.
    locked = session.bias == PaperSession.Bias.BEARISH
    if locked:
        strategy.orders.clear()

    bars_processed = 0
    funding_paid = 0.0
    funding_until = session.last_funding_at or session.started_at
    funding_fetched = True
    last_price = 0.0

    # Completed signal candles: context for every bar is rebuilt from the
    # candles strictly before it, exactly as in the backtest.
    hist: list[dict[str, Any]] = []
    try:
        hist = _fetch_hist(client, symbol, now, params, config.interval)
    except BinanceError as exc:
        logger.warning("Grid history sync failed for %s: %s", symbol, exc)
    hist.sort(key=lambda row: row["time"])

    # Funding is fetched on every tick, not only while a position is open:
    # inventory opened during this tick still owes the prints that follow.
    funding_events, funding_fetched = _funding_window(
        client, symbol, funding_until, now, params
    )

    # Hybrid mode only trades confirmed 1h uptrends. The gate is recomputed
    # from fully closed candles on every tick; normal mode clears any leftover
    # hybrid state from a previous switch.
    trend_signal: TrendSignal | None = None
    if hybrid and not locked:
        trend_signal = _hybrid_confidence(client, symbol, now, strategy, trading_state)
    else:
        strategy.clear_trend_filter()

    # First tick: build the grid around the live price and wait for future bars.
    if not strategy.initialized:
        price = _fetch_price(client, symbol)
        last_price = price
        if trend_signal is not None:
            strategy.apply_trend_signal(trend_signal, price, now)
        strategy.set_funding_context(_funding_z_at(funding_events, now, params))
        if hist:
            strategy.prev_close = float(hist[-1]["close"])
        if not locked:
            strategy.open_initial_inventory(price, now)
        strategy.initialize(hist, price, now)
        if locked:
            strategy.orders.clear()
        adapter.mark(price)
        session.last_funding_at = now
        funding_fetched = True
        log_decision(
            "paper",
            f"{session.coin.symbol} grid initialized with "
            f"{len(strategy.level_plan)} levels "
            f"({sum(1 for item in strategy.level_plan if item['reason'] == 'active')} armed) "
            f"over {strategy.range_low:.6g}-{strategy.range_high:.6g}.",
            coin=session.coin,
            payload={**_grid_log_payload(session, strategy, price), "locked": locked},
        )
    else:
        adapter.mark(session.mark_price or 0.0)
        start = strategy.last_bar_time or session.last_tick_at or session.started_at
        bar_indices = [index for index, row in enumerate(hist) if row["time"] > start]
        charge_from = funding_until

        def charge_funding(up_to: datetime, fallback: float) -> float:
            nonlocal charge_from
            paid = 0.0
            for event in funding_events:
                if charge_from < event["time"] <= up_to:
                    mark = event.get("mark_price") or fallback
                    paid += adapter.apply_funding(float(event["rate"]), float(mark), event["time"])
            charge_from = max(charge_from, up_to)
            return paid

        stopped = False
        stop_reason = ""
        rebalances_before = strategy.rebalances
        for index in bar_indices:
            bar = hist[index]
            funding_paid += charge_funding(bar["time"], float(bar["open"]))
            if locked:
                # Marked and charged funding, but the ladder stays disarmed.
                strategy.prev_close = float(bar["close"])
                strategy.last_bar_time = bar["time"]
                adapter.mark(float(bar["close"]))
                bars_processed += 1
                continue
            result = strategy.step(
                bar,
                hist[:index],
                funding_z=_funding_z_at(funding_events, bar["time"], params),
            )
            bars_processed += 1
            if result.get("stopped"):
                stopped = True
                stop_reason = result.get("reason", "")
                break

        if not stopped:
            funding_paid += charge_funding(now, session.mark_price or session.avg_entry)

        last_price = float(hist[bar_indices[-1]]["close"]) if bar_indices else 0.0
        if last_price <= 0:
            try:
                last_price = _fetch_price(client, symbol)
            except BinanceError:
                last_price = session.mark_price
        if last_price > 0:
            adapter.mark(last_price)

        # The newest closed 1h/15m candles decide whether the ladder may arm.
        # Bars above were replayed under the previous gate; a confirmed exit
        # flattens now so no further inventory is added under a broken trend.
        if not stopped and trend_signal is not None and last_price > 0:
            event = strategy.apply_trend_signal(trend_signal, last_price, now)
            if event == "halted":
                log_decision(
                    "risk",
                    f"{session.coin.symbol} hybrid trend filter halted the grid: "
                    f"2 closed 15m candles below the 1h EMA100. Position closed.",
                    severity="warning",
                    coin=session.coin,
                    payload=_hybrid_payload(session, trend_signal),
                )
            elif event == "resumed":
                log_decision(
                    "paper",
                    f"{session.coin.symbol} hybrid trend filter resumed: 2 closed 15m "
                    "candles back above the 1h EMA100 and entry filters pass.",
                    coin=session.coin,
                    payload=_hybrid_payload(session, trend_signal),
                )

        if stopped:
            session.status = PaperSession.Status.STOPPED
            session.stop_reason = stop_reason
            session.promotion_ready = False

        if strategy.rebalances > rebalances_before:
            rebalance_payload = _grid_log_payload(session, strategy, last_price)
            rebalance_event = {
                "reason": strategy.last_rebalance_reason,
                "after": {
                    key: rebalance_payload[key]
                    for key in ("range_low", "range_high", "active_levels")
                },
            }
            log_decision(
                "paper",
                f"{session.coin.symbol} grid rebalanced "
                f"({strategy.last_rebalance_reason}).",
                coin=session.coin,
                payload=rebalance_payload,
            )

        if funding_fetched:
            funding_until = now

    # Overbought guard: off by default until a backtest shows it adds value.
    # A bearish session is already locked and holds no ladder to protect.
    risk_result = None
    if (
        session.status == PaperSession.Status.ACTIVE
        and not locked
        and settings_row.overbought_guard_enabled
    ):
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
                    strategy_version_id=session.strategy_version_id,
                    config_version_id=session.config_version_id,
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

    if rebalance_event is not None:
        record_event(
            GridEvent.Type.REBALANCE,
            session=session,
            reason=rebalance_event["reason"],
            after=rebalance_event["after"],
        )
        snapshot_session(session, "rebalance")

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
        "mode": trading_state.mode,
        "bars": bars_processed,
        "fills": len(fills),
        "equity": round(session.equity, 4),
        "status": session.status,
        "promotion_ready": session.promotion_ready,
        "risk": risk_result,
        "trend": trend_signal.to_dict() if trend_signal is not None else None,
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
    settings_row = StrategySettings.load()
    book = portfolio.refresh_book_state(settings_row, now)
    block = portfolio.buy_block_reason(book, settings_row)
    results: list[dict[str, Any]] = []
    errors = 0
    total_fills = 0
    for session in session_list:
        try:
            # Row lock: a manual tick and a scheduled tick must never replay
            # the same candles for one session concurrently.
            with transaction.atomic():
                locked_session = (
                    PaperSession.objects.select_for_update()
                    .select_related("coin")
                    .get(id=session.id)
                )
                if locked_session.status != PaperSession.Status.ACTIVE:
                    continue
                session = locked_session
                result = _tick_session(session, client, now, settings_row, block)
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
        "book": {key: value for key, value in book.items() if key != "start"},
        "buy_block": block,
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

    if not settings.RISK.get("ENABLED", True) or not StrategySettings.load().overbought_guard_enabled:
        # With the guard switched off, sessions it paused must not stay
        # paused forever waiting for a recovery check that never runs.
        resumed_ids = []
        for session in PaperSession.objects.filter(status=PaperSession.Status.PAUSED):
            if (session.risk_state or {}).get("phase") == "paused":
                session.risk_state = {}
                session.save(update_fields=["risk_state", "updated_at"])
                resume_session(session)
                resumed_ids.append(str(session.id))
        return {
            "ok": True,
            "enabled": False,
            "sessions": 0,
            "resumed": len(resumed_ids),
            "results": [],
        }
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
        record_event(GridEvent.Type.PAUSE, session=session)
        snapshot_session(session, "pause")
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
        record_event(GridEvent.Type.RESUME, session=session)
        snapshot_session(session, "resume")
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
        record_event(GridEvent.Type.STOP, session=session, reason=reason)
        snapshot_session(session, "stop")
        log_decision(
            "paper",
            f"{session.coin.symbol} paper session stopped: {reason}.",
            severity="warning",
            coin=session.coin,
            payload={"session_id": str(session.id)},
        )
    return session
