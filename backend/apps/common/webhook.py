"""Outbound webhook alerts for risk events.

Alerts are best-effort: a missing URL or an unreachable endpoint is logged and
never interrupts a trading tick.
"""

from __future__ import annotations

import logging
from typing import Any

import requests
from django.conf import settings

logger = logging.getLogger("engine")


def _default_text(payload: dict[str, Any]) -> str:
    symbol = payload.get("symbol") or payload.get("coin") or "unknown"
    event = payload.get("event") or "risk_event"
    reason = payload.get("reason") or ""
    price = payload.get("price")
    price_text = f" @ {price:.8g}" if isinstance(price, (int, float)) else ""
    return f"{event.upper()} {symbol}{price_text} {reason}".strip()


def send_webhook(
    payload: dict[str, Any],
    *,
    url: str | None = None,
    timeout: int | None = None,
) -> bool:
    """POST ``payload`` as JSON. Returns True only when the endpoint accepted it."""

    target = url if url is not None else str(settings.RISK.get("WEBHOOK_URL") or "")
    if not target:
        return False
    body = dict(payload)
    body.setdefault("text", _default_text(payload))
    wait = timeout or int(settings.RISK.get("WEBHOOK_TIMEOUT", 10))
    try:
        response = requests.post(target, json=body, timeout=wait)
    except requests.RequestException as exc:
        logger.warning("Risk webhook failed: %s", exc)
        return False
    if response.status_code >= 400:
        logger.warning("Risk webhook rejected with HTTP %s", response.status_code)
        return False
    return True
