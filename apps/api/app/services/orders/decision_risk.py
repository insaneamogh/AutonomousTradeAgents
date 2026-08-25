"""Council-inputs read path — feeds the execution-time risk re-run.

The executor re-runs ``engine.risk.evaluate`` at the moment of order
placement, against the SAME inputs the council used. None of those inputs
(council confidence, specialist scores, the intraday flags) survive on the
``ApprovalProposalDto`` the mobile app round-trips, so this module reads
them back off the originating ``agent_decisions`` row (``load_decision_risk_row``)
and derives the one flag that doesn't have a column of its own
(``had_same_day_entry``, for the PDT day-trade check).

``resolve_decision_uuid`` is a smaller, standalone lookup used by the
execution-claim compare-and-swap (see ``execution_claim.py``) to tell "no
decision row at all" apart from "another approval holds the claim."
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core.ids import to_uuid as _to_uuid
from engine.env import env_flag

# US equities trade on the NY session — "same day" for PDT scoring is a NY
# calendar day, not a UTC one (a 20:00 ET fill is already tomorrow in UTC).
_NY = ZoneInfo("America/New_York")


async def resolve_decision_uuid(proposal_id: str) -> uuid.UUID | None:
    """agent_decisions row UUID for a proposal DTO id (``proposal->>'id'``)."""
    if not env_flag("USE_POSTGRES"):
        return None
    from sqlalchemy import select

    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        stmt = (
            select(AgentDecision.id)
            .where(AgentDecision.proposal["id"].astext == proposal_id)
            .limit(1)
        )
        return (await session.execute(stmt)).scalar_one_or_none()


@dataclass(frozen=True)
class DecisionRiskRow:
    """The council's own numbers, read back at execution time.

    The executor re-runs ``engine.risk.evaluate`` at the moment of order
    placement. To run the SAME chain the council ran it needs the council's
    confidence and the specialist scores — neither of which survives on the
    ``ApprovalProposalDto`` — so we read them off the originating
    ``agent_decisions`` row.
    """

    decision_id: uuid.UUID
    proposal: dict[str, Any]
    council_confidence: float | None
    judge_confidence: float | None
    specialists: tuple[tuple[str, float, float], ...]
    """(name, score, confidence) triples — plain tuples so this module stays
    free of an ``engine.risk`` import."""


def _coerce_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _specialists_from_row(row: Any) -> tuple[tuple[str, float, float], ...]:
    """Rebuild the council's specialist scores from the decision row.

    Prefers the raw per-analyst JSONB (it carries the analyst's own
    confidence); falls back to the promoted ``*_score`` columns, which are
    the indexed copies the Reflection Agent reads.
    """
    out: list[tuple[str, float, float]] = []
    for name in ("technical", "fundamental", "macro"):
        raw = getattr(row, name, None)
        score: float | None = None
        confidence = 0.0
        if isinstance(raw, dict):
            score = _coerce_float(raw.get("score"))
            confidence = _coerce_float(raw.get("confidence")) or 0.0
        if score is None:
            score = _coerce_float(getattr(row, f"{name}_score", None))
        if score is not None:
            out.append((name, score, confidence))
    return tuple(out)


async def load_decision_risk_row(
    *, proposal_id: str, user_id: str
) -> DecisionRiskRow | None:
    """Council inputs for the execution-time risk re-run, or None.

    None means "no decision row" — Postgres inactive, a MockStore-era
    proposal, or another user's row. The caller degrades to the proposal
    DTO's own fields and logs that it did.
    """
    if not env_flag("USE_POSTGRES"):
        return None

    uid = _to_uuid(user_id)
    if uid is None:
        return None

    from sqlalchemy import select

    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        stmt = (
            select(AgentDecision)
            .where(
                AgentDecision.user_id == uid,
                AgentDecision.proposal["id"].astext == proposal_id,
            )
            .limit(1)
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None

        proposal = row.proposal if isinstance(row.proposal, dict) else {}
        return DecisionRiskRow(
            decision_id=row.id,
            proposal=proposal,
            council_confidence=_coerce_float(
                proposal.get("councilConfidence", proposal.get("confidence"))
            ),
            judge_confidence=_coerce_float(row.judge_confidence),
            specialists=_specialists_from_row(row),
        )


async def had_same_day_entry(*, user_id: str, symbol: str) -> bool:
    """True if this user already got a BUY filled in ``symbol`` today (NY).

    Closing such a position is a day trade under FINRA's PDT rule, which is
    the input ``pdt_block`` gates on. Returns False when Postgres is
    inactive — the caller treats that as "unknown", and the dev-mode
    warning about running blind already covers it.
    """
    if not env_flag("USE_POSTGRES"):
        return False

    uid = _to_uuid(user_id)
    if uid is None:
        return False

    from sqlalchemy import func, or_, select

    from engine.db.models import Order
    from engine.db.session import async_session_factory

    session_open = (
        datetime.now(_NY)
        .replace(hour=0, minute=0, second=0, microsecond=0)
        .astimezone(UTC)
    )

    factory = async_session_factory()
    async with factory() as session:
        stmt = (
            select(func.count())
            .select_from(Order)
            .where(
                Order.user_id == uid,
                Order.symbol == symbol,
                Order.side == "BUY",
                Order.filled_qty > 0,
                or_(
                    Order.filled_at >= session_open,
                    Order.submitted_at >= session_open,
                ),
            )
        )
        return bool((await session.execute(stmt)).scalar_one())
