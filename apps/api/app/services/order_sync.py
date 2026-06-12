"""Per-user order + position sync against the broker. Runs every fleet tick.

Three responsibilities, in order:

  1. ORDER STATUS — every open ``orders`` row (submitted / accepted /
     partially_filled, with a broker_order_id) is re-read from the broker.
     Status, filled_qty, avg_fill_price converge to broker truth; a fill
     delta inserts an ``order_fills`` row.

  2. DECISION LIFECYCLE — a filled BUY heals the decision's entry columns
     (fill_qty / fill_avg_price). A fully-filled SELL linked to a decision
     closes it: ``closed_at`` + ``realized_pnl`` (long-only math:
     (exit - entry) * qty). If the entry and exit filled on the same UTC
     date, a ``pdt_ledger`` row is recorded (idempotent on close_order_id).

  3. EXTERNAL CLOSES — the user is always allowed to close positions
     directly in the Alpaca app. We detect it: an open agent position
     (decision approved + filled + not closed) whose symbol has VANISHED
     from the broker's positions, with no open SELL order of ours in
     flight, is marked ``close_reason='external_broker'``. Realized P&L is
     approximated from the last reconciler snapshot's mark for that symbol
     (the broker doesn't tell us the user's exact exit price) — when no
     mark exists we leave realized_pnl NULL rather than fabricate one.
     A push notification tells the user we noticed.

Everything is deterministic reads/writes. Per-user; called by the fleet
with errors isolated upstream.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import desc, select, update

from app.services.broker_use import with_broker_client

if TYPE_CHECKING:
    from broker.base import BrokerInterface
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

logger = logging.getLogger("api.order_sync")

# Order rows in these states still need broker polling.
OPEN_ORDER_STATUSES: tuple[str, ...] = (
    "pending",
    "submitted",
    "accepted",
    "partially_filled",
)

# Order rows in these states count as "an exit is already in flight" for
# the external-close detector.
IN_FLIGHT_STATUSES: tuple[str, ...] = OPEN_ORDER_STATUSES


async def sync_user_orders_and_positions(
    *,
    user_id: str,
    session_factory: async_sessionmaker,
) -> None:
    """One sync pass for one user. Opens the broker connection once."""
    uid = uuid.UUID(user_id)

    async with (
        with_broker_client(user_id, broker="alpaca") as (broker, _conn),
        session_factory() as session,
    ):
        await _sync_open_orders(session, uid, broker)
        await _detect_external_closes(session, uid, broker, user_id=user_id)
        await session.commit()


# ─────────────────────────────────────────────────────────────────────
# 1 + 2. Order status → decision lifecycle
# ─────────────────────────────────────────────────────────────────────


async def _sync_open_orders(
    session: AsyncSession, uid: uuid.UUID, broker: BrokerInterface
) -> None:
    from engine.db.models import Order

    stmt = (
        select(Order)
        .where(Order.user_id == uid)
        .where(Order.status.in_(OPEN_ORDER_STATUSES))
        .where(Order.broker_order_id.is_not(None))
    )
    rows = (await session.execute(stmt)).scalars().all()

    for row in rows:
        try:
            broker_order = await broker.get_order(row.broker_order_id)
        except Exception:
            logger.exception(
                "order_sync: get_order failed for %s (broker_order_id=%s)",
                row.id, row.broker_order_id,
            )
            continue

        new_status = (
            broker_order.status.value
            if hasattr(broker_order.status, "value")
            else str(broker_order.status)
        )
        fill_delta = int(broker_order.filled_qty) - int(row.filled_qty or 0)
        avg_price = (
            Decimal(str(broker_order.avg_fill_price))
            if broker_order.avg_fill_price is not None
            else None
        )

        if new_status == row.status and fill_delta <= 0:
            continue

        row.status = new_status
        row.filled_qty = int(broker_order.filled_qty)
        if avg_price is not None:
            row.avg_fill_price = avg_price
        if broker_order.filled_at is not None:
            row.filled_at = broker_order.filled_at

        if fill_delta > 0 and avg_price is not None:
            await _record_fill_delta(session, row, fill_delta, avg_price, broker_order)

        if new_status == "filled":
            await _apply_decision_lifecycle(session, row)

        logger.info(
            "order_sync: order %s → %s (filled %d/%d)",
            row.client_order_id, new_status, row.filled_qty, row.qty,
        )


async def _record_fill_delta(
    session: AsyncSession,
    order_row: object,
    fill_delta: int,
    avg_price: Decimal,
    broker_order: object,
) -> None:
    """One order_fills row per observed fill increment. The broker interface
    only exposes cumulative filled_qty + avg price, so the delta row carries
    the avg as its price — close enough for fee-free Phase 4 paper, and the
    cumulative columns on ``orders`` stay exact either way."""
    from engine.db.models import OrderFill

    session.add(
        OrderFill(
            id=uuid.uuid4(),
            order_id=order_row.id,
            fill_qty=fill_delta,
            fill_price=avg_price,
            fill_time=getattr(broker_order, "filled_at", None) or datetime.now(UTC),
            raw={"source": "order_sync", "cumulative_filled": int(broker_order.filled_qty)},
        )
    )


async def _apply_decision_lifecycle(session: AsyncSession, order_row: object) -> None:
    """Propagate a fully-filled order to its agent_decisions row."""
    from engine.db.models import AgentDecision

    if order_row.agent_decision_id is None:
        return

    decision = await session.get(AgentDecision, order_row.agent_decision_id)
    if decision is None:
        return

    if order_row.side == "BUY":
        decision.fill_qty = int(order_row.filled_qty)
        decision.fill_avg_price = order_row.avg_fill_price
        return

    # SELL → the decision's position is (fully or partially) exiting.
    # v1 closes the decision when the exit order is filled; partial manual
    # scaling is out of scope (long-only, one entry / one exit per decision).
    if decision.closed_at is None:
        entry = decision.fill_avg_price
        exit_price = order_row.avg_fill_price
        if entry is not None and exit_price is not None and decision.fill_qty:
            qty = min(int(order_row.filled_qty), int(decision.fill_qty))
            decision.realized_pnl = (
                (exit_price - entry) * Decimal(qty)
            ).quantize(Decimal("0.01"))
        decision.closed_at = order_row.filled_at or datetime.now(UTC)
        if decision.close_reason is None:
            decision.close_reason = "user_manual"
        await _maybe_record_pdt(session, decision, order_row)


async def _maybe_record_pdt(
    session: AsyncSession, decision: object, close_order: object
) -> None:
    """Same-UTC-day entry+exit → one pdt_ledger row. Idempotent on the
    close order. Phase 0 uses calendar days (same simplification as the
    PDT lookback); Phase 1.5 swaps to NYSE business days."""
    from engine.db.models import Order, PdtLedger

    entry_stmt = (
        select(Order)
        .where(Order.agent_decision_id == decision.id)
        .where(Order.side == "BUY")
        .where(Order.status == "filled")
        .order_by(Order.filled_at.asc())
        .limit(1)
    )
    entry_order = (await session.execute(entry_stmt)).scalar_one_or_none()
    if entry_order is None or entry_order.filled_at is None or close_order.filled_at is None:
        return
    if entry_order.filled_at.date() != close_order.filled_at.date():
        return

    existing_stmt = select(PdtLedger.id).where(PdtLedger.close_order_id == close_order.id)
    if (await session.execute(existing_stmt)).scalar_one_or_none() is not None:
        return

    session.add(
        PdtLedger(
            id=uuid.uuid4(),
            user_id=close_order.user_id,
            symbol=close_order.symbol,
            open_order_id=entry_order.id,
            close_order_id=close_order.id,
            trade_date=close_order.filled_at.date(),
            qty=min(int(entry_order.filled_qty), int(close_order.filled_qty)),
            realized_pnl=decision.realized_pnl,
            notes="recorded by order_sync (same-UTC-day round trip)",
        )
    )
    logger.warning(
        "order_sync: DAY TRADE recorded — %s %s (user=%s)",
        close_order.symbol, close_order.filled_at.date(), close_order.user_id,
    )


# ─────────────────────────────────────────────────────────────────────
# 3. External closes — the user traded at the broker directly
# ─────────────────────────────────────────────────────────────────────


async def _detect_external_closes(
    session: AsyncSession,
    uid: uuid.UUID,
    broker: BrokerInterface,
    *,
    user_id: str,
) -> None:
    from engine.db.models import AgentDecision, Order

    open_decisions_stmt = (
        select(AgentDecision)
        .where(AgentDecision.user_id == uid)
        .where(AgentDecision.user_response == "approved")
        .where(AgentDecision.fill_qty.is_not(None))
        .where(AgentDecision.closed_at.is_(None))
    )
    open_decisions = (await session.execute(open_decisions_stmt)).scalars().all()
    if not open_decisions:
        return

    try:
        broker_positions = await broker.list_positions()
    except Exception:
        logger.exception("order_sync: list_positions failed — skipping close detection")
        return
    held_qty = {p.symbol.upper(): int(p.qty) for p in broker_positions}

    for decision in open_decisions:
        symbol = decision.symbol.upper()
        if held_qty.get(symbol, 0) > 0:
            continue  # still held (v1 ignores partial external reductions)

        # An exit of OURS in flight explains the gap — not external.
        in_flight_stmt = (
            select(Order.id)
            .where(Order.agent_decision_id == decision.id)
            .where(Order.side == "SELL")
            .where(Order.status.in_(IN_FLIGHT_STATUSES))
            .limit(1)
        )
        if (await session.execute(in_flight_stmt)).scalar_one_or_none() is not None:
            continue

        approx_exit = await _last_snapshot_mark(session, uid, symbol)
        realized: Decimal | None = None
        if approx_exit is not None and decision.fill_avg_price is not None and decision.fill_qty:
            realized = (
                (approx_exit - decision.fill_avg_price) * Decimal(int(decision.fill_qty))
            ).quantize(Decimal("0.01"))

        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.id == decision.id)
            .values(
                closed_at=datetime.now(UTC),
                close_reason="external_broker",
                realized_pnl=realized,
            )
        )
        logger.info(
            "order_sync: %s closed EXTERNALLY at the broker (user=%s, approx_pnl=%s)",
            symbol, uid, realized,
        )
        _notify_external_close(user_id=user_id, symbol=symbol, qty=int(decision.fill_qty or 0))


async def _last_snapshot_mark(
    session: AsyncSession, uid: uuid.UUID, symbol: str
) -> Decimal | None:
    """Most recent snapshot price for a symbol — the best exit-price proxy
    we have for a close that happened outside our order flow."""
    from engine.db.models import PositionsSnapshot

    stmt = (
        select(PositionsSnapshot)
        .where(PositionsSnapshot.user_id == uid)
        .order_by(desc(PositionsSnapshot.captured_at))
        .limit(20)
    )
    snapshots = (await session.execute(stmt)).scalars().all()
    for snap in snapshots:
        for pos in snap.open_positions or []:
            if str(pos.get("symbol", "")).upper() != symbol:
                continue
            qty = int(pos.get("qty", 0) or 0)
            mv = float(pos.get("market_value", 0) or 0)
            if qty > 0 and mv > 0:
                return Decimal(str(round(mv / qty, 4)))
    return None


def _notify_external_close(*, user_id: str, symbol: str, qty: int) -> None:
    """Fire-and-forget push — lock-screen-safe, no IDs/PII (AGENTV1 rule)."""
    try:
        from app.services.notifications import schedule_position_event_notification

        schedule_position_event_notification(
            user_id=user_id,
            title="Position closed at broker",
            body=f"Your {qty} {symbol} closed directly at Alpaca — trade log updated.",
        )
    except Exception:
        logger.exception("order_sync: external-close notification failed")
