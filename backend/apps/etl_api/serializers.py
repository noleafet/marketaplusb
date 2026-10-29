from datetime import datetime, timezone as dt_timezone


def iso(dt: datetime | None) -> str | None:
    """UTC ISO-8601 with a Z suffix."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=dt_timezone.utc)
    dt = dt.astimezone(dt_timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")
