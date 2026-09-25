"""Broker-side exit for a Zerodha equity position: a GTT OCO (docs/PLAN_ZERODHA.md).

Kite has no bracket order for an API equity entry, so an agent-mode NSE
entry goes in as a plain order, and once it FILLS this places a two-leg
GTT: sell at the disclosed stop or at the target, whichever the price
reaches first. It survives our process being down, which is what the
approval card promised ("the broker holds the stop").

The GTT is recorded as an ``orders`` row (client_order_id
``gtt-oco-<decision>-<n>``, broker_order_id ``gtt:<trigger_id>``), so the
existing machinery does the rest:

  * order_sync polls it like any open order; ZerodhaBroker.get_order reads
    ``gtt:<id>`` as one exit order, FILLED when a leg's order fills;
  * that fill closes the decision through _apply_decision_lifecycle, as
    ``bracket_stop`` or ``bracket_target`` with the real price;
  * a resting GTT is not "a close in flight" (same exemption as the Alpaca
    option protective stop), so the time stop and signal exit keep working;
  * closing the position any other way deletes the GTT first
    (ZerodhaBroker.cancel_open_orders), so it cannot fire a sell later.

If placement fails, the operator is paged and position_manager's software
stop/target covers the position while the app runs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.services.broker.broker_use import with_broker_client
from app.services.orders.order_store import persist_linked_order_submit, persist_order_result
from engine.risk.markets import market_of

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger("api.exit_oco")

EXIT_OCO_PREFIX = "gtt-oco-"
"""client_order_id prefix of a resting GTT exit. Like option_stops'
PROTECTIVE_STOP_PREFIX, it is how "is a close in flight?" queries tell a
permanently-resting exit from an actual close attempt."""


def is_exit_oco_id(client_order_id: str | None) -> bool:
    return bool(client_order_id) and str(client_order_id).startswith(EXIT_OCO_PREFIX)


def needs_exit_oco(decision: Any) -> bool:
    """An open, agent-managed, filled NSE/BSE EQUITY decision that
    disclosed both a stop and a target."""
    proposal = getattr(decision, "proposal", None) or {}
    if bool(proposal.get("isOption", proposal.get("is_option", False))):
        return False
    if market_of(str(getattr(decision, "symbol", ""))) != "IN":
        return False
    if getattr(decision, "closed_at", None) is not None:
        return False
    if str(getattr(decision, "exit_mode", "") or "") != "agent":
        return False
    if not getattr(decision, "fill_qty", None):
        return False
    stop = proposal.get("stopLoss", proposal.get("stop_loss"))
    target = proposal.get("targetPrice", proposal.get("target_price"))
    return stop is not None and target is not None


async def _existing_rows(session_factory: async_sessionmaker, decision_id: Any) -> list[Any]:
    from engine.db.models import Order

    async with session_factory() as session:
        return list((await session.execute(
            select(Order)
            .where(Order.agent_decision_id == decision_id)
            .where(Order.client_order_id.like(f"{EXIT_OCO_PREFIX}%"))
        )).scalars().all())


async def sync_exit_oco(
    session_factory: async_sessionmaker, *, user_id: str, decision: Any
) -> str | None:
    """Place the GTT OCO for ``decision`` if it needs one and has none
    working. Idempotent. Returns the ``gtt:<id>`` placed, else None."""
    from app.services.orders.order_sync import OPEN_ORDER_STATUSES

    if not needs_exit_oco(decision):
        return None
    rows = await _existing_rows(session_factory, decision.id)
    if any(r.status in OPEN_ORDER_STATUSES for r in rows):
        return None

    proposal = decision.proposal or {}
    stop = float(proposal.get("stopLoss", proposal.get("stop_loss")))
    target = float(proposal.get("targetPrice", proposal.get("target_price")))
    entry_side = str(proposal.get("side", "BUY")).upper()
    qty = int(decision.fill_qty)
    symbol = decision.symbol.upper()
    client_order_id = f"{EXIT_OCO_PREFIX}{decision.id}-{len(rows)}"

    from broker.types import Order, OrderStatus, Side

    try:
        async with with_broker_client(user_id, broker="zerodha") as (broker, conn):
            place = getattr(broker, "place_exit_oco", None)
            if place is None:
                return None
            held = await broker.get_position(symbol)
            last = (
                abs(held.market_value) / abs(held.qty)
                if held is not None and held.qty else float(decision.fill_avg_price)
            )
            gtt_id = await place(
                symbol=symbol, qty=qty, entry_side=Side(entry_side), stop=stop,
                target=target, last_price=round(last, 2),
            )
    except Exception as exc:
        logger.exception("exit_oco: could not place the GTT exit for %s (%s)", symbol, decision.id)
        from app.services.notifications.ops_alerts import raise_ops_alert

        raise_ops_alert(
            "exit_oco_failed", user_id=str(user_id), key=f"{symbol}:{decision.id}",
            title="Broker-side exit not placed",
            body=(f"The GTT stop/target for {symbol} could not be placed "
                  f"({type(exc).__name__}). The position is protected only while the "
                  "app's own monitor is running."),
        )
        return None

    closing = "SELL" if entry_side == "BUY" else "BUY"
    try:
        row_id = await persist_linked_order_submit(
            user_id=user_id, broker_connection_id=str(conn.id),
            decision_id=decision.id if isinstance(decision.id, uuid.UUID)
            else uuid.UUID(str(decision.id)),
            client_order_id=client_order_id, symbol=symbol, side=closing, qty=qty,
            is_paper=conn.is_paper, order_type="GTT_OCO", stop_price=stop,
            limit_price=target, time_in_force="GTC",
        )
        if row_id is not None:
            await persist_order_result(order_row_id=row_id, broker_order=Order(
                broker_order_id=gtt_id, client_order_id=client_order_id, symbol=symbol,
                side=Side(closing), qty=qty, filled_qty=0, avg_fill_price=None,
                status=OrderStatus.ACCEPTED, submitted_at=datetime.now(UTC),
            ))
    except Exception:
        logger.exception(
            "exit_oco: GTT %s placed for %s but its row could not be persisted; it is live "
            "at the broker but untracked here", gtt_id, symbol,
        )
    logger.info("exit_oco: GTT %s on %s, %d @ stop %.2f / target %.2f",
                gtt_id, symbol, qty, stop, target)
    return gtt_id
