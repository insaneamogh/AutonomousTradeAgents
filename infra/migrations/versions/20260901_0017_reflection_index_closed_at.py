"""ix_agent_decisions_pending_reflection: triggered_at -> closed_at

Revision ID: 0017_reflection_index_closed_at
Revises: 0016_auto_approve_consent
Create Date: 2026-09-01

``PostgresDecisionLog.list_pending_reflection`` filtered/ordered on
``triggered_at`` (when a decision's entry order was placed) instead of
``closed_at`` (migration 0009 — when the position actually went flat).
Those can be days apart for an ordinary swing position, and
``triggered_at`` only ever gets OLDER relative to "now" — so a decision
that took longer than the reflection job's ``since`` window (24h in the
daily cron) to close became permanently invisible to Reflection the
moment it finally closed, not just late for one cycle.

Verified live 2026-09-01 against the production DB: 6/6 real closed
decisions (triggered ~117h earlier, closed ~48h earlier) still had
``reviewed_at IS NULL``, and every ``strategy_confidence`` row was still
sitting at the migration-0003 seed (confidence=0.500) — Reflection had
never once fired against real data. This is the root cause of the Review
screen's agreement stat reading a flat 0%.

The application-level query fix lives in
``apps/agents/trading_agents/memory/postgres.py``
(``PostgresDecisionLog.list_pending_reflection``); this migration only
moves the supporting partial index so it matches the query's new shape,
per that method's own docstring contract ("the trailing WHERE clauses are
byte-equal to the index predicate").
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017_reflection_index_closed_at"
down_revision: str | None = "0016_auto_approve_consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "ix_agent_decisions_pending_reflection",
        table_name="agent_decisions",
    )
    op.create_index(
        "ix_agent_decisions_pending_reflection",
        "agent_decisions",
        ["closed_at"],
        postgresql_where=sa.text(
            "reviewed_at IS NULL AND realized_pnl IS NOT NULL AND closed_at IS NOT NULL"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_agent_decisions_pending_reflection",
        table_name="agent_decisions",
    )
    op.create_index(
        "ix_agent_decisions_pending_reflection",
        "agent_decisions",
        ["triggered_at"],
        postgresql_where=sa.text("reviewed_at IS NULL AND realized_pnl IS NOT NULL"),
    )
