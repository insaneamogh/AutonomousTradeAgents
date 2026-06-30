"""In-process sliding-window rate limiter.

Guards abuse-prone unauthenticated endpoints (magic-link issuance). Keyed
on email + client IP. In-memory + single-process — fine for the single
uvicorn worker we run today; a Redis-backed window is the multi-worker
upgrade (noted in HANDOFF). Fail-open on internal error: a limiter bug
must never lock users out of login.
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

# Defaults from the audit: 5 magic-links/hour/email, 30/hour/IP.
EMAIL_LIMIT = 5
IP_LIMIT = 30
WINDOW_SECONDS = 3600


class SlidingWindowLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, *, limit: int, window: int = WINDOW_SECONDS) -> bool:
        """Record a hit for ``key``; return True if still within ``limit``
        over the trailing ``window`` seconds, False if it would exceed."""
        now = time.time()
        cutoff = now - window
        with self._lock:
            q = self._hits[key]
            while q and q[0] < cutoff:
                q.popleft()
            if len(q) >= limit:
                return False
            q.append(now)
            return True

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_limiter = SlidingWindowLimiter()


def check_login_rate(email: str, ip: str | None) -> bool:
    """True if this magic-link request is allowed. Charges both the email
    and IP windows (both must pass)."""
    try:
        email_ok = _limiter.allow(f"email:{email.lower().strip()}", limit=EMAIL_LIMIT)
        ip_ok = _limiter.allow(f"ip:{ip or 'unknown'}", limit=IP_LIMIT)
        return email_ok and ip_ok
    except Exception:  # noqa: BLE001 — never lock users out on a limiter bug
        return True


def reset_rate_limit_for_tests() -> None:
    _limiter.reset()
