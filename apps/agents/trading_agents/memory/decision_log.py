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
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Protocol, runtime_checkable

ALL_USERS: Final[str] = "__ALL_USERS__"
"""Sentinel for the ``user_id`` argument of the cross-user read methods.

Passing it returns rows for EVERY tenant. It exists for scheduled jobs
only — the reflection pass, the ghost-P&L marker, the daily cron — which
grade the whole book and have no requesting user. It must NEVER be
derived from request data, and no HTTP handler may pass it: a router
always has an authenticated ``user.id`` to scope on.

The read methods take ``user_id`` as a REQUIRED argument (no ``None``
default) precisely so mypy forces every call site to state which of the
two it is.
"""


@dataclass
class DecisionEntry:
    """One council pass — pre-fill, pre-reflection.

    ``realized_pnl`` and ``reviewed_at`` start as None and get filled in by
    the executor (when the position closes) and the Reflection Agent
    respectively. ``raw_state`` is the JSON snapshot of CouncilState for
    replay — kept tight (no embeddings, no LLM raw text).
    """

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    """A real UUID string (not the old ``"dec-..."`` opaque id) so
    ``runtime.run_council`` can hand it the same ``council_run_id`` every LLM
    call in the pass was correlated under, and so ``PostgresDecisionLog.
    record()`` can reuse it as the row's real PK instead of discarding it."""
    user_id: str | None = None
    symbol: str = ""
    horizon: str = "short"
    triggered_at: datetime = field(default_factory=lambda: datetime.now(UTC))
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

    # Full council audit surface (WP0). The Postgres impl writes these to
    # dedicated columns so the trade-biography and theater features can
    # read per-analyst outputs without unpacking raw_state.
    technical: dict[str, Any] | None = None
    fundamental: dict[str, Any] | None = None
    macro: dict[str, Any] | None = None
    analyst_subset: list[str] | None = None
    bull_case: str | None = None
    bear_case: str | None = None
    risk_reason: str | None = None
    token_usage: dict[str, Any] | None = None
    completed_at: datetime | None = None
    # Names of council nodes that ran on a parse-retry or neutral fallback.
    # Non-empty → the run was degraded; reflection/calibration exclude it.
    # First-class so it survives even when proposal_dto replaces raw_state.
    degraded_nodes: list[str] | None = None
    reasoning: dict[str, Any] | None = None
    """The deterministic reasoning surface — strategy-fit components, the
    sizing arithmetic, the risk rules that passed, the scan trigger, and a
    feature snapshot. Its own field (and its own column) because
    ``raw_state`` is written to ``proposal`` only when there is NO approved
    proposal, which dropped it on exactly the rows worth explaining."""

    # The camelCase ApprovalProposalDto dict — present only when the risk
    # officer approved. Stored in the ``proposal`` JSONB column so the API's
    # ``list_pending`` can parse the row directly (single write path).
    proposal_dto: dict[str, Any] | None = None

    # Filled later — by executor + Reflection Agent.
    fill_qty: int | None = None
    fill_avg_price: float | None = None
    realized_pnl: float | None = None
    reviewed_at: datetime | None = None


def _visible_to(entry: DecisionEntry, user_id: str) -> bool:
    """Tenant predicate shared by the in-memory reads.

    A row with ``user_id is None`` (an unattributed CLI/smoke run) belongs
    to no tenant and is therefore invisible to every real user — only the
    ``ALL_USERS`` sentinel sees it.
    """
    if user_id == ALL_USERS:
        return True
    return entry.user_id == user_id


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
        user_id: str,
        since: timedelta = timedelta(hours=24),
        limit: int = 200,
    ) -> list[DecisionEntry]:
        """Closed-but-ungraded decisions for ``user_id``.

        Cross-user by nature, so ``user_id`` is required: the EOD
        reflection job passes ``ALL_USERS``; anything request-scoped
        passes the authenticated user's id.
        """
        ...

    async def mark_reviewed(self, decision_id: str) -> None: ...

    async def update_outcome(
        self,
        decision_id: str,
        *,
        fill_qty: int | None = None,
        fill_avg_price: float | None = None,
        realized_pnl: float | None = None,
    ) -> None: ...

    async def all_decisions(self, *, user_id: str) -> list[DecisionEntry]:
        """Every decision belonging to ``user_id``. Not for the hot path.

        ``user_id`` is required — this method used to return every
        tenant's rows and the API leaked them straight to whoever asked.
        Scheduled jobs that really do grade the whole book pass
        ``ALL_USERS`` explicitly.
        """
        ...

    async def has_decision_today(
        self, *, user_id: str | None, symbol: str, day_utc: str
    ) -> bool:
        """Indexed idempotency check for the daily cron: is there already a
        decision for (user, symbol) on ``day_utc`` (YYYY-MM-DD)? Replaces an
        all-history scan."""
        ...

    async def minutes_since_last_decision(
        self, *, user_id: str | None, symbol: str
    ) -> float | None:
        """Minutes since the most recent decision for (user, symbol), or
        ``None`` if there has never been one.

        Exists because once-per-DAY is the wrong cadence for options. A
        contract that had no setup at 14:00 can be a clean one at 15:30,
        and options are a timing instrument — the daily dedup that is
        right for a swing equity position silently caps options at one
        look per session. Callers gate on a cooldown instead.
        """
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
        user_id: str,
        since: timedelta = timedelta(hours=24),
        limit: int = 200,
    ) -> list[DecisionEntry]:
        cutoff = datetime.now(UTC) - since
        pending = [
            r for r in self._rows
            if _visible_to(r, user_id)
            and r.triggered_at >= cutoff
            and r.realized_pnl is not None
            and r.reviewed_at is None
        ]
        return pending[:limit]

    async def mark_reviewed(self, decision_id: str) -> None:
        for r in self._rows:
            if r.id == decision_id:
                r.reviewed_at = datetime.now(UTC)
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

    async def all_decisions(self, *, user_id: str) -> list[DecisionEntry]:
        return [r for r in self._rows if _visible_to(r, user_id)]

    async def has_decision_today(
        self, *, user_id: str | None, symbol: str, day_utc: str
    ) -> bool:
        for r in self._rows:
            if user_id is not None and r.user_id != user_id:
                continue
            if r.symbol != symbol:
                continue
            if r.triggered_at.strftime("%Y-%m-%d") == day_utc:
                return True
        return False

    async def minutes_since_last_decision(
        self, *, user_id: str | None, symbol: str
    ) -> float | None:
        from datetime import UTC as _UTC
        from datetime import datetime as _dt

        newest = None
        for r in self._rows:
            if user_id is not None and r.user_id != user_id:
                continue
            if r.symbol != symbol:
                continue
            if newest is None or r.triggered_at > newest:
                newest = r.triggered_at
        if newest is None:
            return None
        return (_dt.now(_UTC) - newest).total_seconds() / 60.0
