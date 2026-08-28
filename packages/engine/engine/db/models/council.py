"""What the agent council produced, and what came of it.

    agent_decisions      One row per LangGraph council run — the audit
                         anchor. Every order traces back to a row here.
    strategy_confidence  Per-strategy priors the Selector reads and the
                         Reflection Agent updates EOD (mig 0003).
    decision_reviews     Phase 4 operator hand-grading (mig 0006).
    llm_calls            Cost ledger: every Anthropic call (mig 0007).
    ghost_outcomes       What non-executed picks would have done (mig 0008).

This is the LLM-facing half of the schema. It records proposals and
rationale; it never records an authorization to trade — that decision is
made by the deterministic risk engine and lands in ``trading``.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column

from engine.db.base import Base

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

    # Position lifecycle (migration 0009). Entries are always human-approved;
    # ``exit_mode`` records whether the user delegated the CLOSE to the
    # position manager ('agent') or kept it manual. ``close_reason`` is a
    # named identifier like risk veto rules: 'agent_target' | 'agent_stop' |
    # 'agent_time' | 'agent_signal' | 'user_manual' | 'external_broker'.
    exit_mode: Mapped[str] = mapped_column(
        String(10), nullable=False, default="agent", server_default="agent"
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Council nodes that ran on a parse-retry or neutral fallback (migration
    # 0010). Non-empty → degraded run; reflection/calibration exclude it.
    degraded_nodes: Mapped[list[str] | None] = mapped_column(ARRAY(Text), nullable=True)

    # The full deterministic reasoning surface (migration 0012): the
    # strategy-fit components, the sizing arithmetic, the risk rules that
    # PASSED, the scan trigger, and a feature snapshot. Separate from
    # ``proposal`` because that column is parsed back into a wire DTO —
    # see the migration for why this used to be silently dropped on
    # exactly the approved decisions a user wants explained.
    reasoning: Mapped[dict | None] = mapped_column(JSONB, nullable=True)


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
        Index("ix_llm_calls_council_run_id", "council_run_id"),
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
    # Correlates every LLM call in one council pass BEFORE agent_decisions.id
    # exists — run_council() generates this once per pass (before any LLM
    # call) and every node's complete_json() call carries it. Deliberately NO
    # ForeignKey (migration 0013): the FK on agent_decision_id is checked
    # per-statement, not deferrable, and this column must survive being
    # written while the matching agent_decisions row does not exist yet. The
    # runtime backfills agent_decision_id on every matching row once the
    # decision is recorded (CostLedger.backfill_decision_id).
    council_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
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


# ─────────────────────────────────────────────────────────────────────
# Ghost outcomes (migration 0008) — what non-executed picks would have done
# ─────────────────────────────────────────────────────────────────────


class GhostOutcome(Base):
    """Hypothetical outcome of a vetoed / declined / expired decision.

    Separate from ``agent_decisions`` so the audit anchor stays immutable;
    the daily evaluator (``trading_agents.jobs.ghost_eval``) appends a
    close-price mark per trading day until ``horizon_days`` elapse, then
    finalizes ``ghost_pnl``. Deterministic Python only.
    """

    __tablename__ = "ghost_outcomes"
    __table_args__ = (
        Index("ix_ghost_outcomes_status", "status"),
        Index("ix_ghost_outcomes_reason", "reason"),
        UniqueConstraint("decision_id", name="uq_ghost_outcomes_decision_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("agent_decisions.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(16), nullable=False)  # vetoed/declined/expired
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    qty: Mapped[int] = mapped_column(Integer, nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    entry_source: Mapped[str] = mapped_column(String(20), nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False, default=5, server_default="5")
    marks: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default="{}")
    last_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    ghost_pnl: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    status: Mapped[str] = mapped_column(
        String(10), nullable=False, default="pending", server_default="pending"
    )
    price_source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="synthetic", server_default="synthetic"
    )
    first_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_evaluated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
