"""Order persistence — the audit link between a decision and the broker.

Write discipline (matches the ``orders`` table docstring in engine.db.models):

  1. ``persist_order_submit``  BEFORE the broker call. Inserts the row with
     status='pending' keyed on our ``client_order_id``. A retry of the same
     proposal hits ON CONFLICT DO NOTHING and returns the EXISTING row id, so
     the (executor retry → broker dedupe) path stays idempotent end to end.
  2. ``persist_order_result``  AFTER the broker acknowledges. Updates
     broker_order_id / status / fills, and pushes fill_qty + fill_avg_price
     up to the originating ``agent_decisions`` row.

Failure semantics — decided with the audit-first product rule in mind:

  - ``persist_order_submit`` raising must FAIL CLOSED in the caller: an
    order the DB doesn't know about is an audit-chain break, so the
    executor refuses to place it.
  - ``persist_order_result`` raising is logged and swallowed by the caller:
    the order already exists at the broker; the order-poller reconciles the
    row on its next pass.

Both functions return ``None`` / no-op when Postgres is inactive (MockStore
dev mode) — there is no orders table to write.

This module also owns the two reads the executor makes against
``agent_decisions`` at execution time: the council inputs the risk re-run
needs (``load_decision_risk_row``) and the compare-and-swap execution claim
that makes a double-approve safe (``claim_decision_for_execution``).
"""

from __future__ import annotations

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from app.core.ids import to_uuid as _to_uuid
from app.core.time import utc_now
from engine.env import env_flag

if TYPE_CHECKING:
    from app.schemas.approvals import ApprovalProposalDto
    from broker.types import Order as BrokerOrder

logger = logging.getLogger("api.order_store")

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


# ─────────────────────────────────────────────────────────────────────
# Execution claim — one winner per proposal
# ─────────────────────────────────────────────────────────────────────

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


async def persist_order_submit(
    *,
    user_id: str,
    broker_connection_id: str,
    proposal: ApprovalProposalDto,
    client_order_id: str,
    qty: int,
    is_paper: bool,
) -> uuid.UUID | None:
    """Insert the pending ``orders`` row. Returns the row id (existing row's
    id on an idempotent retry), or None when Postgres is inactive.

    Raises on DB failure — the executor treats that as fail-closed.
    """
    if not env_flag("USE_POSTGRES"):
        return None

    uid = _to_uuid(user_id)
    conn_id = _to_uuid(broker_connection_id)
    if uid is None or conn_id is None:
        raise ValueError(
            f"persist_order_submit: non-UUID user_id={user_id!r} "
            f"or broker_connection_id={broker_connection_id!r}"
        )

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from engine.db.models import AgentDecision, Order
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        decision_stmt = (
            select(AgentDecision.id)
            .where(AgentDecision.proposal["id"].astext == proposal.id)
            .limit(1)
        )
        decision_id = (await session.execute(decision_stmt)).scalar_one_or_none()
        if decision_id is None:
            # MockStore-era proposals (or in-memory pending queue) have no
            # decision row. The order is still recorded — just unlinked.
            logger.warning(
                "order_store: no agent_decisions row for proposal=%s — order will be unlinked",
                proposal.id,
            )

        stmt = (
            pg_insert(Order)
            .values(
                id=uuid.uuid4(),
                user_id=uid,
                broker_connection_id=conn_id,
                agent_decision_id=decision_id,
                client_order_id=client_order_id,
                symbol=proposal.symbol,
                side=proposal.side,
                qty=qty,
                order_type=proposal.order_type,
                limit_price=(
                    Decimal(str(proposal.limit_price))
                    if proposal.limit_price is not None
                    else None
                ),
                stop_price=(
                    Decimal(str(proposal.stop_loss))
                    if proposal.stop_loss is not None
                    else None
                ),
                status="pending",
                is_paper=is_paper,
            )
            .on_conflict_do_nothing(constraint="uq_orders_client_order_id")
        )
        await session.execute(stmt)
        await session.commit()

        row_id_stmt = select(Order.id).where(Order.client_order_id == client_order_id)
        row_id = (await session.execute(row_id_stmt)).scalar_one()

    logger.info(
        "order_store: pending order persisted id=%s client_order_id=%s decision=%s",
        row_id, client_order_id, decision_id,
    )
    return row_id


async def persist_linked_order_submit(
    *,
    user_id: str,
    broker_connection_id: str,
    decision_id: uuid.UUID,
    client_order_id: str,
    symbol: str,
    side: str,
    qty: int,
    is_paper: bool,
    order_type: str = "MARKET",
) -> uuid.UUID | None:
    """Pending ``orders`` row for an order that already knows its decision
    (the position manager's closes). Same idempotency + fail-closed
    semantics as ``persist_order_submit``."""
    if not env_flag("USE_POSTGRES"):
        return None

    uid = _to_uuid(user_id)
    conn_id = _to_uuid(broker_connection_id)
    if uid is None or conn_id is None:
        raise ValueError(
            f"persist_linked_order_submit: non-UUID user_id={user_id!r} "
            f"or broker_connection_id={broker_connection_id!r}"
        )

    from sqlalchemy import select
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from engine.db.models import Order
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        stmt = (
            pg_insert(Order)
            .values(
                id=uuid.uuid4(),
                user_id=uid,
                broker_connection_id=conn_id,
                agent_decision_id=decision_id,
                client_order_id=client_order_id,
                symbol=symbol,
                side=side,
                qty=qty,
                order_type=order_type,
                status="pending",
                is_paper=is_paper,
            )
            .on_conflict_do_nothing(constraint="uq_orders_client_order_id")
        )
        await session.execute(stmt)
        await session.commit()

        row_id_stmt = select(Order.id).where(Order.client_order_id == client_order_id)
        return (await session.execute(row_id_stmt)).scalar_one()


async def persist_order_result(
    *,
    order_row_id: uuid.UUID,
    broker_order: BrokerOrder,
) -> None:
    """Update the row with the broker's acknowledgement + propagate fills to
    the decision. Caller logs-and-continues on raise (order already placed;
    the order poller heals the row on its next pass)."""
    if not env_flag("USE_POSTGRES"):
        return

    from sqlalchemy import select, update

    from engine.db.models import AgentDecision, Order
    from engine.db.session import async_session_factory

    status = (
        broker_order.status.value
        if hasattr(broker_order.status, "value")
        else str(broker_order.status)
    )
    avg_price = (
        Decimal(str(broker_order.avg_fill_price))
        if broker_order.avg_fill_price is not None
        else None
    )

    factory = async_session_factory()
    async with factory() as session:
        await session.execute(
            update(Order)
            .where(Order.id == order_row_id)
            .values(
                broker_order_id=broker_order.broker_order_id,
                status=status,
                filled_qty=broker_order.filled_qty,
                avg_fill_price=avg_price,
                filled_at=broker_order.filled_at,
                raw_response=dict(broker_order.raw) if broker_order.raw else None,
            )
        )

        if broker_order.filled_qty and avg_price is not None:
            decision_id_stmt = select(Order.agent_decision_id).where(Order.id == order_row_id)
            decision_id = (await session.execute(decision_id_stmt)).scalar_one_or_none()
            if decision_id is not None:
                await session.execute(
                    update(AgentDecision)
                    .where(AgentDecision.id == decision_id)
                    .values(
                        fill_qty=broker_order.filled_qty,
                        fill_avg_price=avg_price,
                    )
                )

        await session.commit()

    logger.info(
        "order_store: order %s updated — status=%s filled=%d",
        order_row_id, status, broker_order.filled_qty,
    )
