"""options_trading_level on positions_snapshot

Revision ID: 0015_snapshot_options_level
Revises: 0014_llm_calls_run_id
Create Date: 2026-08-28

Council-time risk checks (``risk_officer_node``, via
``PostgresRiskContextProvider``) read ``RiskContext`` from the reconciler's
cached ``positions_snapshot`` row, not a live broker call — that's a
separate path from the executor's own ``_build_risk_context``, which
already reads ``broker.get_options_trading_level()`` directly. Without
this column, ``RiskContext.options_trading_level`` is always None at
council time, and ``options_level_insufficient`` would veto every options
proposal unconditionally regardless of the real account's approval tier.

Nullable: existing snapshot rows (and every equity-only deployment with
``ALLOW_OPTIONS=0``) are unaffected — the options rule set is the only
reader, and it already treats "level unknown" the same as "insufficient",
which is the correct fail-closed direction for a column that predates
this migration.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0015_snapshot_options_level"
down_revision: str | None = "0014_llm_calls_run_id"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "positions_snapshot",
        sa.Column("options_trading_level", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("positions_snapshot", "options_trading_level")
