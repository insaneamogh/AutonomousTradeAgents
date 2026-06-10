"""DecisionLog — one row per council pass; the Reflection Agent's input.

We capture more than the proposal — we also capture the analyst scores +
the regime + the Selector's pick + risk-officer verdict + (eventually) the
fill price + realized PnL when the position closes. The Reflection Agent
joins those to grade per-strategy outcomes.

Phase 0 ships the in-memory implementation. The Alembic migration carries
the matching Postgres schema (``agent_decisions``) so a future ``PostgresDecisionLog``
slots in without contract changes.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol, runtime_checkable


@dataclass
class DecisionEntry:
    """One council pass — pre-fill, pre-reflection.

    ``realized_pnl`` and ``reviewed_at`` start as None and get filled in by
    the executor (when the position closes) and the Reflection Agent
    respectively. ``raw_state`` is the JSON snapshot of CouncilState for
    replay — kept tight (no embeddings, no LLM raw text).
    """

    id: str = field(default_factory=lambda: f"dec-{uuid.uuid4().hex[:12]}")
    user_id: str | None = None
    symbol: str = ""
    horizon: str = "short"
    triggered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    regime: str | None = None
    selected_strategy: str | None = None
    selector_confidence: float = 0.0
    selector_rationale: str = ""
    final_action: str = "HOLD"
    proposal_id: str | None = None
    risk_approved: bool = False
    risk_veto_rule: str | None = None
    technical_score: float | None = None
    fundamental_score: float | None = None
    macro_score: float | None = None
    raw_state: dict[str, Any] = field(default_factory=dict)

    # Filled later — by executor + Reflection Agent.
    fill_qty: int | None = None
    fill_avg_price: float | None = None
    realized_pnl: float | None = None
    reviewed_at: datetime | None = None


@runtime_checkable
class DecisionLog(Protocol):
    """Backend contract for the agent decision log.

    Methods are async to keep the door open for a Postgres impl; the
    in-memory one ignores the await but still satisfies the type.
    """

    async def record(self, entry: DecisionEntry) -> DecisionEntry: ...

    async def list_pending_reflection(
        self,
        *,
        since: timedelta = timedelta(hours=24),
        limit: int = 200,
    ) -> list[DecisionEntry]: ...

    async def mark_reviewed(self, decision_id: str) -> None: ...

    async def update_outcome(
        self,
        decision_id: str,
        *,
        fill_qty: int | None = None,
        fill_avg_price: float | None = None,
        realized_pnl: float | None = None,
    ) -> None: ...

    async def all_decisions(self) -> list[DecisionEntry]:
        """Debug / testing only — full snapshot. Don't call in the hot path."""
        ...


class InMemoryDecisionLog:
    """Process-local DecisionLog. The default for tests + CLI.

    Not thread-safe across processes; we mount one per ``run_council`` run
    or one per CLI invocation. Real Postgres impl will use ``asyncpg`` +
    SQLAlchemy 2.0 async session per call.
    """

    def __init__(self) -> None:
        self._rows: list[DecisionEntry] = []

    async def record(self, entry: DecisionEntry) -> DecisionEntry:
        self._rows.append(entry)
        return entry

    async def list_pending_reflection(
        self,
        *,
        since: timedelta = timedelta(hours=24),
        limit: int = 200,
    ) -> list[DecisionEntry]:
        cutoff = datetime.now(timezone.utc) - since
        pending = [
            r for r in self._rows
            if r.triggered_at >= cutoff
            and r.realized_pnl is not None
            and r.reviewed_at is None
        ]
        return pending[:limit]

    async def mark_reviewed(self, decision_id: str) -> None:
        for r in self._rows:
            if r.id == decision_id:
                r.reviewed_at = datetime.now(timezone.utc)
                return

    async def update_outcome(
        self,
        decision_id: str,
        *,
        fill_qty: int | None = None,
        fill_avg_price: float | None = None,
        realized_pnl: float | None = None,
    ) -> None:
        for r in self._rows:
            if r.id == decision_id:
                if fill_qty is not None:
                    r.fill_qty = fill_qty
                if fill_avg_price is not None:
                    r.fill_avg_price = fill_avg_price
                if realized_pnl is not None:
                    r.realized_pnl = realized_pnl
                return

    async def all_decisions(self) -> list[DecisionEntry]:
        return list(self._rows)
