"""Browsable decision list — the read path behind ``GET /api/v1/decisions``.

Every council pass writes exactly one ``agent_decisions`` row, whether or
not it ever became a proposal. Before this endpoint existed, a row was
only reachable via its id if it had been approved (Positions) or was
still pending (approvals/pending) — a strategy-fit HOLD, which is most
council runs on any given sweep, had no id to look it up by anywhere in
the app once the sweep moved on. The Strategies screen could report "58
decisions in this window" and there was genuinely no way to see any of
them.
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.core.ids import to_uuid as _to_uuid
from app.schemas.decisions import DecisionSummaryDto
from engine.db.models import AgentDecision
from engine.db.session import async_session_factory

_VALID_ACTIONS = frozenset({"BUY", "SELL", "HOLD"})


async def list_decisions(
    *,
    user_id: str,
    symbol: str | None = None,
    action: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[DecisionSummaryDto], int]:
    """Newest-first page of the caller's decisions, plus the total count
    for that filter (so the client can render "page 2 of N" honestly).
    """
    uid = _to_uuid(user_id)
    if uid is None:
        return [], 0

    filters = [AgentDecision.user_id == uid]
    if symbol:
        filters.append(AgentDecision.symbol == symbol)
    if action and action in _VALID_ACTIONS:
        filters.append(AgentDecision.final_action == action)

    session_factory = async_session_factory()
    async with session_factory() as session:
        total = (
            await session.execute(select(func.count()).select_from(AgentDecision).where(*filters))
        ).scalar_one()

        stmt = (
            select(AgentDecision)
            .where(*filters)
            .order_by(AgentDecision.triggered_at.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await session.execute(stmt)).scalars().all()

    return (
        [
            DecisionSummaryDto(
                id=str(r.id),
                symbol=r.symbol,
                final_action=r.final_action,
                triggered_at=r.triggered_at,
                risk_approved=bool(r.risk_approved),
                risk_veto_rule=r.risk_veto_rule,
                selected_strategy=r.selected_strategy,
                selector_confidence=float(r.selector_confidence or 0),
                selector_rationale=r.selector_rationale or "",
                regime=r.regime,
                analyst_subset=list(r.analyst_subset) if r.analyst_subset else None,
                user_response=r.user_response,
            )
            for r in rows
        ],
        int(total),
    )
