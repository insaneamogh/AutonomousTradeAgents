"""reasoning JSONB on agent_decisions

Revision ID: 0012_decision_reasoning
Revises: 0011_live_trading_consent
Create Date: 2026-08-26

The council already produced a full reasoning surface — which strategy fit
and by which NAMED precondition checks, the sizing arithmetic behind the
qty/stop/target, the deterministic risk rules that PASSED (not just the one
that vetoed), the scanner trigger that woke the run, and a snapshot of the
features every analyst was reading. None of it survived the write.

The reason it did not survive is subtle and worth recording: the decision
log packs its audit snapshot into ``raw_state``, but ``PostgresDecisionLog``
writes that into the ``proposal`` column ONLY when there is no approved
proposal to put there instead. So exactly the decisions a user wants
explained — the approved ones — were the decisions whose reasoning was
dropped on the floor.

A dedicated nullable JSONB column, rather than nesting it inside
``proposal``: that column is parsed straight back into an
``ApprovalProposalDto`` by the pending-approvals read path, and burying an
unrelated audit blob inside a wire DTO is how a schema stops meaning one
thing. Nullable so every historical row stays valid and reads as "this
predates the reasoning surface" rather than as an empty one.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012_decision_reasoning"
down_revision: str | None = "0011_live_trading_consent"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_decisions",
        sa.Column("reasoning", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_decisions", "reasoning")
