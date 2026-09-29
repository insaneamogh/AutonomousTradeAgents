"""The persisted paper book for a broker with no paper account (Kite).

    paper_accounts    one simulated cash balance per (user, broker)
    paper_positions   what that book holds, at average cost
    paper_orders      every simulated order and GTT, open or done

Zerodha has no sandbox. KitePaperBroker (app.services.orders.kite_paper)
trades this book against Kite's REAL quotes, so the whole order, exit and
ledger machinery runs unchanged in India without a real order. It lives
in Postgres because a book that resets on every redeploy cannot build the
forward track record the go-live criteria ask for (PLAN_PLATFORM Phase 7).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, ForeignKey, Integer, Numeric, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from engine.db.base import Base


class PaperAccount(Base):
    __tablename__ = "paper_accounts"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    broker: Mapped[str] = mapped_column(String(20), primary_key=True)
    cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    starting_cash: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    charges_paid: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False, default=0)
    """Indian costs charged on simulated fills (engine.backtester.costs_india):
    on a paper book they are the difference between a thesis and a trade."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PaperPosition(Base):
    __tablename__ = "paper_positions"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    broker: Mapped[str] = mapped_column(String(20), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(40), primary_key=True)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    avg_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)


class PaperOrder(Base):
    __tablename__ = "paper_orders"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    broker: Mapped[str] = mapped_column(String(20), nullable=False)
    broker_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    client_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    symbol: Mapped[str] = mapped_column(String(40), nullable=False)
    side: Mapped[str] = mapped_column(String(20), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    order_type: Mapped[str] = mapped_column(String(20), nullable=False)
    """MARKET / LIMIT / STOP / STOP_LIMIT, or GTT_OCO for a two-leg exit."""
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    gtt_legs: Mapped[list | None] = mapped_column(JSONB, nullable=True)
    """GTT_OCO only: [{"kind": "stop"|"limit", "trigger": x, "limit": y}]."""
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    filled_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    avg_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    filled_leg: Mapped[str | None] = mapped_column(String(10), nullable=True)
    trading_day: Mapped[date] = mapped_column(Date, nullable=False)
    """The IST session the order was placed in. A DAY order still open
    after that session's close is expired, as Kite does."""
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    filled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
