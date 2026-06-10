"""Trade biography — one decision's life as an ordered event timeline.

Assembled from ``agent_decisions`` (the audit anchor) joined with
``orders`` / ``order_fills`` (when the executor wrote real rows) and
``decision_review`` (operator grade). The paper executor doesn't persist
orders rows yet — when fill columns exist on the decision but no orders
do, we synthesize a single ``filled`` event flagged ``source="paper"``.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from engine.db import async_session_factory
from engine.db.models import AgentDecision, DecisionReview, Order, OrderFill
from sqlalchemy import select


@dataclass
class TimelineEvent:
    kind: str
    at: datetime | None
    title: str
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)


@dataclass
class Biography:
    decision_id: str
    symbol: str
    side: str | None
    status: str
    events: list[TimelineEvent]


def _analyst_summaries(row: AgentDecision) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for role, payload, score in (
        ("technical", row.technical, row.technical_score),
        ("fundamental", row.fundamental, row.fundamental_score),
        ("macro", row.macro, row.macro_score),
    ):
        if payload is None and score is None:
            continue
        p = payload or {}
        out.append(
            {
                "role": role,
                "score": float(score) if score is not None else p.get("score"),
                "confidence": p.get("confidence"),
                "thesis": str(p.get("thesis", ""))[:200],
            }
        )
    return out


def _status_of(row: AgentDecision) -> str:
    if not row.risk_approved:
        return "vetoed"
    if row.user_response == "approved":
        return "closed" if row.realized_pnl is not None else "approved"
    if row.user_response in ("declined", "rejected"):
        return "declined"
    if row.user_response == "expired":
        return "expired"
    return "pending"


async def build_biography(decision_id: str) -> Biography | None:
    try:
        did = uuid.UUID(decision_id)
    except (ValueError, TypeError):
        return None

    session_factory = async_session_factory()
    async with session_factory() as session:
        row = await session.get(AgentDecision, did)
        if row is None:
            return None

        orders = (
            (
                await session.execute(
                    select(Order)
                    .where(Order.agent_decision_id == did)
                    .order_by(Order.submitted_at.asc())
                )
            )
            .scalars()
            .all()
        )
        fills: list[OrderFill] = []
        if orders:
            fills = (
                (
                    await session.execute(
                        select(OrderFill)
                        .where(OrderFill.order_id.in_([o.id for o in orders]))
                        .order_by(OrderFill.fill_time.asc())
                    )
                )
                .scalars()
                .all()
            )
        review = (
            await session.execute(
                select(DecisionReview)
                .where(DecisionReview.decision_id == did)
                .limit(1)
            )
        ).scalar_one_or_none()

    proposal = row.proposal or {}
    side = proposal.get("side") or (row.final_action if row.final_action in ("BUY", "SELL") else None)
    events: list[TimelineEvent] = []

    events.append(
        TimelineEvent(
            kind="proposed",
            at=row.triggered_at,
            title=f"Council proposed {side or row.final_action} {row.symbol}",
            detail=str(proposal.get("rationale", ""))[:300],
            data={
                "regime": row.regime,
                "selectedStrategy": row.selected_strategy,
                "selectorConfidence": float(row.selector_confidence)
                if row.selector_confidence is not None
                else None,
                "analysts": _analyst_summaries(row),
                "qty": proposal.get("qty"),
                "estimatedNotional": proposal.get("estimatedNotional"),
            },
        )
    )

    events.append(
        TimelineEvent(
            kind="risk_verdict",
            at=row.triggered_at,
            title="Risk engine cleared the trade" if row.risk_approved else "Risk engine vetoed",
            detail=(row.risk_reason or "")[:300],
            data={"approved": bool(row.risk_approved), "vetoRule": row.risk_veto_rule},
        )
    )

    if row.user_response is not None:
        verb = {
            "approved": "You approved the trade",
            "declined": "You passed on the trade",
            "rejected": "You passed on the trade",
            "expired": "The proposal expired unanswered",
        }.get(row.user_response, f"Decision: {row.user_response}")
        events.append(
            TimelineEvent(
                kind="user_decision",
                at=row.user_responded_at,
                title=verb,
                detail=(review.notes or "") if review is not None else "",
                data={"response": row.user_response},
            )
        )

    if orders:
        for o in orders:
            events.append(
                TimelineEvent(
                    kind="order_submitted",
                    at=o.submitted_at,
                    title=f"Order submitted · {o.side} {o.qty} {o.symbol}",
                    detail=f"{o.order_type} · {o.status}",
                    data={"orderId": str(o.id), "isPaper": bool(o.is_paper)},
                )
            )
        for f in fills:
            events.append(
                TimelineEvent(
                    kind="filled",
                    at=f.fill_time,
                    title=f"Filled {f.fill_qty} @ ${float(f.fill_price):.2f}",
                    data={"qty": f.fill_qty, "price": float(f.fill_price), "source": "broker"},
                )
            )
    elif row.fill_qty is not None and row.fill_avg_price is not None:
        # Paper-mode fallback: the executor recorded fills on the decision
        # row only — synthesize one fill event from those columns.
        events.append(
            TimelineEvent(
                kind="filled",
                at=row.user_responded_at or row.completed_at,
                title=f"Filled {row.fill_qty} @ ${float(row.fill_avg_price):.2f}",
                data={
                    "qty": row.fill_qty,
                    "price": float(row.fill_avg_price),
                    "source": "paper",
                },
            )
        )

    if row.realized_pnl is not None:
        pnl = float(row.realized_pnl)
        events.append(
            TimelineEvent(
                kind="closed",
                at=row.completed_at,
                title=f"Closed · {'+' if pnl >= 0 else '-'}${abs(pnl):,.2f}",
                data={"realizedPnl": pnl},
            )
        )

    if review is not None:
        events.append(
            TimelineEvent(
                kind="review_grade",
                at=review.reviewed_at,
                title={"good": "You graded it a good call", "bad": "You graded it a bad call"}.get(
                    review.grade, f"Grade: {review.grade}"
                ),
                detail=review.notes or "",
                data={"grade": review.grade},
            )
        )
    elif row.reviewed_at is not None:
        events.append(
            TimelineEvent(
                kind="reflection",
                at=row.reviewed_at,
                title="Reflection agent graded this trade",
                data={"strategy": row.selected_strategy},
            )
        )

    # Stable order: by timestamp where known, original order otherwise.
    events.sort(key=lambda e: (e.at is None, e.at))

    return Biography(
        decision_id=str(row.id),
        symbol=row.symbol,
        side=side,
        status=_status_of(row),
        events=events,
    )
