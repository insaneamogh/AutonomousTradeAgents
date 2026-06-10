"""llm_calls — cost ledger for every Anthropic call

Revision ID: 0007_llm_calls
Revises: 0006_decision_review
Create Date: 2026-05-30

PLAN.md §9 cost-tracking story. Every call through ``trading_agents.llm.LLM``
writes one row here with token counts + a computed USD cost. The
``/api/v1/health/full`` endpoint sums YTD; future budget caps + per-user
spend dashboards read the same table.

Cost computation lives in app code so we can rev prices without a
migration. The schema just holds the durable numbers.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007_llm_calls"
down_revision: str | None = "0006_decision_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "llm_calls",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        # Most calls happen in the context of a council run; the FK lets
        # us join cost ↔ decision for a per-decision cost breakdown.
        # Reflection / out-of-band calls have NULL here.
        sa.Column("agent_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        # Per-user attribution. The cron user's calls roll up under
        # the fixture user id in Phase 4.
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model", sa.String(length=64), nullable=False),
        # Role of the call — 'router' | 'technical' | 'fundamental' |
        # 'macro' | 'selector' | 'drafter' | 'reflection' | 'unknown'.
        # Lets us slice cost by node when we want to optimize.
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("output_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_read_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cache_creation_tokens", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "cost_usd",
            sa.Numeric(precision=10, scale=6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("is_mock", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "called_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["agent_decision_id"],
            ["agent_decisions.id"],
            name="fk_llm_calls_agent_decision_id",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_llm_calls_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_llm_calls"),
    )
    op.create_index(
        "ix_llm_calls_called_at",
        "llm_calls",
        ["called_at"],
    )
    op.create_index(
        "ix_llm_calls_user_id_called_at",
        "llm_calls",
        ["user_id", "called_at"],
    )
    # Partial index for the YTD-spend hot path — real calls only.
    op.create_index(
        "ix_llm_calls_real",
        "llm_calls",
        ["called_at"],
        postgresql_where=sa.text("is_mock = false"),
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_real", table_name="llm_calls")
    op.drop_index("ix_llm_calls_user_id_called_at", table_name="llm_calls")
    op.drop_index("ix_llm_calls_called_at", table_name="llm_calls")
    op.drop_table("llm_calls")
