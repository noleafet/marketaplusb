"""Backfill version stamps, start snapshots and grid events for legacy rows."""

import uuid
from collections import defaultdict

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
_CHUNK = 1000


def _chunks(items):
    items = list(items)
    for i in range(0, len(items), _CHUNK):
        yield items[i:i + _CHUNK]


def _stamp(model, field, groups):
    """One UPDATE per version id (chunked): ``groups`` maps version id -> pks."""
    for vid, pks in groups.items():
        for chunk in _chunks(pks):
            model.objects.filter(pk__in=chunk).update(**{field: vid})


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
    has_start = set(
        SessionSnapshot.objects.filter(trigger="start").values_list("session_id", flat=True)
    )
    cv_groups, sv_groups = defaultdict(list), defaultdict(list)
    fill_cv, fill_sv = defaultdict(list), defaultdict(list)
    cv_ids, sv_ids = set(), set()
    snapshots = []
    for session in PaperSession.objects.select_related("coin").iterator():
        if session.config_version_id is None:
            symbol = session.coin.binance_symbol
            vid = digest({"symbol": symbol, "params": session.params})
            if vid not in cv_ids:
                _config_version(apps, symbol, session.params)
                cv_ids.add(vid)
            session.config_version_id = vid
            cv_groups[vid].append(session.pk)
        if session.strategy_version_id is None:
            snap = {"paper_config": session.config}
            vid = digest({"snapshot": snap, "code_sha": "unknown"})
            if vid not in sv_ids:
                _strategy_version(apps, snap)
                sv_ids.add(vid)
            session.strategy_version_id = vid
            sv_groups[vid].append(session.pk)
        if session.pk not in has_start:
            snapshots.append(SessionSnapshot(
                session_id=session.pk,
                time=session.started_at,
                trigger="start",
                state=_state(session),
                strategy_version_id=session.strategy_version_id,
                config_version_id=session.config_version_id,
                backfilled=True,
            ))
        fill_sv[session.strategy_version_id].append(session.pk)
        fill_cv[session.config_version_id].append(session.pk)
    _stamp(PaperSession, "config_version_id", cv_groups)
    _stamp(PaperSession, "strategy_version_id", sv_groups)
    SessionSnapshot.objects.bulk_create(snapshots, batch_size=_CHUNK)
    for vid, session_ids in fill_sv.items():
        for chunk in _chunks(session_ids):
            PaperFill.objects.filter(
                session_id__in=chunk, strategy_version__isnull=True
            ).update(strategy_version_id=vid)
    for vid, session_ids in fill_cv.items():
        for chunk in _chunks(session_ids):
            PaperFill.objects.filter(
                session_id__in=chunk, config_version__isnull=True
            ).update(config_version_id=vid)


def _backfill_analyses(apps):
    CoinAnalysis = apps.get_model("engine", "CoinAnalysis")
    AnalysisRun = apps.get_model("engine", "AnalysisRun")
    # values_list keeps the large chart/folds/trade_sample JSON out of memory.
    groups, seen = defaultdict(list), set()
    rows = CoinAnalysis.objects.filter(config_version__isnull=True).values_list(
        "pk", "params", "coin__binance_symbol"
    )
    for pk, params, symbol in rows.iterator():
        vid = digest({"symbol": symbol, "params": params})
        if vid not in seen:
            _config_version(apps, symbol, params)
            seen.add(vid)
        groups[vid].append(pk)
    _stamp(CoinAnalysis, "config_version_id", groups)

    groups, seen = defaultdict(list), set()
    rows = AnalysisRun.objects.filter(strategy_version__isnull=True).values_list("pk", "params")
    for pk, params in rows.iterator():
        params = params if isinstance(params, dict) else {}
        snap = {"analysis_config": params.get("config", {})}
        vid = digest({"snapshot": snap, "code_sha": "unknown"})
        if vid not in seen:
            _strategy_version(apps, snap)
            seen.add(vid)
        groups[vid].append(pk)
    _stamp(AnalysisRun, "strategy_version_id", groups)


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


def _session_uuid(raw_id):
    try:
        return uuid.UUID(str(raw_id))
    except (ValueError, TypeError, AttributeError):
        return None


def _backfill_events(apps):
    DecisionLog = apps.get_model("engine", "DecisionLog")
    PaperSession = apps.get_model("trading", "PaperSession")
    GridEvent = apps.get_model("trading", "GridEvent")
    candidates = []
    logs = (
        DecisionLog.objects.filter(category="paper")
        .order_by("created_at", "id")
        .only("created_at", "message", "payload")
    )
    for log in logs.iterator():
        payload = log.payload if isinstance(log.payload, dict) else {}
        raw_id = payload.get("session_id")
        if not raw_id:
            continue
        converted = _event_for(log, payload)
        if converted is None:
            continue
        sid = _session_uuid(raw_id)
        if sid is None:
            continue
        candidates.append((log.created_at, sid, converted))
    if not candidates:
        return

    # One query per 1000 sessions, and one for the events they already have.
    sessions = {}
    for chunk in _chunks({sid for _, sid, _ in candidates}):
        for session in PaperSession.objects.select_related("coin").filter(pk__in=chunk):
            sessions[session.pk] = session
    existing = set()
    for chunk in _chunks(sessions):
        existing.update(
            GridEvent.objects.filter(session_id__in=chunk).values_list("session_id", "time", "type")
        )

    events = []
    for created_at, sid, (etype, reason, after) in candidates:
        session = sessions.get(sid)
        if session is None:
            continue
        key = (session.pk, created_at, etype)
        if key in existing:
            continue
        existing.add(key)
        events.append(GridEvent(
            time=created_at,
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
        ))
    GridEvent.objects.bulk_create(events, batch_size=_CHUNK)


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
