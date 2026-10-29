"""Query-string parsing shared by every v1 list endpoint."""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as dt_timezone
from uuid import UUID

from .errors import InvalidParameter, WindowTooLarge

ENVIRONMENTS = ("backtest", "walk_forward", "paper", "live")
HOLD_BACK = timedelta(seconds=5)
DEFAULT_PAGE_SIZE = 500


@dataclass
class Query:
    symbols: list[str] = field(default_factory=list)
    timeframe: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    updated_since: datetime | None = None
    environment: str | None = None
    strategy_version: str | None = None
    config_version: str | None = None
    session: UUID | None = None
    cursor: str | None = None
    page_size: int = DEFAULT_PAGE_SIZE


def _invalid(message, name, code):
    return InvalidParameter(message, errors=[{"field": name, "code": code}])


def parse_datetime(value: str, name: str) -> datetime:
    """ISO-8601 (``Z`` accepted); naive is taken as UTC, offsets converted to UTC."""
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            return dt.replace(tzinfo=dt_timezone.utc)
        return dt.astimezone(dt_timezone.utc)
    except (ValueError, OverflowError):
        raise _invalid(f"'{name}' is not a valid ISO-8601 datetime.", name, "invalid_datetime")


def parse_query(
    request,
    *,
    required: tuple[str, ...] = (),
    max_page_size: int = 1000,
    max_window_days: int | None = None,
    environments: tuple[str, ...] = ENVIRONMENTS,
) -> Query:
    """Parse and validate ``request.query_params`` (a DRF Request); unknown keys are ignored."""
    params = request.query_params
    query = Query(page_size=min(DEFAULT_PAGE_SIZE, max_page_size))

    query.symbols = [s.strip().upper() for s in params.getlist("symbol") if s.strip()]
    for name in ("timeframe", "strategy_version", "config_version", "cursor"):
        setattr(query, name, params.get(name) or None)
    for name in ("start", "end", "updated_since"):
        raw = params.get(name)
        if raw:
            setattr(query, name, parse_datetime(raw, name))

    missing = []
    for name in required:
        attr = "symbols" if name == "symbol" else name
        if not getattr(query, attr, None):
            missing.append(name)
    if missing:
        raise InvalidParameter(
            "Missing required parameter(s): " + ", ".join(missing) + ".",
            errors=[{"field": n, "code": "required"} for n in missing],
        )

    if query.start and query.end:
        if query.start > query.end:
            raise _invalid("'start' must not be after 'end'.", "start", "after_end")
        if max_window_days is not None and query.end - query.start > timedelta(
            days=max_window_days
        ):
            raise WindowTooLarge(
                f"The start-end window may not exceed {max_window_days} days.",
                errors=[{"field": "end", "code": "window_too_large"}],
            )

    env = params.get("environment")
    if env:
        if env not in environments:
            raise _invalid(
                "'environment' must be one of: " + ", ".join(environments) + ".",
                "environment",
                "invalid_choice",
            )
        query.environment = env

    raw = params.get("session")
    if raw:
        try:
            query.session = UUID(raw)
        except ValueError:
            raise _invalid("'session' must be a UUID.", "session", "invalid_uuid")

    raw = params.get("page_size")
    if raw not in (None, ""):
        try:
            size = int(raw)
        except ValueError:
            size = 0
        if size < 1 or size > max_page_size:
            raise _invalid(
                f"'page_size' must be an integer between 1 and {max_page_size}.",
                "page_size",
                "out_of_range",
            )
        query.page_size = size
    return query
