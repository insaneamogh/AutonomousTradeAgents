"""Execution claim — one winner per proposal.

A compare-and-swap mutex implemented against the
``agent_decisions.user_response`` column: ``NULL`` → ``'executing'`` claims
the exclusive right to place a proposal's order, so two concurrent
approvals can't both reach the broker. The winner converts the claim to a
final outcome via ``finalize_execution_claim``; a failure path releases it
via ``release_execution_claim`` so a retry can proceed.

MockStore / no-Postgres dev mode has no row to compare-and-swap against, so
an in-process lock (``_memory_claims``) backs the same guarantee there —
that backend is single-process by construction, so the in-process claim is
exactly as strong as the SQL one.
"""

from __future__ import annotations

import logging
import threading
from typing import Any

from app.core.ids import to_uuid as _to_uuid
from app.core.time import utc_now
from app.services.orders.decision_risk import resolve_decision_uuid
from engine.env import env_flag

logger = logging.getLogger("api.order_store")

EXECUTING = "executing"
"""Transient ``agent_decisions.user_response`` value held between the claim
and the order landing at the broker. Never surfaced to the user: the winner
overwrites it with 'approved', a failure path clears it back to NULL."""

# MockStore / no-Postgres dev mode has no row to compare-and-swap against.
# That backend is single-process by construction, so an in-process claim set
# is exactly as strong as the SQL one there.
_memory_claims: set[str] = set()
_memory_claims_lock = threading.Lock()


def _memory_key(user_id: str, proposal_id: str) -> str:
    return f"{user_id}:{proposal_id}"


def _claim_in_memory(user_id: str, proposal_id: str) -> bool:
    with _memory_claims_lock:
        key = _memory_key(user_id, proposal_id)
        if key in _memory_claims:
            return False
        _memory_claims.add(key)
        return True


def _release_in_memory(user_id: str, proposal_id: str) -> None:
    with _memory_claims_lock:
        _memory_claims.discard(_memory_key(user_id, proposal_id))


async def claim_decision_for_execution(*, user_id: str, proposal_id: str) -> bool:
    """Claim the exclusive right to place this proposal's order.

    Compare-and-swap: ``user_response IS NULL`` → ``'executing'``. Exactly
    one concurrent approval wins; the loser is refused instead of racing a
    second order past the broker's dedupe window and then fabricating an
    order id for a row it never wrote.

    Always paired with ``release_execution_claim`` (failure) or
    ``finalize_execution_claim`` (success).
    """
    if not _claim_in_memory(user_id, proposal_id):
        return False
    if not env_flag("USE_POSTGRES"):
        return True

    uid = _to_uuid(user_id)
    if uid is None:
        return True

    from sqlalchemy import update

    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        result = await session.execute(
            update(AgentDecision)
            .where(
                AgentDecision.user_id == uid,
                AgentDecision.proposal["id"].astext == proposal_id,
                AgentDecision.user_response.is_(None),
            )
            .values(user_response=EXECUTING)
        )
        await session.commit()

    if result.rowcount == 0:
        # Either another approval holds the claim, or there is no decision
        # row at all (MockStore-era proposal). Distinguish the two — a
        # missing row must not block execution forever.
        if await resolve_decision_uuid(proposal_id) is None:
            logger.info(
                "order_store: no agent_decisions row for proposal=%s — "
                "execution claim is in-process only",
                proposal_id,
            )
            return True
        _release_in_memory(user_id, proposal_id)
        return False

    return True


async def release_execution_claim(*, user_id: str, proposal_id: str) -> None:
    """Undo a claim so a retry can proceed. Best-effort; never raises."""
    _release_in_memory(user_id, proposal_id)
    if not env_flag("USE_POSTGRES"):
        return

    uid = _to_uuid(user_id)
    if uid is None:
        return

    try:
        from sqlalchemy import update

        from engine.db.models import AgentDecision
        from engine.db.session import async_session_factory

        factory = async_session_factory()
        async with factory() as session:
            await session.execute(
                update(AgentDecision)
                .where(
                    AgentDecision.user_id == uid,
                    AgentDecision.proposal["id"].astext == proposal_id,
                    AgentDecision.user_response == EXECUTING,
                )
                .values(user_response=None)
            )
            await session.commit()
    except Exception:
        # A stuck claim is recoverable; raising here is not — the caller is
        # already on a failure path.
        logger.exception(
            "order_store: could not release the execution claim for %s — "
            "the row stays 'executing' until the reconciler clears it",
            proposal_id,
        )


async def finalize_execution_claim(
    *, user_id: str, proposal_id: str, outcome: str, exit_mode: str | None
) -> bool:
    """Turn a held claim into the final decision. True if a row was updated.

    ``Store.decide`` can't do this: it only matches ``user_response IS
    NULL``, and the claim has already moved the row to 'executing'.
    """
    _release_in_memory(user_id, proposal_id)
    if not env_flag("USE_POSTGRES"):
        return False

    uid = _to_uuid(user_id)
    if uid is None:
        return False

    from sqlalchemy import update

    from engine.db.models import AgentDecision
    from engine.db.session import async_session_factory

    now = utc_now()
    values: dict[str, Any] = {
        "user_response": outcome,
        "user_responded_at": now,
        "completed_at": now,
    }
    if exit_mode in ("agent", "manual"):
        values["exit_mode"] = exit_mode

    factory = async_session_factory()
    async with factory() as session:
        result = await session.execute(
            update(AgentDecision)
            .where(
                AgentDecision.user_id == uid,
                AgentDecision.proposal["id"].astext == proposal_id,
                AgentDecision.user_response == EXECUTING,
            )
            .values(**values)
        )
        await session.commit()
    return bool(result.rowcount)


def reset_execution_claims_for_tests() -> None:
    with _memory_claims_lock:
        _memory_claims.clear()
