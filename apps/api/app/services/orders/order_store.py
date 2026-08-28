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

The two reads the executor makes against ``agent_decisions`` at execution
time — the council inputs the risk re-run needs, and the compare-and-swap
execution claim that makes a double-approve safe — used to live here too;
they now live in the sibling modules ``decision_risk.py`` and
``execution_claim.py`` respectively.
"""

from __future__ import annotations

import logging
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING

from app.core.ids import to_uuid as _to_uuid
from engine.env import env_flag

if TYPE_CHECKING:
    from app.schemas.approvals import ApprovalProposalDto
    from broker.types import Order as BrokerOrder

logger = logging.getLogger("api.order_store")


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
                is_option=proposal.is_option,
                multiplier=proposal.multiplier,
                option_action=proposal.option_action,
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
    is_option: bool = False,
    multiplier: int = 1,
    option_action: str | None = None,
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
                is_option=is_option,
                multiplier=multiplier,
                option_action=option_action,
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
