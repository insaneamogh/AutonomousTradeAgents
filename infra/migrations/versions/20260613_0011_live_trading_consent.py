"""per-connection live-trading consent

Revision ID: 0011_live_trading_consent
Revises: 0010_degraded_nodes
Create Date: 2026-06-13

Live (real-money) trading was gated by a single global LIVE_TRADING_ENABLED
env var — all-or-nothing across users. This adds a per-connection consent
flag so a real-money order requires BOTH the operator env AND the user's
explicit per-connection opt-in. Defaults False (safe): existing connections
stay paper-only until consent is granted.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011_live_trading_consent"
down_revision: str | None = "0010_degraded_nodes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "broker_connections",
        sa.Column(
            "live_trading_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("broker_connections", "live_trading_consent")
