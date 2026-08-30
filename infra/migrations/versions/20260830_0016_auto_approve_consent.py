"""per-connection auto-approve consent

Revision ID: 0016_auto_approve_consent
Revises: 0015_snapshot_options_level
Create Date: 2026-08-30

Autonomous order placement (``auto_approve_for_user`` — see
``docs/PLAN_AUTO_APPROVE.md``) is gated by the operator's global
``AUTO_APPROVE_ENABLED`` env var — all-or-nothing across users. This adds a
per-connection consent flag, mirroring migration 0011's
``live_trading_consent`` exactly, so an auto-approved entry requires BOTH
the operator env AND the account owner's own in-app opt-in. Defaults False
(safe): existing connections stay human-approval-only until consent is
granted from the app.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016_auto_approve_consent"
down_revision: str | None = "0015_snapshot_options_level"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "broker_connections",
        sa.Column(
            "auto_approve_consent",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("broker_connections", "auto_approve_consent")
