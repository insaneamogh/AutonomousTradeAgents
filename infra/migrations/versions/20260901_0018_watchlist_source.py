"""user_watchlist.source: distinguish auto-discovered rows from manual ones

Revision ID: 0018_watchlist_source
Revises: 0017_reflection_index_closed_at
Create Date: 2026-09-01

The scanner has only ever swept the user's own hand-curated
``user_watchlist`` rows (45 symbols as of this writing) — never Alpaca's
full ~13.4k-symbol tradable universe, which ``list_tradable_assets``/
``list_most_active_symbols`` (``packages/broker/broker/alpaca.py``) can
already fetch for free. This column is what lets a new universe-screener
job (``apps/agents/trading_agents/jobs/universe_refresh.py``) write a much
larger auto-discovered candidate set into the SAME table the scheduler
already reads, refreshed on its own schedule, WITHOUT ever touching or
deleting a user's own manually-added rows — the two are told apart by this
column, not by a separate table, so no downstream reader (the scheduler,
Settings' watchlist UI) needs a second query to see "everything I should
scan."

Defaults ``'manual'``: every existing row predates this feature and was,
by definition, added by a human via the Settings/API route.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0018_watchlist_source"
down_revision: str | None = "0017_reflection_index_closed_at"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "user_watchlist",
        sa.Column(
            "source",
            sa.String(10),
            nullable=False,
            server_default="manual",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_watchlist", "source")
