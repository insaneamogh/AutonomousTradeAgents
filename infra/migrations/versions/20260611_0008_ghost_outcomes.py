"""ghost_outcomes — what vetoed/declined picks would have done

Revision ID: 0008_ghost_outcomes
Revises: 0007_llm_calls
Create Date: 2026-06-11

Ghost P&L feature. One row per non-executed decision (risk-vetoed,
user-declined, expired). The daily evaluator appends a mark per trading
day until the proposal's horizon elapses, then finalizes ``ghost_pnl``.

Separate table (not columns on agent_decisions) on purpose: the decision
row stays an immutable audit anchor — same reasoning that put operator
grades in ``decision_review`` — while ghost evaluation is multi-write
and re-runnable.

Computation is deterministic Python over close prices. No LLM anywhere
in this path.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_ghost_outcomes"
down_revision: str | None = "0007_llm_calls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ghost_outcomes",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        # Why the trade never executed: 'vetoed' | 'declined' | 'expired'
        sa.Column("reason", sa.String(16), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("entry_price", sa.Numeric(14, 4), nullable=False),
        # 'proposal_limit' (limitPrice present) | 'proposal_notional' (notional/qty)
        sa.Column("entry_source", sa.String(20), nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False, server_default="5"),
        # {"1": close, "3": close, ...} — trading-day offsets → close price
        sa.Column("marks", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_price", sa.Numeric(14, 4), nullable=True),
        # direction * qty * (last_mark - entry); finalized at horizon
        sa.Column("ghost_pnl", sa.Numeric(14, 2), nullable=True),
        # 'pending' | 'partial' | 'final'
        sa.Column("status", sa.String(10), nullable=False, server_default="pending"),
        # 'alpaca' | 'synthetic'
        sa.Column("price_source", sa.String(16), nullable=False, server_default="synthetic"),
        sa.Column("first_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["agent_decisions.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("decision_id", name="uq_ghost_outcomes_decision_id"),
    )
    op.create_index("ix_ghost_outcomes_status", "ghost_outcomes", ["status"])
    op.create_index("ix_ghost_outcomes_reason", "ghost_outcomes", ["reason"])


def downgrade() -> None:
    op.drop_index("ix_ghost_outcomes_reason", table_name="ghost_outcomes")
    op.drop_index("ix_ghost_outcomes_status", table_name="ghost_outcomes")
    op.drop_table("ghost_outcomes")
