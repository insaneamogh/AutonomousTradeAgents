"""Demo-user identity for MCP tool calls.

There is no real per-MCP-caller identity to authenticate here — the caller
is Claude Desktop or the MCP Inspector, not an end user with a mobile-app
session — so every tool call is attributed to a fixed fixture user, the
same one ``DEV_AUTH_BYPASS`` resolves requests to elsewhere in this
codebase (see ``app.services.auth.auth_store.FIXTURE_USER_ID``). This
reuses that existing fixture-user pattern rather than building new
per-caller auth infrastructure for a server nobody logs into.

Guardrail: every tool in ``mcp_server.tools`` must pass ``DEMO_USER_ID`` as
its ``user_id`` — never ``None``, and never the ``ALL_USERS`` sentinel
(``trading_agents.memory.decision_log.ALL_USERS``). ``ALL_USERS`` exists
only for whole-book scheduled jobs (the EOD reflection pass, the
ghost-P&L marker, the daily cron) that have no requesting user to scope
on; an MCP tool call always has this one fixed demo identity instead.
"""

from __future__ import annotations

import os

from app.services.auth.auth_store import FIXTURE_USER_ID

DEMO_USER_ID: str = os.environ.get("MCP_DEMO_USER_ID", "").strip() or FIXTURE_USER_ID


async def ensure_demo_user_seeded() -> None:
    """Force ``PostgresAuthStore``'s lazy fixture-user seed before the
    server accepts tool calls.

    ``AgentDecision.user_id`` (``packages/engine/engine/db/models/council.py``)
    is a NOT NULL foreign key to ``users.id``. No Alembic migration seeds
    that row directly — ``PostgresAuthStore._ensure_seed()`` inserts it
    lazily, per-process, on the store's first use (see
    ``app.services.auth.postgres_auth_store``, called from every one of
    its own methods). If this MCP server is the first — or only —
    Postgres-backed process to touch a freshly migrated database,
    ``run_council_pass``'s decision-log write would hit that FK
    constraint and fail on its very first call. Calling
    ``get_auth_store().get_user_by_id()`` once here, before the server
    starts serving tool calls, forces the same idempotent seed
    ``_ensure_seed`` performs everywhere else in the app.

    No-op when ``USE_POSTGRES`` is off — ``InMemoryDecisionLog`` has no FK
    to violate, and there is no store to seed.
    """
    from engine.env import env_flag

    if not env_flag("USE_POSTGRES"):
        return

    from app.services.auth.auth_store import get_auth_store

    await get_auth_store().get_user_by_id(DEMO_USER_ID)
