"""council_run_id on llm_calls — correlate cost rows before the decision exists

Revision ID: 0014_llm_calls_run_id
Revises: 0013_options_orders
Create Date: 2026-08-28

Renumbered from 0013 to 0014 at merge time — it was built in parallel with
migration 0013_options_orders (the options-trading foundation), both
against the same 0012 base, and 0013_options_orders landed on main first.

Every ``llm_calls`` row was writing with ``agent_decision_id`` and ``user_id``
unconditionally NULL — there was no way to answer "which LLM calls produced
decision X" or "how much did user Y's trading cost in LLM spend".

The obvious fix — write the real ``agent_decisions.id`` at LLM-call time —
does not work: ``run_council()`` awaits the full graph (router, analysts,
drafter) to completion before ``decision_log.record()`` ever runs, and
``PostgresDecisionLog.record()`` assigns that row's id at insert time. So the
true decision id genuinely does not exist yet when the ledger rows for that
same pass are written. ``llm_calls.agent_decision_id`` carries a live
``ForeignKey("agent_decisions.id", ...)`` with no ``deferrable=True``, so
Postgres checks it per-statement — writing a not-yet-existent id into it would
raise ``ForeignKeyViolation`` on every insert for that pass, turning today's
100%-present-but-unattributed rows into 100%-silently-dropped ones (the write
path is a best-effort try/except): worse, not better.

``council_run_id`` is generated once per council pass, before any LLM call,
and carried on every ``llm_calls`` row written during that pass. It has
DELIBERATELY NO foreign key — it must survive being written while the
matching ``agent_decisions`` row does not exist yet. Once the decision is
recorded, the runtime backfills ``agent_decision_id`` on every row sharing
this run's ``council_run_id`` in one UPDATE (``WHERE council_run_id = :id AND
agent_decision_id IS NULL``).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014_llm_calls_run_id"
down_revision: str | None = "0013_options_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "llm_calls",
        sa.Column("council_run_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_index(
        "ix_llm_calls_council_run_id",
        "llm_calls",
        ["council_run_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_llm_calls_council_run_id", table_name="llm_calls")
    op.drop_column("llm_calls", "council_run_id")
