"""Ghost P&L evaluator — what non-executed picks would have done.

Selects decisions that never became trades (risk-vetoed, user-declined,
expired) within the lookback window, derives an entry price from the
stored proposal, marks each against daily closes, and finalizes
``ghost_pnl`` once the proposal's horizon has elapsed.

Deterministic Python over close prices — no LLM in this path. Idempotent
per day: re-running upserts the same marks.

Invoked from ``daily_cron.py`` (after the council loop) or standalone:

    uv run --package agents python -m scripts.ghost_eval
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from engine.db import async_session_factory
from engine.db.models import AgentDecision, GhostOutcome
from engine.prices import get_price_provider
from sqlalchemy import or_, select

logger = logging.getLogger("agents.ghost_eval")

DEFAULT_HORIZON_DAYS = 5
# Look back horizon + buffer so weekend/holiday gaps still finalize.
LOOKBACK_BUFFER_DAYS = 7

_HORIZON_BY_PROPOSAL_HORIZON = {
    "intraday": 1,
    "short": 5,
    "mid": 10,
    "long": 20,
}


def _entry_price(proposal: dict[str, Any]) -> tuple[float, str] | None:
    """Entry reference: explicit limit, else notional/qty. None = skip."""
    limit = proposal.get("limitPrice")
    if isinstance(limit, (int, float)) and limit > 0:
        return float(limit), "proposal_limit"
    qty = proposal.get("qty")
    notional = proposal.get("estimatedNotional")
    if (
        isinstance(qty, (int, float))
        and qty
        and isinstance(notional, (int, float))
        and notional > 0
    ):
        return float(notional) / float(qty), "proposal_notional"
    return None


def _reason_of(row: AgentDecision) -> str | None:
    if not row.risk_approved:
        return "vetoed"
    if row.user_response in ("declined", "rejected"):
        return "declined"
    if row.user_response == "expired":
        return "expired"
    return None


def _ghost_pnl(side: str, qty: int, entry: float, mark: float) -> float:
    direction = 1.0 if side == "BUY" else -1.0
    return round(direction * qty * (mark - entry), 2)


def _trading_day_offset(start: date, day: date) -> int:
    """Count trading days (Mon-Fri) strictly after ``start`` up to ``day``."""
    if day <= start:
        return 0
    offset = 0
    d = start
    while d < day:
        d += timedelta(days=1)
        if d.weekday() < 5:
            offset += 1
    return offset


async def evaluate_ghosts(*, today: date | None = None) -> dict[str, int]:
    """One evaluator pass. Returns counters for logging/tests."""
    today = today or datetime.now(UTC).date()
    session_factory = async_session_factory()
    created = updated = finalized = skipped = 0

    async with session_factory() as session:
        cutoff = datetime.now(UTC) - timedelta(
            days=max(_HORIZON_BY_PROPOSAL_HORIZON.values()) + LOOKBACK_BUFFER_DAYS
        )
        stmt = (
            select(AgentDecision)
            .where(
                AgentDecision.triggered_at >= cutoff,
                AgentDecision.proposal.is_not(None),
                or_(
                    AgentDecision.risk_approved.is_(False),
                    AgentDecision.user_response.in_(["declined", "rejected", "expired"]),
                ),
            )
            .order_by(AgentDecision.triggered_at.asc())
        )
        decisions = (await session.execute(stmt)).scalars().all()

        for row in decisions:
            reason = _reason_of(row)
            proposal = row.proposal or {}
            entry = _entry_price(proposal)
            side = proposal.get("side")
            qty = proposal.get("qty")
            if reason is None or entry is None or side not in ("BUY", "SELL") or not qty:
                skipped += 1
                continue
            entry_price, entry_source = entry
            horizon = _HORIZON_BY_PROPOSAL_HORIZON.get(row.horizon, DEFAULT_HORIZON_DAYS)
            start_day = row.triggered_at.date()

            ghost = (
                await session.execute(
                    select(GhostOutcome).where(GhostOutcome.decision_id == row.id)
                )
            ).scalar_one_or_none()
            if ghost is None:
                ghost = GhostOutcome(
                    id=uuid.uuid4(),
                    decision_id=row.id,
                    reason=reason,
                    side=str(side),
                    qty=int(qty),
                    entry_price=Decimal(str(round(entry_price, 4))),
                    entry_source=entry_source,
                    horizon_days=horizon,
                    marks={},
                    status="pending",
                    first_evaluated_at=datetime.now(UTC),
                )
                session.add(ghost)
                created += 1
            elif ghost.status == "final":
                continue  # idempotent: nothing to do

            provider = get_price_provider(anchor_price=entry_price, anchor_day=start_day)
            closes = await provider.daily_closes(row.symbol, start_day, today)
            if not closes:
                skipped += 1
                continue

            marks: dict[str, float] = dict(ghost.marks or {})
            for c in closes:
                off = _trading_day_offset(start_day, c.day)
                if 1 <= off <= horizon:
                    marks[str(off)] = c.close

            if not marks:
                continue

            last_offset = max(int(k) for k in marks)
            last_price = marks[str(last_offset)]
            ghost.marks = marks
            ghost.last_price = Decimal(str(round(last_price, 4)))
            ghost.ghost_pnl = Decimal(
                str(_ghost_pnl(str(side), int(qty), entry_price, last_price))
            )
            ghost.price_source = provider.name
            ghost.last_evaluated_at = datetime.now(UTC)
            new_status = "final" if last_offset >= horizon else "partial"
            if new_status == "final":
                finalized += 1
            ghost.status = new_status
            updated += 1

        await session.commit()

    counters = {
        "created": created,
        "updated": updated,
        "finalized": finalized,
        "skipped": skipped,
    }
    logger.info("ghost_eval pass: %s", counters)
    return counters


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(evaluate_ghosts())
