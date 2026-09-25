"""Widen every symbol column from VARCHAR(20) to VARCHAR(40)

Revision ID: 0020_symbol_length
Revises: 0019_iv_history
Create Date: 2026-09-25

An NSE index option's Kite symbol does not fit in 20 characters:
"NFO:NIFTY26OCT25000CE" is 21, "NFO:BANKNIFTY26OCT55000CE" 25, a stock
option such as "NFO:RELIANCE26OCT3000CE" 23. The first NIFTY option close
failed to persist its orders row ("value too long for type character
varying(20)"), found by the e2e scenario for NSE index options.

40 fits every NSE/BSE/NFO/BFO/MCX symbol shape with room, and an OCC
contract (21). Widening a VARCHAR is a catalog-only change in Postgres:
no rewrite, no lock beyond the brief ACCESS EXCLUSIVE on each table.
The downgrade narrows back to 20 and fails if a longer symbol exists,
which is the correct refusal.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_symbol_length"
down_revision: str | None = "0019_iv_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("orders", "pdt_ledger", "user_watchlist", "agent_decisions", "iv_history")


def upgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "symbol", type_=sa.String(40), existing_type=sa.String(20))


def downgrade() -> None:
    for table in _TABLES:
        op.alter_column(table, "symbol", type_=sa.String(20), existing_type=sa.String(40))
