"""Job handlers. Each handler receives the job params and the JobRun."""

from __future__ import annotations

import logging
from typing import Any, Callable

from django.utils import timezone

from apps.coins.services.ingest import refresh_top_coins
from apps.engine.analysis import run_daily_analysis
from apps.engine.models import AnalysisRun, log_decision
from apps.trading.paper import evaluate_session, run_risk_guard, tick_paper_sessions

logger = logging.getLogger("jobs")

JobHandler = Callable[[dict[str, Any], "JobRun"], dict[str, Any]]


def handle_coins_refresh(params: dict[str, Any], job_run) -> dict[str, Any]:
    per_page = int(params.get("per_page", 100))
    result = refresh_top_coins(per_page=per_page)
    if not result.get("ok"):
        raise RuntimeError(result.get("error") or "coin refresh failed")
    return result


def handle_daily_analysis(params: dict[str, Any], job_run) -> dict[str, Any]:
    trigger = "scheduled" if job_run.trigger == "scheduled" else "manual"
    analysis = run_daily_analysis(
        trigger=trigger,
        params=params,
        job_run_id=job_run.id,
    )
    if analysis.status != analysis.Status.SUCCEEDED:
        raise RuntimeError(analysis.error or "analysis run failed")
    result: dict[str, Any] = {
        "analysis_id": str(analysis.id),
        "status": analysis.status,
        "summary": analysis.summary,
        "duration_seconds": analysis.duration_seconds,
    }

    # The hybrid run replays the same universe with the closed 1h/15m trend
    # gate. It depends on the normal run only for the freshly synced candles.
    try:
        hybrid = run_daily_analysis(
            trigger=trigger,
            params=params,
            job_run_id=job_run.id,
            trend_mode=AnalysisRun.Mode.HYBRID,
        )
    except Exception as exc:  # noqa: BLE001 - normal results are already persisted
        logger.exception("Hybrid analysis run failed")
        raise RuntimeError(f"hybrid analysis run failed: {exc}") from exc
    result["hybrid"] = {
        "analysis_id": str(hybrid.id),
        "status": hybrid.status,
        "summary": hybrid.summary,
        "duration_seconds": hybrid.duration_seconds,
    }
    if hybrid.status != hybrid.Status.SUCCEEDED:
        raise RuntimeError(hybrid.error or "hybrid analysis run failed")
    return result


def handle_paper_tick(params: dict[str, Any], job_run) -> dict[str, Any]:
    session_ids = params.get("session_ids") or None
    return tick_paper_sessions(session_ids=session_ids, job_run_id=job_run.id)


def handle_paper_evaluate(params: dict[str, Any], job_run) -> dict[str, Any]:
    from apps.trading.models import PaperSession

    now = timezone.now()
    sessions = PaperSession.objects.filter(
        status__in=[PaperSession.Status.ACTIVE, PaperSession.Status.STOPPED]
    ).select_related("coin")
    results = []
    passed = 0
    for session in sessions:
        evaluation = evaluate_session(session, now=now, force=bool(params.get("force")))
        if evaluation and evaluation.get("passed"):
            passed += 1
        results.append(
            {
                "session_id": str(session.id),
                "symbol": session.coin.symbol,
                "evaluated": evaluation is not None,
                "passed": bool(evaluation and evaluation.get("passed")),
                "promotion_ready": session.promotion_ready,
            }
        )
    summary = {
        "ok": True,
        "sessions": len(results),
        "promotion_ready": passed,
        "results": results,
    }
    log_decision(
        "paper",
        f"Paper evaluation scanned {len(results)} session(s); {passed} meet thresholds.",
        job_run_id=job_run.id,
        payload={"promotion_ready": passed},
    )
    return summary


def handle_paper_risk(params: dict[str, Any], job_run) -> dict[str, Any]:
    return run_risk_guard(job_run_id=job_run.id)


HANDLERS: dict[str, JobHandler] = {
    "coins_refresh": handle_coins_refresh,
    "daily_analysis": handle_daily_analysis,
    "paper_tick": handle_paper_tick,
    "paper_evaluate": handle_paper_evaluate,
    "paper_risk": handle_paper_risk,
}


def run_handler(job_type: str, params: dict[str, Any], job_run) -> dict[str, Any]:
    handler = HANDLERS.get(job_type)
    if handler is None:
        raise ValueError(f"Unknown job type: {job_type}")
    return handler(params, job_run)
