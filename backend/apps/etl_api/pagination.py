"""Keyset (cursor) pagination ordered by ``(key, id)`` with watermarks.

Cursor ids: the ``id`` in a cursor is the row's primary key. Integers round-trip
as integers; UUIDs (and any other non-int) are stored as ``str(pk)`` and are
converted back to the model's pk type before filtering.

Every page is held back: rows with ``key > now - HOLD_BACK`` are never returned,
so a consumer walking by watermark cannot miss rows committed late.
"""

import base64
import binascii
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone as dt_timezone
from typing import Any, Callable

from django.db.models import IntegerField, Q
from django.utils import timezone

from .errors import CursorExpired, InvalidParameter
from .params import HOLD_BACK, Query, parse_datetime
from .serializers import iso


@dataclass
class Page:
    rows: list
    next_cursor: str | None
    watermark: str | None


def _bad_cursor():
    return InvalidParameter("Invalid cursor.", errors=[{"field": "cursor", "code": "invalid"}])


def _json_id(value: Any):
    return value if isinstance(value, int) and not isinstance(value, bool) else str(value)


def encode_cursor(endpoint: str, key: datetime, id: Any) -> str:
    payload = {"e": endpoint, "k": iso(key), "i": _json_id(id)}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode_cursor(endpoint: str, token: str) -> tuple[datetime, Any]:
    try:
        data = json.loads(base64.urlsafe_b64decode(token.encode()))
    except (ValueError, binascii.Error, UnicodeError):
        raise _bad_cursor()
    if not isinstance(data, dict):
        raise _bad_cursor()
    e, k, i = data.get("e"), data.get("k"), data.get("i")
    if not isinstance(e, str) or not isinstance(k, str):
        raise _bad_cursor()
    if isinstance(i, bool) or not isinstance(i, (int, str)):
        raise _bad_cursor()
    try:
        key = parse_datetime(k, "cursor")
    except InvalidParameter:
        raise _bad_cursor()
    if e != endpoint:
        raise CursorExpired("Cursor was issued for a different endpoint.")
    return key, i


def _row_value(row, path: str):
    for part in path.split("__"):
        row = getattr(row, part)
    return row


def _coerce_id(model, value):
    """Convert a decoded cursor id to the type of the model's primary key."""
    pk = model._meta.pk
    if pk.get_internal_type() == "UUIDField":
        try:
            return uuid.UUID(str(value))
        except ValueError:
            raise _bad_cursor()
    if isinstance(pk, (IntegerField,)) and not isinstance(value, int):
        raise _bad_cursor()
    return value


def _build(rows, got_more, query, endpoint, key_of, id_of) -> Page:
    rows = rows[: query.page_size]
    next_cursor = None
    watermark = iso(query.updated_since)
    if rows:
        last = rows[-1]
        watermark = iso(key_of(last))
        if got_more:
            next_cursor = encode_cursor(endpoint, key_of(last), id_of(last))
    return Page(rows=rows, next_cursor=next_cursor, watermark=watermark)


def paginate_queryset(qs, *, endpoint: str, key: str, query: Query) -> Page:
    """Page a queryset by ``(key, id)``.

    ``key`` may be a related lookup path (``analysis__data_cutoff``). Filters
    applied to ``key``: ``updated_since`` (>=), ``start`` (>=) and ``end`` (<=)
    when set (an endpoint wanting different semantics should pre-filter ``qs``
    and leave those unset), the hold-back (<= now - HOLD_BACK), and the cursor.
    """
    qs = qs.filter(**{f"{key}__lte": timezone.now() - HOLD_BACK})
    if query.updated_since:
        qs = qs.filter(**{f"{key}__gte": query.updated_since})
    if query.start:
        qs = qs.filter(**{f"{key}__gte": query.start})
    if query.end:
        qs = qs.filter(**{f"{key}__lte": query.end})
    if query.cursor:
        k, i = decode_cursor(endpoint, query.cursor)
        i = _coerce_id(qs.model, i)
        qs = qs.filter(Q(**{f"{key}__gt": k}) | Q(**{key: k, "pk__gt": i}))
    rows = list(qs.order_by(key, "pk")[: query.page_size + 1])
    return _build(
        rows,
        len(rows) > query.page_size,
        query,
        endpoint,
        lambda r: _row_value(r, key),
        lambda r: r.pk,
    )


def paginate_rows(
    fetch: Callable[[datetime | None, Any, int], list[dict]],
    *,
    endpoint: str,
    key: str,
    id_key: str,
    query: Query,
) -> Page:
    """Same contract for raw-SQL sources.

    ``fetch(after_key, after_id, limit)`` must return dict rows ordered by
    ``(key, id_key)`` strictly after the cursor (all rows when the cursor is
    absent), applying the endpoint's filters and the hold-back itself.
    """
    after_key, after_id = (None, None)
    if query.cursor:
        after_key, after_id = decode_cursor(endpoint, query.cursor)
    rows = fetch(after_key, after_id, query.page_size + 1)
    return _build(
        rows, len(rows) > query.page_size, query, endpoint, lambda r: r[key], lambda r: r[id_key]
    )


def page_meta(page: Page, query: Query) -> dict:
    return {
        "next_cursor": page.next_cursor,
        "watermark": page.watermark,
        "page_size": query.page_size,
    }
