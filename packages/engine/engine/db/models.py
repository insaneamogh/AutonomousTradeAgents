"""SQLAlchemy models — Phase 0 / 1 schema.

Tables:
    users                   App users.
    broker_connections      Per-user encrypted broker session (Alpaca only in v1).
    agent_decisions         One row per LangGraph council run — the audit anchor.
    orders                  Every order we submitted through a broker.
    order_fills             Per-fill detail (a single order can have N fills).
    pdt_ledger              US Pattern Day Trader counter — non-negotiable for v1.
    circuit_breaker_state   Per-user halt state for the drawdown circuit breaker.
    positions_snapshot      Reconciler-written portfolio snapshot (Phase 1).

NOT in this migration (deliberate):
    - strategies / strategy_versions   → Phase 1 when we hand-code the 5 references
    - feature_store                    → Phase 1 / 2
    - psychology_reports               → Phase 5
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
from sqlalchemy import false as text_false
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from engine.db.base import Base


# ─────────────────────────────────────────────────────────────────────
# Users
# ─────────────────────────────────────────────────────────────────────


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Phase 3 (migration 0004): which auth path the user came in through.
    # 'magic_link' | 'password' | 'oauth_alpaca' | 'dev_bypass'.
    auth_method: Mapped[str] = mapped_column(
        String(20), nullable=False, default="magic_link", server_default="magic_link"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


# ─────────────────────────────────────────────────────────────────────
# Broker connections
# ─────────────────────────────────────────────────────────────────────


class BrokerConnection(Base):
    """Encrypted broker credentials. One row per (user, broker, environment).

    Token columns store ciphertext. Encryption happens at the application
    boundary (``apps/api/app/core/crypto.py``) — never store plaintext.
    """

    __tablename__ = "broker_connections"
    __table_args__ = (
        UniqueConstraint("user_id", "broker", "is_paper", name="uq_broker_connections_user_broker_env"),
        Index("ix_broker_connections_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    broker: Mapped[str] = mapped_column(String(20), nullable=False)  # 'alpaca'
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    account_number: Mapped[str | None] = mapped_column(String(64), nullable=True)

    encrypted_access_token: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ─────────────────────────────────────────────────────────────────────
# Agent decisions
# ─────────────────────────────────────────────────────────────────────


class AgentDecision(Base):
    """One row per Orchestrator/LangGraph run. The audit-trail anchor.

    Captures the full council output so a regulator (or angry user) can ask
    'why did the agent buy NVDA on March 4' and we can answer with the bull
    case, bear case, judge rationale, the deterministic risk decision, and
    the user's response — all from a single primary key.
    """

    __tablename__ = "agent_decisions"
    __table_args__ = (
        Index("ix_agent_decisions_user_id", "user_id"),
        Index("ix_agent_decisions_symbol", "symbol"),
        Index("ix_agent_decisions_triggered_at", "triggered_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    symbol: Mapped[str] = mapped_column(String(20), nullable=False)
    horizon: Mapped[str] = mapped_column(String(10), nullable=False)

    # Router output
    regime: Mapped[str | None] = mapped_column(String(20), nullable=True)
    analyst_subset: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    # Specialist outputs (raw, for audit)
    technical: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    fundamental: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    macro: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Council debate
    bull_case: Mapped[str | None] = mapped_column(Text, nullable=True)
    bear_case: Mapped[str | None] = mapped_column(Text, nullable=True)
    judge_verdict: Mapped[str | None] = mapped_column(String(15), nullable=True)
    judge_confidence: Mapped[Decimal | None] = mapped_column(Numeric(4, 3), nullable=True)
    judge_rationale: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Proposal as drafted (pre-risk)
    proposal: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Risk Officer decision
    risk_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    risk_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    risk_veto_rule: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Approval gate
    approval_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="ask")
    user_response: Mapped[str | None] = mapped_column(String(20), nullable=True)  # approved / rejected / expired
    user_responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Outcome
    final_action: Mapped[str] = mapped_column(String(10), nullable=False)  # BUY / SELL / HOLD / VETOED

    # Cost + provenance
    model_versions: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    triggered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Phase 2 Reflection-loop columns (migration 0003). The Selector
    # picks a strategy id; the Drafter narrative is captured separately
    # via ``proposal`` JSONB; per-analyst scores promoted to columns for
    # index-able queries the Reflection Agent runs.
    selected_strategy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    selector_confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=0, server_default="0"
    )
    selector_rationale: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    technical_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    fundamental_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    macro_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    # Post-execution outcomes. Populated by the executor (fill_qty /
    # fill_avg_price) + the close handler (realized_pnl). Reflection
    # reads ``realized_pnl IS NOT NULL AND reviewed_at IS NULL``.
    fill_qty: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fill_avg_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    realized_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


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


# ─────────────────────────────────────────────────────────────────────
# Phase 3 auth — sessions + magic links (migration 0004)
# ─────────────────────────────────────────────────────────────────────


class UserSession(Base):
    """One row per active refresh-token. Each device gets its own session
    row; refresh rotation swaps ``refresh_token_hash`` and bumps
    ``last_seen_at``. Logout sets ``revoked_at`` — the table is the
    source of truth, NOT the JWT itself (so a stolen refresh can be
    revoked even when the JWT hasn't expired).
    """

    __tablename__ = "user_sessions"
    __table_args__ = (Index("ix_user_sessions_user_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # bcrypt/scrypt hash — never the raw token.
    refresh_token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    device_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    device_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MagicLinkToken(Base):
    """Single-use email login tokens. Hashed at rest (scrypt). ``used_at``
    locks the row after first verify so replays fail.
    """

    __tablename__ = "magic_link_tokens"
    __table_args__ = (Index("ix_magic_link_tokens_email", "email"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ─────────────────────────────────────────────────────────────────────
# Phase 3 notifications — device push tokens (migration 0005)
# ─────────────────────────────────────────────────────────────────────


class DeviceToken(Base):
    """Expo push tokens per (user, device). Idempotent UQ on
    (user_id, expo_push_token) — re-registering the same device hits
    the existing row.
    """

    __tablename__ = "device_tokens"
    __table_args__ = (
        UniqueConstraint("user_id", "expo_push_token", name="uq_device_tokens_user_token"),
        Index("ix_device_tokens_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    expo_push_token: Mapped[str] = mapped_column(String(255), nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # 'ios' | 'android' | 'web'
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ─────────────────────────────────────────────────────────────────────
# Phase 2 Reflection — per-strategy priors (migration 0003)
# ─────────────────────────────────────────────────────────────────────


class StrategyConfidence(Base):
    """Per-strategy priors the Reflection Agent maintains.

    Seeded at confidence=0.5 for the five PLAN-locked strategy ids by
    migration 0003. Reflection applies a clamped delta after grading
    completed trades; Selector reads these on every council pass.
    """

    __tablename__ = "strategy_confidence"

    strategy_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    confidence: Mapped[Decimal] = mapped_column(
        Numeric(4, 3), nullable=False, default=Decimal("0.5"), server_default="0.5"
    )
    wins: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    last_reflection_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")


# ─────────────────────────────────────────────────────────────────────
# Phase 4 month-1 review — operator hand-grading (migration 0006)
# ─────────────────────────────────────────────────────────────────────


class DecisionReview(Base):
    """One row per (decision, operator). PLAN.md §11 Phase 4 hand-grading.

    The agreement between this row's grade and the matching agent_decisions
    row's reflection-applied confidence_delta is the calibration signal
    for the Reflection Agent.
    """

    __tablename__ = "decision_review"
    __table_args__ = (
        UniqueConstraint(
            "decision_id",
            "operator_user_id",
            name="uq_decision_review_decision_operator",
        ),
        Index("ix_decision_review_decision_id", "decision_id"),
        Index("ix_decision_review_operator_user_id", "operator_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    operator_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # 'good' | 'bad' | 'skip'. Enum-checked at the app layer.
    grade: Mapped[str] = mapped_column(String(8), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


# ─────────────────────────────────────────────────────────────────────
# Phase 4 cost ledger — every Anthropic call (migration 0007)
# ─────────────────────────────────────────────────────────────────────


class LlmCall(Base):
    """One row per LLM call through ``trading_agents.llm.LLM``.

    Source of truth for cost telemetry. ``/api/v1/health/full`` sums
    ``cost_usd`` for the year; future budget caps + per-role
    optimization will slice by ``role`` + ``model``.
    """

    __tablename__ = "llm_calls"
    __table_args__ = (
        Index("ix_llm_calls_called_at", "called_at"),
        Index("ix_llm_calls_user_id_called_at", "user_id", "called_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    agent_decision_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_decisions.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    output_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_read_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cache_creation_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(10, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    is_mock: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text_false()
    )
    called_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
