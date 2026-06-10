"""evaluate_breaker — flip ``circuit_breaker_state`` to halted when drawdown breaches threshold.

PLAN.md §12 + AGENTV1's Step 8 ("don't auto-unhalt"): once halted, the row
stays halted until the user explicitly acknowledges. This function only
TRIPS — it never un-trips.

Idempotent: calling repeatedly on the same already-halted user is a no-op.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from engine.db.models import CircuitBreakerState, PositionsSnapshot

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("engine.reconciler.breaker")


@dataclass(frozen=True)
class BreakerTransition:
    tripped: bool
    previous_status: str
    new_status: str
    reason: str | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def evaluate_breaker(
    session: "AsyncSession",
    *,
    user_id: uuid.UUID,
    snapshot: PositionsSnapshot,
    threshold_pct: float,
) -> BreakerTransition:
    """Read the current breaker state; if normal and drawdown ≤ threshold, trip it.

    ``threshold_pct`` is a negative number (e.g. ``-3.0`` for a 3% drawdown).
    """
    # Resolve current state. UPSERT keeps the row at most once per user.
    current = (
        await session.execute(
            select(CircuitBreakerState).where(CircuitBreakerState.user_id == user_id)
        )
    ).scalar_one_or_none()
    previous = current.status if current else "normal"

    if previous != "normal":
        # Already halted (or manually overridden) — never auto-unhalt.
        return BreakerTransition(
            tripped=False, previous_status=previous, new_status=previous
        )

    pnl_pct = float(snapshot.daily_pnl_pct or 0.0)
    if pnl_pct > threshold_pct:
        return BreakerTransition(tripped=False, previous_status="normal", new_status="normal")

    reason = (
        f"Daily drawdown {pnl_pct:.2f}% breached halt threshold {threshold_pct:.2f}% "
        f"(equity ${float(snapshot.account_equity):,.2f})"
    )
    halted_at = _now()

    stmt = (
        pg_insert(CircuitBreakerState)
        .values(
            user_id=user_id,
            status="halted",
            halted_at=halted_at,
            halt_reason=reason,
            halt_threshold_pct=Decimal(str(threshold_pct)),
            halt_observed_drawdown_pct=Decimal(str(round(pnl_pct, 3))),
            halt_account_equity=snapshot.account_equity,
        )
        .on_conflict_do_update(
            index_elements=["user_id"],
            set_={
                "status": "halted",
                "halted_at": halted_at,
                "halt_reason": reason,
                "halt_threshold_pct": Decimal(str(threshold_pct)),
                "halt_observed_drawdown_pct": Decimal(str(round(pnl_pct, 3))),
                "halt_account_equity": snapshot.account_equity,
            },
        )
    )
    await session.execute(stmt)
    await session.commit()

    logger.warning(
        "circuit breaker TRIPPED for user %s — %s", user_id, reason,
    )
    return BreakerTransition(
        tripped=True, previous_status="normal", new_status="halted", reason=reason,
    )
