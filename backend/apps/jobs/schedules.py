"""Schedule definitions and next-run computation (UTC)."""

from __future__ import annotations

from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from apps.jobs.models import JobSchedule

DEFAULT_SCHEDULES = [
    {
        "slug": "coins-refresh",
        "name": "Monthly top-100 coin refresh",
        "description": "Fetch the top 100 coins by market cap and remap Binance futures eligibility.",
        "job_type": "coins_refresh",
        "kind": JobSchedule.Kind.MONTHLY,
        "params": {},
        "offset_minutes": 0,
    },
    {
        "slug": "daily-analysis",
        "name": "Daily grid suitability analysis",
        "description": "Sync market data, run walk-forward backtests, rank coins, and produce reports.",
        "job_type": "daily_analysis",
        "kind": JobSchedule.Kind.DAILY,
        "params": {},
        "offset_minutes": 10,
    },
    {
        "slug": "paper-tick",
        "name": "Paper trading tick",
        "description": "Process live Binance data into active paper sessions.",
        "job_type": "paper_tick",
        "kind": JobSchedule.Kind.INTERVAL,
        "params": {},
        "interval_seconds": max(60, int(settings.PAPER.get("TICK_MINUTES", 5)) * 60),
    },
    {
        "slug": "paper-evaluate",
        "name": "Paper performance evaluation",
        "description": "Evaluate active paper sessions against profitability and risk thresholds.",
        "job_type": "paper_evaluate",
        "kind": JobSchedule.Kind.INTERVAL,
        "params": {},
        "interval_seconds": max(300, int(settings.PAPER.get("EVAL_INTERVAL_HOURS", 6)) * 3600),
    },
    {
        "slug": "paper-risk",
        "name": "Overbought pause/resume guard",
        "description": (
            "Evaluate 15m RSI/MACD/VWAP and 1h open interest to pause paper "
            "sessions near exhaustion and resume them after recovery."
        ),
        "job_type": "paper_risk",
        "kind": JobSchedule.Kind.INTERVAL,
        "params": {},
        "interval_seconds": max(60, int(settings.RISK.get("INTERVAL_SECONDS", 300))),
    },
    {
        "slug": "btc-open-interest",
        "name": "BTC open-interest recorder",
        "description": (
            "Store BTC 1h open interest so the BTC market regime's OI flush "
            "signal can be backtested."
        ),
        "job_type": "btc_open_interest",
        "kind": JobSchedule.Kind.INTERVAL,
        "params": {},
        "interval_seconds": 3600,
    },
]


def _offset_minutes(schedule: JobSchedule) -> int:
    try:
        return int((schedule.params or {}).get("offset_minutes", 0))
    except (TypeError, ValueError):
        return 0


def _at_offset(day: datetime, minutes: int) -> datetime:
    base = day.replace(hour=0, minute=0, second=0, microsecond=0)
    return base + timedelta(minutes=max(0, minutes))


def next_run_for(schedule: JobSchedule, now: datetime | None = None) -> datetime:
    now = now or timezone.now()
    offset = _offset_minutes(schedule)
    if schedule.kind == JobSchedule.Kind.INTERVAL:
        seconds = schedule.interval_seconds or 300
        return now + timedelta(seconds=max(30, int(seconds)))
    if schedule.kind == JobSchedule.Kind.DAILY:
        candidate = _at_offset(now, offset)
        if candidate <= now:
            candidate = _at_offset(now + timedelta(days=1), offset)
        return candidate
    if schedule.kind == JobSchedule.Kind.MONTHLY:
        year, month = now.year, now.month
        if now >= _at_offset(now.replace(day=1), offset):
            month += 1
            if month > 12:
                month = 1
                year += 1
        day = now.replace(year=year, month=month, day=1)
        return _at_offset(day, offset)
    # Unknown kinds should not spin: retry tomorrow.
    return _at_offset(now + timedelta(days=1), offset)


def ensure_schedules() -> list[JobSchedule]:
    """Create missing default schedules without overwriting operator settings."""
    now = timezone.now()
    created: list[JobSchedule] = []
    for definition in DEFAULT_SCHEDULES:
        params = dict(definition["params"])
        params.setdefault("offset_minutes", definition.get("offset_minutes", 0))
        schedule, was_created = JobSchedule.objects.get_or_create(
            slug=definition["slug"],
            defaults={
                "name": definition["name"],
                "description": definition["description"],
                "job_type": definition["job_type"],
                "kind": definition["kind"],
                "params": params,
                "interval_seconds": definition.get("interval_seconds"),
                "enabled": True,
            },
        )
        if was_created:
            schedule.next_run_at = next_run_for(schedule, now)
            schedule.save(update_fields=["next_run_at"])
            created.append(schedule)
        elif "offset_minutes" not in (schedule.params or {}):
            schedule.params = {**(schedule.params or {}), "offset_minutes": params["offset_minutes"]}
            schedule.next_run_at = next_run_for(schedule, now)
            schedule.save(update_fields=["params", "next_run_at"])
        elif schedule.next_run_at is None and schedule.enabled:
            schedule.next_run_at = next_run_for(schedule, now)
            schedule.save(update_fields=["next_run_at"])
    return created


def schedule_payload(schedule: JobSchedule) -> dict:
    return {
        "slug": schedule.slug,
        "name": schedule.name,
        "description": schedule.description,
        "job_type": schedule.job_type,
        "kind": schedule.kind,
        "params": schedule.params,
        "enabled": schedule.enabled,
        "interval_seconds": schedule.interval_seconds,
        "next_run_at": schedule.next_run_at,
        "last_enqueued_at": schedule.last_enqueued_at,
        "last_job_id": str(schedule.last_job_id) if schedule.last_job_id else None,
    }
