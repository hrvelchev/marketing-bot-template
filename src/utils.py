"""Small helpers used across the codebase."""
from __future__ import annotations

from datetime import datetime, timezone


def now_utc() -> datetime:
    """Current time in UTC with tzinfo set."""
    return datetime.now(tz=timezone.utc)


def now_utc_iso() -> str:
    """ISO-8601 string for now() in UTC. Used as the default timestamp value
    for all DB rows."""
    return now_utc().isoformat()
