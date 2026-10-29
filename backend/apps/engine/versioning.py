"""Content-hashed strategy and config versions."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

from django.conf import settings
from django.db import IntegrityError, transaction

from apps.engine.models import ConfigVersion, StrategyVersion

SNAPSHOT_FIELDS = (
    "signal_interval",
    "fill_model",
    "trade_through_bps",
    "overbought_guard_enabled",
    "holdout_days",
    "book_capital",
    "max_sessions",
    "max_book_exposure",
    "daily_loss_limit_pct",
    "retire_after_failed_runs",
    "max_strategy_correlation",
)

# Deployment knobs that never change strategy results, so they stay out of the snapshot.
OPERATIONAL_KEYS = {
    "engine": ("WORKERS", "CANDLE_SYNC_TIMEOUT", "SYMBOL_LIMIT"),
    "paper": ("TICK_MINUTES",),
}


def _normalise(obj: Any) -> Any:
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        return float(f"{obj:.10g}")
    if isinstance(obj, dict):
        return {str(k): _normalise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_normalise(v) for v in obj]
    return obj


def canonical_json(obj: Any) -> str:
    return json.dumps(
        _normalise(obj), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def digest(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def current_code_sha() -> str:
    return os.environ.get("GIT_SHA") or "unknown"


def strategy_snapshot(settings_row=None) -> dict:
    # Imported lazily: apps.trading imports from apps.engine.
    from apps.trading.models import StrategySettings

    row = settings_row or StrategySettings.load()
    snap = {name: getattr(row, name) for name in SNAPSHOT_FIELDS}
    for group, source in (("engine", settings.ENGINE), ("paper", settings.PAPER)):
        skip = OPERATIONAL_KEYS[group]
        snap[group] = {k: v for k, v in source.items() if k not in skip}
    return snap


def _flatten(obj: Any, prefix: str = "") -> dict:
    if isinstance(obj, dict) and obj:
        out: dict = {}
        for k, v in obj.items():
            out.update(_flatten(v, f"{prefix}{k}."))
        return out
    return {prefix[:-1]: obj}


def diff_snapshots(old: dict, new: dict) -> dict:
    a, b = _flatten(_normalise(old)), _flatten(_normalise(new))
    return {
        key: {"from": a.get(key), "to": b.get(key)}
        for key in sorted(a.keys() | b.keys())
        if a.get(key) != b.get(key)
    }


def _get_or_create(model, pk, defaults):
    try:
        with transaction.atomic():
            obj, _ = model.objects.get_or_create(id=pk, defaults=defaults)
        return obj
    except IntegrityError:
        return model.objects.get(id=pk)


def resolve_strategy_version(
    settings_row=None,
    *,
    snapshot: dict | None = None,
    code_sha: str | None = None,
    backfilled: bool = False,
) -> StrategyVersion:
    snap = snapshot if snapshot is not None else strategy_snapshot(settings_row)
    sha = code_sha if code_sha is not None else current_code_sha()
    vid = digest({"snapshot": snap, "code_sha": sha})
    existing = StrategyVersion.objects.filter(id=vid).first()
    if existing is not None:
        return existing
    # Backfilled versions have synthetic snapshots; diffing against them is meaningless.
    parent = (
        StrategyVersion.objects.exclude(backfilled=True)
        .order_by("-first_seen_at", "-id")
        .first()
    )
    diff = diff_snapshots(parent.snapshot, snap) if parent else {}
    if parent and parent.code_sha != sha:
        diff["code_sha"] = {"from": parent.code_sha, "to": sha}
    return _get_or_create(
        StrategyVersion,
        vid,
        {
            "snapshot": snap,
            "code_sha": sha,
            "parent": parent,
            "diff": diff,
            "backfilled": backfilled,
        },
    )


def resolve_config_version(
    symbol: str, params: dict, *, backfilled: bool = False
) -> ConfigVersion:
    vid = digest({"symbol": symbol, "params": params})
    existing = ConfigVersion.objects.filter(id=vid).first()
    if existing is not None:
        return existing
    return _get_or_create(
        ConfigVersion,
        vid,
        {"symbol": symbol, "params": params, "backfilled": backfilled},
    )
