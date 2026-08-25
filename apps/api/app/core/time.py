"""UTC "now", in one place.

Eight modules carried their own ``datetime.now(timezone.utc)`` (or the
``UTC`` alias of it) for timestamping rows and cache entries. Harmless
today since they all agreed, but a future call site that used a naive
``datetime.now()`` instead would silently drift local-time writes into
timestamp columns everyone else fills in UTC — a bug that's invisible
until someone compares two rows across a DST boundary.
"""

from __future__ import annotations

from datetime import UTC, datetime

__all__ = ["utc_now"]


def utc_now() -> datetime:
    """The current time, always timezone-aware UTC."""
    return datetime.now(UTC)
