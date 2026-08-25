"""Parsing external id strings into UUIDs, in one place.

Three services each carried an identical ``_to_uuid`` — a client-supplied
proposal/decision id is a string at the API boundary but a UUID column in
Postgres, and every read path needs the same fail-soft parse (a malformed
id is a 404, not a 500).
"""

from __future__ import annotations

import uuid

__all__ = ["to_uuid"]


def to_uuid(value: str) -> uuid.UUID | None:
    """Parse ``value`` as a UUID, or ``None`` if it isn't one."""
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None
