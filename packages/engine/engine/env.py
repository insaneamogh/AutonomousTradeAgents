"""Environment-flag parsing, in one place.

Feature switches in this repo are environment booleans — ``USE_POSTGRES``,
``AGENTS_REQUIRE_REAL_DATA``, ``DEV_AUTH_BYPASS``, and friends. Every module
that read one used to carry its own private copy of the same four-line
truthiness check. That is fine until two copies disagree about whether
``"on"`` counts, at which point a flag means one thing in the risk path and
another in the API — a class of bug that is invisible in review.

``engine`` is the package both ``apps/api`` and ``apps/agents`` already
depend on, so the single definition lives here.
"""

from __future__ import annotations

import os

_TRUTHY = frozenset({"1", "true", "yes", "on"})

__all__ = ["env_flag"]


def env_flag(name: str, *, default: bool = False) -> bool:
    """Read ``name`` from the environment as a boolean feature switch.

    Truthy values are ``1``/``true``/``yes``/``on``, case- and
    whitespace-insensitive. An unset variable yields ``default``; anything
    else set but unrecognised is False, so a typo'd value fails closed
    rather than silently enabling a flag.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY
