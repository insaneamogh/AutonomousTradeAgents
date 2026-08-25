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

# Refresh: a legit device refreshes ~4×/hour (15-min access TTL); 60/hour/IP
# is generous headroom while still capping a stolen-token flood.
REFRESH_IP_LIMIT = 60

# Verify: each attempt costs a scrypt(n=2**14) per outstanding magic-link for
# the email, so an unthrottled endpoint is a CPU amplifier. A real user needs
# one verify per login (two or three with fat fingers); 10/hour/email and
# 40/hour/IP leave room for a shared NAT without leaving the amplifier open.
VERIFY_EMAIL_LIMIT = 10
VERIFY_IP_LIMIT = 40


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


def check_verify_rate(email: str, ip: str | None) -> bool:
    """True if this magic-link verification attempt is allowed.

    Charges both the email and IP windows (both must pass), like
    ``check_login_rate`` — verification is the expensive half of the flow
    (one scrypt per outstanding link) so it needs its own cap.
    """
    try:
        email_ok = _limiter.allow(
            f"verify-email:{email.lower().strip()}", limit=VERIFY_EMAIL_LIMIT
        )
        ip_ok = _limiter.allow(f"verify-ip:{ip or 'unknown'}", limit=VERIFY_IP_LIMIT)
        return email_ok and ip_ok
    except Exception:
        # Never lock users out on a limiter bug.
        return True


def check_refresh_rate(ip: str | None) -> bool:
    """True if this refresh is allowed (per-IP flood cap)."""
    try:
        return _limiter.allow(f"refresh-ip:{ip or 'unknown'}", limit=REFRESH_IP_LIMIT)
    except Exception:  # noqa: BLE001 — never lock users out on a limiter bug
        return True


def reset_rate_limit_for_tests() -> None:
    _limiter.reset()
