"""Backfill version stamps, start snapshots and grid events for legacy rows."""

from django.db import migrations

# Intentional: digest is the identity contract shared with the live resolvers.
from apps.engine.versioning import digest

def _grid_spacing(levels):
    """Mean fractional gap between adjacent levels (copy of the live helper)."""
    if not levels or len(levels) < 2:
        return None
    ordered = sorted(levels)
    if ordered[0] <= 0:
        return None
    gaps = [(b - a) / a for a, b in zip(ordered, ordered[1:])]
    return sum(gaps) / len(gaps)


_GRID_KEYS = ("range_low", "range_high", "levels")
_PAUSED = "paper session paused."
_RESUMED = "paper session resumed."


def _strategy_version(apps, snapshot, code_sha="unknown"):
    model = apps.get_model("engine", "StrategyVersion")
    vid = digest({"snapshot": snapshot, "code_sha": code_sha})
    obj, _ = model.objects.get_or_create(
        id=vid,
        defaults={"snapshot": snapshot, "code_sha": code_sha, "parent": None,
                  "diff": {}, "backfilled": True},
    )
    return obj


def _config_version(apps, symbol, params):
    model = apps.get_model("engine", "ConfigVersion")
    vid = digest({"symbol": symbol, "params": params})
    obj, _ = model.objects.get_or_create(
        id=vid, defaults={"symbol": symbol, "params": params, "backfilled": True}
    )
    return obj


def _state(session):
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
        "net_return": session.equity / session.capital - 1.0 if session.capital else 0.0,
        "grid": {
            **{k: grid_state.get(k) for k in _GRID_KEYS},
            "spacing": _grid_spacing(grid_state.get("levels")),
        },
        "stop_reason": session.stop_reason,
    }


def _backfill_sessions(apps):
    PaperSession = apps.get_model("trading", "PaperSession")
    PaperFill = apps.get_model("trading", "PaperFill")
    SessionSnapshot = apps.get_model("trading", "SessionSnapshot")
    for session in PaperSession.objects.select_related("coin").iterator():
        changed = []
        if session.config_version_id is None:
            cv = _config_version(apps, session.coin.binance_symbol, session.params)
            session.config_version_id = cv.id
            changed.append("config_version")
        if session.strategy_version_id is None:
            sv = _strategy_version(apps, {"paper_config": session.config})
            session.strategy_version_id = sv.id
            changed.append("strategy_version")
        if changed:
            PaperSession.objects.filter(pk=session.pk).update(
                **{f"{name}_id": getattr(session, f"{name}_id") for name in changed}
            )
        if not SessionSnapshot.objects.filter(session_id=session.pk, trigger="start").exists():
            SessionSnapshot.objects.create(
                session_id=session.pk,
                time=session.started_at,
                trigger="start",
                state=_state(session),
                strategy_version_id=session.strategy_version_id,
                config_version_id=session.config_version_id,
                backfilled=True,
            )
        PaperFill.objects.filter(session_id=session.pk, strategy_version__isnull=True).update(
            strategy_version_id=session.strategy_version_id
        )
        PaperFill.objects.filter(session_id=session.pk, config_version__isnull=True).update(
            config_version_id=session.config_version_id
        )


def _backfill_analyses(apps):
    CoinAnalysis = apps.get_model("engine", "CoinAnalysis")
    AnalysisRun = apps.get_model("engine", "AnalysisRun")
    for row in CoinAnalysis.objects.filter(config_version__isnull=True).select_related("coin").iterator():
        cv = _config_version(apps, row.coin.binance_symbol, row.params)
        CoinAnalysis.objects.filter(pk=row.pk).update(config_version_id=cv.id)
    for run in AnalysisRun.objects.filter(strategy_version__isnull=True).iterator():
        params = run.params if isinstance(run.params, dict) else {}
        sv = _strategy_version(apps, {"analysis_config": params.get("config", {})})
        AnalysisRun.objects.filter(pk=run.pk).update(strategy_version_id=sv.id)


def _event_for(log, payload):
    """Return (type, reason, after) for a convertible log, else None."""
    # Retire logs are not backfilled: their payload carries no reason.
    # Init logs share the rebalance payload shape, so match on the message.
    if "grid rebalanced" in log.message and all(
        k in payload for k in ("range_low", "range_high", "reason")
    ):
        return "rebalance", str(payload["reason"])[:255], {
            "range_low": payload["range_low"],
            "range_high": payload["range_high"],
            "active_levels": payload.get("active_levels"),
        }
    if log.message.endswith(_PAUSED):
        return "pause", "", {}
    if log.message.endswith(_RESUMED):
        return "resume", "", {}
    return None


def _backfill_events(apps):
    DecisionLog = apps.get_model("engine", "DecisionLog")
    PaperSession = apps.get_model("trading", "PaperSession")
    GridEvent = apps.get_model("trading", "GridEvent")
    sessions = {}
    for log in DecisionLog.objects.filter(category="paper").order_by("created_at", "id").iterator():
        payload = log.payload if isinstance(log.payload, dict) else {}
        raw_id = payload.get("session_id")
        if not raw_id:
            continue
        converted = _event_for(log, payload)
        if converted is None:
            continue
        key = str(raw_id)
        if key not in sessions:
            try:
                sessions[key] = PaperSession.objects.select_related("coin").filter(pk=key).first()
            except (ValueError, TypeError):
                sessions[key] = None
        session = sessions[key]
        if session is None:
            continue
        etype, reason, after = converted
        if GridEvent.objects.filter(session_id=session.pk, time=log.created_at, type=etype).exists():
            continue
        GridEvent.objects.create(
            time=log.created_at,
            type=etype,
            environment="paper",
            symbol=session.coin.binance_symbol,
            session_id=session.pk,
            reason=reason,
            before={},
            after=after,
            strategy_version_id=session.strategy_version_id,
            config_version_id=session.config_version_id,
            backfilled=True,
        )


def backfill(apps, schema_editor):
    _backfill_analyses(apps)
    _backfill_sessions(apps)
    _backfill_events(apps)


class Migration(migrations.Migration):

    dependencies = [
        ("engine", "0007_version_stamps"),
        ("trading", "0006_gridevent_sessionsnapshot_version_stamps"),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]
