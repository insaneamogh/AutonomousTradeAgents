"""FastAPI middleware + Depends helpers.

Kept as a thin module so a future ASGI-level middleware (rate limiting,
request-id, audit log) lands next to ``get_current_user`` without a flat
``app/`` layout.
"""

from app.middleware.auth import (
    AuthedUser,
    get_current_user,
    require_real_auth,
)

__all__ = [
    "AuthedUser",
    "get_current_user",
    "require_real_auth",
]
