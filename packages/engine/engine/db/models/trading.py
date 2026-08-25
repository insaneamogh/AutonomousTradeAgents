"""Orders, fills, and the deterministic risk state they feed.

    orders                  Every order submitted through a broker.
    order_fills             Per-fill detail — one order can have N fills.
    pdt_ledger              US Pattern Day Trader counter. Non-negotiable
                            for v1: four day-trades in five business days
                            on a sub-$25k account is a regulatory breach,
                            so the ``pdt_block`` risk rule reads this table.
    circuit_breaker_state   Per-user drawdown halt state.
    positions_snapshot      Reconciler-written portfolio cache (Phase 1).

These are the tables the risk engine reads before it will approve a trade.
Nothing here is LLM-written — the council never touches these rows.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from engine.db.base import Base

# ─────────────────────────────────────────────────────────────────────
# Orders + fills
# ─────────────────────────────────────────────────────────────────────


class Order(Base):
    """Every order we submitted through a broker.

    ``client_order_id`` is OUR idempotency key — generated before the broker
    is called. ``broker_order_id`` is populated after the broker accepts. If
    the broker call fails, the row still exists with status='pending' and
    can be retried by the same client_order_id (Alpaca de-dupes on it).
    """

    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("client_order_id", name="uq_orders_client_order_id"),
        Index("ix_orders_user_id", "user_id"),
        Index("ix_orders_symbol", "symbol"),
        Index("ix_orders_status", "status"),
        Index("ix_orders_submitted_at", "submitted_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    broker_connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("broker_connections.id", ondelete="RESTRICT"),
        nullable=False,
    )
    agent_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_decisions.id", ondelete="SET NULL"),
        nullable=True,
    )

    client_order_id: Mapped[str] = mapped_column(String(80), nullable=False)
    broker_order_id: Mapped[str | None] = mapped_column(String(80), nullable=True)

    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(15), nullable=False)
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    time_in_force: Mapped[str] = mapped_column(String(5), nullable=False, default="DAY")

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    raw_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class OrderFill(Base):
    """Individual fill events for a single order."""

    __tablename__ = "order_fills"
    __table_args__ = (Index("ix_order_fills_order_id", "order_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), nullable=False
    )
    fill_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    fill_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sec_fee: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    finra_taf: Mapped[Decimal | None] = mapped_column(Numeric(10, 4), nullable=True)
    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


# ─────────────────────────────────────────────────────────────────────
# PDT ledger — US Pattern Day Trader rule
# ─────────────────────────────────────────────────────────────────────


class PdtLedger(Base):
    """Tracks day-trade events per user. A day trade = open + close same
    NYSE business day in a margin account < $25K equity → max 3 per rolling
    5 business days. Risk engine reads this before allowing intraday closes.
    """

    __tablename__ = "pdt_ledger"
    __table_args__ = (
        Index("ix_pdt_ledger_user_id_trade_date", "user_id", "trade_date"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    open_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    close_order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("orders.id", ondelete="RESTRICT"), nullable=False
    )
    trade_date: Mapped[date] = mapped_column(Date, nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────
# Circuit breaker — per-user drawdown halt state
# ─────────────────────────────────────────────────────────────────────


class CircuitBreakerState(Base):
    """Per-user halt state for the drawdown circuit breaker.

    When ``status='halted'`` the orchestrator must not propose any new BUY
    orders. SELL/exit orders are still allowed (to flatten). The halt
    persists until the user explicitly acknowledges — there is NO automatic
    un-halt on a new trading day. This is intentional.
    """

    __tablename__ = "circuit_breaker_state"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="normal")
    # 'normal' | 'halted' | 'manual_override'

    halted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    halt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halt_threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    halt_observed_drawdown_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)
    halt_account_equity: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────
# Positions snapshot — reconciler-written portfolio cache
# ─────────────────────────────────────────────────────────────────────


class PositionsSnapshot(Base):
    """One row per reconciler tick. Newest row is the source of truth for
    ``RiskContext.account_equity`` / ``open_positions`` / ``daily_pnl_pct``.

    Phase 0/1: the reconciler writes from a ``MockBrokerPoller``. Phase 2
    swaps in ``AlpacaBrokerPoller`` which reads live Alpaca positions.

    Daily P&L is computed against the FIRST snapshot of the same UTC day.
    Phase 1 will swap UTC days for NY business days via ``pandas_market_calendars``.
    """

    __tablename__ = "positions_snapshot"
    __table_args__ = (
        Index("ix_positions_snapshot_user_id_captured_at", "user_id", "captured_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    source: Mapped[str] = mapped_column(String(20), nullable=False)  # 'alpaca' | 'mock'

    account_equity: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    cash: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    buying_power: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    # List of {symbol, qty, avg_entry_price, market_value, sector?}.
    open_positions: Mapped[list[dict]] = mapped_column(JSONB, nullable=False, default=list)

    daily_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    daily_pnl_pct: Mapped[Decimal | None] = mapped_column(Numeric(6, 3), nullable=True)

    raw: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
