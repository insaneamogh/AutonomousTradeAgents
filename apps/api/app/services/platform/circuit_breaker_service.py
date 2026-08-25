"""Circuit-breaker status + acknowledgement — backs the danger banner.

The reconciler flips ``circuit_breaker_state`` to ``halted`` on a daily
drawdown breach (deterministic, engine-side). This service is the read +
acknowledge surface the mobile app needs to render DESIGN.md's mandated
persistent danger banner and let the user clear it.

Acknowledge semantics (PLAN.md §12 — the halt does NOT auto-clear): the
user explicitly acknowledges the drawdown, which flips the row to
``manual_override`` and stamps who/when. Only then do new BUYs pass the
``drawdown_halt`` risk rule again (it blocks only while status=='halted').
Postgres-only — MockStore dev mode is never halted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from app.core.ids import to_uuid as _to_uuid
from app.core.time import utc_now
from engine.env import env_flag

logger = logging.getLogger("api.circuit_breaker")


@dataclass(frozen=True)
class CircuitBreakerStatus:
    halted: bool
    reason: str | None = None
    halted_at: datetime | None = None
    observed_drawdown_pct: float | None = None
    threshold_pct: float | None = None


_NORMAL = CircuitBreakerStatus(halted=False)


async def get_status(user_id: str) -> CircuitBreakerStatus:
    """Current breaker state for the user. Halted only while status=='halted'."""
    if not env_flag("USE_POSTGRES"):
        return _NORMAL
    uid = _to_uuid(user_id)
    if uid is None:
        return _NORMAL

    from engine.db.models import CircuitBreakerState
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        row = await session.get(CircuitBreakerState, uid)
    if row is None or row.status != "halted":
        return _NORMAL
    return CircuitBreakerStatus(
        halted=True,
        reason=row.halt_reason,
        halted_at=row.halted_at,
        observed_drawdown_pct=(
            float(row.halt_observed_drawdown_pct)
            if row.halt_observed_drawdown_pct is not None
            else None
        ),
        threshold_pct=(
            float(row.halt_threshold_pct) if row.halt_threshold_pct is not None else None
        ),
    )


async def acknowledge(user_id: str) -> CircuitBreakerStatus:
    """User acknowledges the drawdown halt → flip to manual_override so new
    BUYs are allowed again, stamping who/when for the audit trail. Idempotent
    (no-op if not currently halted)."""
    if not env_flag("USE_POSTGRES"):
        return _NORMAL
    uid = _to_uuid(user_id)
    if uid is None:
        return _NORMAL

    from sqlalchemy import update

    from engine.db.models import CircuitBreakerState
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        await session.execute(
            update(CircuitBreakerState)
            .where(CircuitBreakerState.user_id == uid, CircuitBreakerState.status == "halted")
            .values(
                status="manual_override",
                acknowledged_at=utc_now(),
                acknowledged_by_user_id=uid,
            )
        )
        await session.commit()
    logger.info("circuit_breaker: user %s acknowledged the drawdown halt", uid)
    return _NORMAL
