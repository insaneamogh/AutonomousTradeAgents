"""decision_review — operator hand-grading

Revision ID: 0006_decision_review
Revises: 0005_device_tokens
Create Date: 2026-05-30

PLAN.md §11 Phase 4 calls for hand-graded month-1 review: the founder
goes through the agent's completed trades + grades each one good/bad/
skip. The agreement between operator grade + Reflection's confidence
delta is the signal that tells us whether Reflection is calibrated.

One row per (decision, operator). UQ (decision_id, operator_user_id)
makes the upsert path idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_decision_review"
down_revision: str | None = "0005_device_tokens"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "decision_review",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operator_user_id", postgresql.UUID(as_uuid=True), nullable=False),
        # 'good' | 'bad' | 'skip' — checked at the app layer + via this varchar.
        sa.Column("grade", sa.String(length=8), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["decision_id"],
            ["agent_decisions.id"],
            name="fk_decision_review_decision_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["operator_user_id"],
            ["users.id"],
            name="fk_decision_review_operator_user_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_decision_review"),
        # Idempotent upsert key: a single operator gets a single grade per decision.
        sa.UniqueConstraint(
            "decision_id",
            "operator_user_id",
            name="uq_decision_review_decision_operator",
        ),
    )
    op.create_index(
        "ix_decision_review_decision_id",
        "decision_review",
        ["decision_id"],
    )
    op.create_index(
        "ix_decision_review_operator_user_id",
        "decision_review",
        ["operator_user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_decision_review_operator_user_id", table_name="decision_review")
    op.drop_index("ix_decision_review_decision_id", table_name="decision_review")
    op.drop_table("decision_review")
