"""Ghost P&L + veto-ledger aggregates.

Reads ``ghost_outcomes`` (joined to ``agent_decisions``) and reduces to
the two headline numbers — "the risk engine saved you $X" and "your
passes cost you $Y" — plus the per-rule veto scorecard.

Both builders take a REQUIRED ``user_id`` and filter on the indexed
``agent_decisions.user_id``. They previously aggregated the whole table,
so /ghost/summary and /risk/vetoes reported other tenants' blocked
notional and ghost P&L as the caller's own numbers.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from engine.db import async_session_factory
from engine.db.models import AgentDecision, GhostOutcome
from sqlalchemy import select
from sqlalchemy.sql.elements import ColumnElement
from trading_agents.memory.decision_log import ALL_USERS


class _NoSuchTenant(Exception):
    """``user_id`` wasn't a UUID, so it can't match any decision row."""


def _tenant_filters(user_id: str) -> list[ColumnElement[bool]]:
    """WHERE fragment scoping a query to ``user_id``.

    Empty only for the ``ALL_USERS`` sentinel. A malformed id raises —
    an unparseable tenant must never widen into "every row".
    """
    if user_id == ALL_USERS:
        return []
    try:
        return [AgentDecision.user_id == uuid.UUID(user_id)]
    except (ValueError, TypeError) as exc:
        raise _NoSuchTenant(user_id) from exc


@dataclass
class GhostBucket:
    count: int
    ghost_pnl: float
    pending_count: int


@dataclass
class GhostSummary:
    window_days: int
    as_of: datetime
    vetoed: GhostBucket
    declined: GhostBucket
    saved_usd: float
    missed_usd: float


@dataclass
class VetoRuleRow:
    rule: str
    count: int
    blocked_notional: float
    ghost_pnl: float | None
    prevented_loss_usd: float | None
    last_at: datetime | None


@dataclass
class VetoLedger:
    window_days: int
    total_vetoes: int
    total_blocked_notional: float
    rules: list[VetoRuleRow]


def _empty_summary(window_days: int) -> GhostSummary:
    """Zeroed summary — what an unknown tenant sees."""
    empty = GhostBucket(count=0, ghost_pnl=0.0, pending_count=0)
    return GhostSummary(
        window_days=window_days,
        as_of=datetime.now(UTC),
        vetoed=empty,
        declined=empty,
        saved_usd=0.0,
        missed_usd=0.0,
    )


async def build_ghost_summary(window_days: int = 30, *, user_id: str) -> GhostSummary:
    """Vetoed/declined ghost P&L for ``user_id`` over the window."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    try:
        tenant = _tenant_filters(user_id)
    except _NoSuchTenant:
        return _empty_summary(window_days)
    session_factory = async_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(GhostOutcome, AgentDecision.triggered_at)
                    .join(AgentDecision, AgentDecision.id == GhostOutcome.decision_id)
                    .where(AgentDecision.triggered_at >= cutoff, *tenant)
                )
            )
            .all()
        )

    def bucket(reasons: tuple[str, ...]) -> GhostBucket:
        subset = [g for g, _ in rows if g.reason in reasons]
        finals = [g for g in subset if g.status == "final" and g.ghost_pnl is not None]
        return GhostBucket(
            count=len(subset),
            ghost_pnl=round(sum(float(g.ghost_pnl) for g in finals), 2),
            pending_count=len(subset) - len(finals),
        )

    vetoed = bucket(("vetoed",))
    declined = bucket(("declined", "expired"))
    return GhostSummary(
        window_days=window_days,
        as_of=datetime.now(UTC),
        vetoed=vetoed,
        declined=declined,
        # Vetoed picks that WOULD have lost money = savings.
        saved_usd=round(max(0.0, -vetoed.ghost_pnl), 2),
        # Declined picks that WOULD have made money = missed upside.
        missed_usd=round(max(0.0, declined.ghost_pnl), 2),
    )


async def build_veto_ledger(window_days: int = 30, *, user_id: str) -> VetoLedger:
    """Per-rule veto scorecard for ``user_id`` over the window."""
    cutoff = datetime.now(UTC) - timedelta(days=window_days)
    try:
        tenant = _tenant_filters(user_id)
    except _NoSuchTenant:
        return VetoLedger(
            window_days=window_days,
            total_vetoes=0,
            total_blocked_notional=0.0,
            rules=[],
        )
    session_factory = async_session_factory()
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(AgentDecision, GhostOutcome)
                    .outerjoin(GhostOutcome, GhostOutcome.decision_id == AgentDecision.id)
                    .where(
                        AgentDecision.risk_approved.is_(False),
                        AgentDecision.triggered_at >= cutoff,
                        *tenant,
                    )
                )
            )
            .all()
        )

    by_rule: dict[str, list[tuple[AgentDecision, GhostOutcome | None]]] = {}
    for dec, ghost in rows:
        rule = dec.risk_veto_rule or "unnamed_rule"
        by_rule.setdefault(rule, []).append((dec, ghost))

    out: list[VetoRuleRow] = []
    total_notional = 0.0
    for rule, pairs in by_rule.items():
        notional = 0.0
        ghost_finals: list[float] = []
        last_at: datetime | None = None
        for dec, ghost in pairs:
            p = dec.proposal or {}
            n = p.get("estimatedNotional")
            if isinstance(n, (int, float)):
                notional += float(n)
            if ghost is not None and ghost.status == "final" and ghost.ghost_pnl is not None:
                ghost_finals.append(float(ghost.ghost_pnl))
            if last_at is None or (dec.triggered_at and dec.triggered_at > last_at):
                last_at = dec.triggered_at
        ghost_pnl = round(sum(ghost_finals), 2) if ghost_finals else None
        out.append(
            VetoRuleRow(
                rule=rule,
                count=len(pairs),
                blocked_notional=round(notional, 2),
                ghost_pnl=ghost_pnl,
                prevented_loss_usd=round(max(0.0, -ghost_pnl), 2) if ghost_pnl is not None else None,
                last_at=last_at,
            )
        )
        total_notional += notional

    out.sort(key=lambda r: r.count, reverse=True)
    return VetoLedger(
        window_days=window_days,
        total_vetoes=sum(r.count for r in out),
        total_blocked_notional=round(total_notional, 2),
        rules=out,
    )
