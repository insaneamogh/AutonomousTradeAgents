"""degraded_nodes on agent_decisions

Revision ID: 0010_degraded_nodes
Revises: 0009_position_lifecycle
Create Date: 2026-06-13

Persists which council nodes ran on a parse-retry or neutral fallback. The
flag already reached Langfuse + in-memory CouncilState, but dropped before
the DB row — so reflection/calibration couldn't exclude degraded runs.
Non-empty array → the decision is degraded.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010_degraded_nodes"
down_revision: str | None = "0009_position_lifecycle"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "agent_decisions",
        sa.Column("degraded_nodes", sa.ARRAY(sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("agent_decisions", "degraded_nodes")
