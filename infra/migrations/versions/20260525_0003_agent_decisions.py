"""agent_decisions ALTER (Reflection columns) + strategy_confidence

Revision ID: 0003_agent_decisions
Revises: 0002_positions_snapshot
Create Date: 2026-05-25

PLAN.md §5.1 Reflection Agent — closes the council loop. Migration shape:

  - ALTER agent_decisions to add Selector/score/fill columns used by the
    Reflection loop. The table itself already exists from 0001; the
    Phase 1 column set was designed around a single LLM "judge" that the
    Phase 2 Selector/Drafter split replaces. Old columns (bull_case,
    bear_case, judge_verdict, judge_rationale) are left in place — the
    runtime simply stops populating them and a later cleanup migration
    can drop them once we're sure no tooling reads them.

  - CREATE strategy_confidence — per-strategy priors the Reflection Agent
    maintains. Seeded at confidence=0.5 for the five PLAN-locked ids.

Postgres impls of the agent-side DecisionLog + StrategyConfidenceStore
protocols are deferred (Mock is the live default while the in-memory
council is the only caller). The schema lands now so Phase 3 auth tables
stack cleanly on top.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_agent_decisions"
down_revision: str | None = "0002_positions_snapshot"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── ALTER agent_decisions — add Reflection-loop columns ──────────
    # NOTE: the table is created in 0001. Here we only add what the
    # Reflection loop needs that wasn't in the original Phase 1 shape.
    op.add_column(
        "agent_decisions",
        sa.Column("selected_strategy", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "agent_decisions",
        sa.Column(
            "selector_confidence",
            sa.Numeric(precision=4, scale=3),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "agent_decisions",
        sa.Column(
            "selector_rationale",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "agent_decisions",
        sa.Column("technical_score", sa.Numeric(precision=5, scale=2), nullable=True),
    )
    op.add_column(
        "agent_decisions",
        sa.Column("fundamental_score", sa.Numeric(precision=5, scale=2), nullable=True),
    )
    op.add_column(
        "agent_decisions",
        sa.Column("macro_score", sa.Numeric(precision=5, scale=2), nullable=True),
    )
    op.add_column(
        "agent_decisions",
        sa.Column("fill_qty", sa.Integer(), nullable=True),
    )
    op.add_column(
        "agent_decisions",
        sa.Column(
            "fill_avg_price",
            sa.Numeric(precision=14, scale=4),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_decisions",
        sa.Column(
            "realized_pnl",
            sa.Numeric(precision=14, scale=2),
            nullable=True,
        ),
    )
    op.add_column(
        "agent_decisions",
        sa.Column(
            "reviewed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Partial index covering the Reflection Agent's hot query:
    # "rows ready to review" = realized_pnl set + reviewed_at unset.
    op.create_index(
        "ix_agent_decisions_pending_reflection",
        "agent_decisions",
        ["triggered_at"],
        postgresql_where=sa.text("reviewed_at IS NULL AND realized_pnl IS NOT NULL"),
    )

    # ── CREATE strategy_confidence ───────────────────────────────────
    op.create_table(
        "strategy_confidence",
        sa.Column("strategy_id", sa.String(length=64), nullable=False),
        sa.Column(
            "confidence",
            sa.Numeric(precision=4, scale=3),
            nullable=False,
            server_default="0.5",
        ),
        sa.Column("wins", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("losses", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_reflection_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("strategy_id", name="pk_strategy_confidence"),
    )
    # Seed one row per PLAN-locked strategy at 0.5. Reflection nudges
    # this; selector reads it. Unknown strategies discovered later get
    # inserted by the InMemory / Postgres stores on first ``get``.
    op.execute(
        """
        INSERT INTO strategy_confidence (strategy_id, confidence)
        VALUES
          ('sma_crossover', 0.5),
          ('rsi_mean_reversion', 0.5),
          ('momentum', 0.5),
          ('breakout', 0.5),
          ('vol_regime_switch', 0.5)
        ON CONFLICT (strategy_id) DO NOTHING;
        """
    )


def downgrade() -> None:
    op.drop_table("strategy_confidence")
    op.drop_index(
        "ix_agent_decisions_pending_reflection",
        table_name="agent_decisions",
    )
    op.drop_column("agent_decisions", "reviewed_at")
    op.drop_column("agent_decisions", "realized_pnl")
    op.drop_column("agent_decisions", "fill_avg_price")
    op.drop_column("agent_decisions", "fill_qty")
    op.drop_column("agent_decisions", "macro_score")
    op.drop_column("agent_decisions", "fundamental_score")
    op.drop_column("agent_decisions", "technical_score")
    op.drop_column("agent_decisions", "selector_rationale")
    op.drop_column("agent_decisions", "selector_confidence")
    op.drop_column("agent_decisions", "selected_strategy")


# Quiet unused import in linters when only `postgresql` is referenced from
# the seed via raw SQL.
_ = postgresql
