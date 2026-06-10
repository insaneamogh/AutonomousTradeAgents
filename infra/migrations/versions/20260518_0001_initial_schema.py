"""initial schema — users, broker_connections, agent_decisions, orders, order_fills, pdt_ledger, circuit_breaker_state

Revision ID: 0001_initial_schema
Revises:
Create Date: 2026-05-18
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial_schema"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── users ────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=True),
        sa.Column("password_hash", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    # ── broker_connections ───────────────────────────────────────────
    op.create_table(
        "broker_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("broker", sa.String(length=20), nullable=False),
        sa.Column("is_paper", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("account_number", sa.String(length=64), nullable=True),
        sa.Column("encrypted_access_token", sa.Text(), nullable=False),
        sa.Column("encrypted_refresh_token", sa.Text(), nullable=True),
        sa.Column("access_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refresh_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_broker_connections_user_id_users", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_broker_connections"),
        sa.UniqueConstraint(
            "user_id", "broker", "is_paper",
            name="uq_broker_connections_user_broker_env",
        ),
    )
    op.create_index(
        "ix_broker_connections_user_id", "broker_connections", ["user_id"],
    )

    # ── agent_decisions ──────────────────────────────────────────────
    op.create_table(
        "agent_decisions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("horizon", sa.String(length=10), nullable=False),
        sa.Column("regime", sa.String(length=20), nullable=True),
        sa.Column("analyst_subset", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("technical", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fundamental", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("macro", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("bull_case", sa.Text(), nullable=True),
        sa.Column("bear_case", sa.Text(), nullable=True),
        sa.Column("judge_verdict", sa.String(length=15), nullable=True),
        sa.Column("judge_confidence", sa.Numeric(precision=4, scale=3), nullable=True),
        sa.Column("judge_rationale", sa.Text(), nullable=True),
        sa.Column("proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("risk_approved", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("risk_reason", sa.Text(), nullable=True),
        sa.Column("risk_veto_rule", sa.String(length=50), nullable=True),
        sa.Column("approval_mode", sa.String(length=10), nullable=False, server_default="ask"),
        sa.Column("user_response", sa.String(length=20), nullable=True),
        sa.Column("user_responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("final_action", sa.String(length=10), nullable=False),
        sa.Column("model_versions", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("token_usage", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("triggered_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_agent_decisions_user_id_users", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_decisions"),
    )
    op.create_index("ix_agent_decisions_user_id", "agent_decisions", ["user_id"])
    op.create_index("ix_agent_decisions_symbol", "agent_decisions", ["symbol"])
    op.create_index("ix_agent_decisions_triggered_at", "agent_decisions", ["triggered_at"])

    # ── orders ───────────────────────────────────────────────────────
    op.create_table(
        "orders",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("broker_connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("agent_decision_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("client_order_id", sa.String(length=80), nullable=False),
        sa.Column("broker_order_id", sa.String(length=80), nullable=True),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("side", sa.String(length=4), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("order_type", sa.String(length=15), nullable=False),
        sa.Column("limit_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("time_in_force", sa.String(length=5), nullable=False, server_default="DAY"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("filled_qty", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("avg_fill_price", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("is_paper", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("raw_response", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_orders_user_id_users", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["broker_connection_id"], ["broker_connections.id"],
            name="fk_orders_broker_connection_id_broker_connections",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["agent_decision_id"], ["agent_decisions.id"],
            name="fk_orders_agent_decision_id_agent_decisions",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_orders"),
        sa.UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
    )
    op.create_index("ix_orders_user_id", "orders", ["user_id"])
    op.create_index("ix_orders_symbol", "orders", ["symbol"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_submitted_at", "orders", ["submitted_at"])

    # ── order_fills ──────────────────────────────────────────────────
    op.create_table(
        "order_fills",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("fill_qty", sa.Integer(), nullable=False),
        sa.Column("fill_price", sa.Numeric(precision=14, scale=4), nullable=False),
        sa.Column("fill_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sec_fee", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("finra_taf", sa.Numeric(precision=10, scale=4), nullable=True),
        sa.Column("raw", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(
            ["order_id"], ["orders.id"],
            name="fk_order_fills_order_id_orders", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_order_fills"),
    )
    op.create_index("ix_order_fills_order_id", "order_fills", ["order_id"])

    # ── pdt_ledger ───────────────────────────────────────────────────
    op.create_table(
        "pdt_ledger",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=20), nullable=False),
        sa.Column("open_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("close_order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("trade_date", sa.Date(), nullable=False),
        sa.Column("qty", sa.Integer(), nullable=False),
        sa.Column("realized_pnl", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_pdt_ledger_user_id_users", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["open_order_id"], ["orders.id"],
            name="fk_pdt_ledger_open_order_id_orders", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["close_order_id"], ["orders.id"],
            name="fk_pdt_ledger_close_order_id_orders", ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_pdt_ledger"),
    )
    op.create_index(
        "ix_pdt_ledger_user_id_trade_date", "pdt_ledger", ["user_id", "trade_date"],
    )

    # ── circuit_breaker_state ────────────────────────────────────────
    op.create_table(
        "circuit_breaker_state",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="normal"),
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.Column("halt_threshold_pct", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("halt_observed_drawdown_pct", sa.Numeric(precision=6, scale=3), nullable=True),
        sa.Column("halt_account_equity", sa.Numeric(precision=14, scale=2), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"],
            name="fk_circuit_breaker_state_user_id_users", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["acknowledged_by_user_id"], ["users.id"],
            name="fk_circuit_breaker_state_acknowledged_by_user_id_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_circuit_breaker_state"),
    )


def downgrade() -> None:
    op.drop_table("circuit_breaker_state")
    op.drop_index("ix_pdt_ledger_user_id_trade_date", table_name="pdt_ledger")
    op.drop_table("pdt_ledger")
    op.drop_index("ix_order_fills_order_id", table_name="order_fills")
    op.drop_table("order_fills")
    op.drop_index("ix_orders_submitted_at", table_name="orders")
    op.drop_index("ix_orders_status", table_name="orders")
    op.drop_index("ix_orders_symbol", table_name="orders")
    op.drop_index("ix_orders_user_id", table_name="orders")
    op.drop_table("orders")
    op.drop_index("ix_agent_decisions_triggered_at", table_name="agent_decisions")
    op.drop_index("ix_agent_decisions_symbol", table_name="agent_decisions")
    op.drop_index("ix_agent_decisions_user_id", table_name="agent_decisions")
    op.drop_table("agent_decisions")
    op.drop_index("ix_broker_connections_user_id", table_name="broker_connections")
    op.drop_table("broker_connections")
    op.drop_table("users")
