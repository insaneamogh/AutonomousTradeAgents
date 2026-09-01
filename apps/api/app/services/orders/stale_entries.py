"""Cancel working ENTRY orders whose thesis did not survive to the fill.

The gap this closes, asked plainly: *"is anything checking, before the
market opens, that a position still worth taking is still worth taking?"*
Until this module, no.

An approved equity entry is submitted with ``TimeInForce.GTC`` whenever it
carries a bracket (``executor.py``). GTC means the broker holds it across
sessions. So a proposal drafted on Monday's 16:00 close — from Monday's
bars, Monday's regime, Monday's analyst scores — could fill on Wednesday
at Wednesday's price, on a thesis nothing had re-examined in two days.
Nothing cancelled it, nothing re-scored it, and the fill looked in every
log exactly like a fresh decision.

(Options entries already use ``TimeInForce.DAY`` — see
``options/tools/trade.py`` — so they die at the close on their own and are
not what this is for. It still covers them if that ever changes.)

The rule is deliberately one line of judgement and no model:

    an ENTRY order that was submitted before the CURRENT session's open,
    and is still working with zero fills once that session has opened,
    is cancelled.

Why that boundary and not a fixed age:

  * "Before this session's open" is the point at which the price the
    proposal was sized against stopped being the price the order would
    get. A fixed "older than N hours" window drifts across weekends and
    holidays and would cancel a Friday-evening order on Monday morning
    for the wrong reason.
  * Waiting until the session has actually OPENED (rather than sweeping
    pre-market) is what keeps this from fighting the normal path: a
    market order queued at 08:00 for the 09:30 open fills within seconds
    of the bell and is never seen here. Only an order that met the
    session and still did not fill is stale.
  * Zero fills only. A partially-filled entry is a live position; cancelling
    its remainder is an exit decision and belongs to the exit ladder, not
    here.

Cancelling is the whole action. It never re-drafts and never places
anything: the symbol simply returns to the scanner, which will re-run the
council against today's data and produce a fresh proposal or a HOLD. That
is the point — a stale order is replaced by a new DECISION, not by a new
order.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import desc, select

from app.services.broker.broker_use import with_broker_client
from app.services.orders.order_sync import OPEN_ORDER_STATUSES
from engine.features.market_calendar import is_us_market_open, us_market_session_bounds

logger = logging.getLogger("api.stale_entries")

__all__ = ["sweep_stale_entry_orders_for_user"]


def _entry_side_of(decision: object) -> str:
    """The side this decision's ENTRY used. A short's entry is a SELL.

    Read off the proposal, never assumed to be "BUY" — the same reasoning
    ``order_sync._apply_decision_lifecycle`` documents. Getting this wrong
    would classify a short's opening SELL as an exit and leave the one
    order type this module exists to cancel untouched.
    """
    proposal = getattr(decision, "proposal", None) or {}
    return str(proposal.get("side", "BUY")).upper()


async def sweep_stale_entry_orders_for_user(
    *,
    user_id: str,
    session_factory: object,
    now: datetime | None = None,
) -> int:
    """Cancel this user's stale working entry orders. Returns how many.

    No-ops entirely while the market is closed: the boundary this tests is
    "did the order survive an open", which cannot be answered before the
    open happens. That also means this costs one cheap query and nothing
    else on the ~85% of fleet ticks that fall outside the session.
    """
    from engine.db.models import AgentDecision, Order

    at = (now or datetime.now(UTC)).astimezone(UTC)
    if not is_us_market_open(at):
        return 0
    bounds = us_market_session_bounds(at.date())
    if bounds is None:
        return 0
    session_open_utc, _ = bounds

    uid = uuid.UUID(user_id)
    canceled = 0

    async with (
        with_broker_client(user_id, broker="alpaca") as (broker, _conn),
        session_factory() as session,  # type: ignore[operator]
    ):
        stmt = (
            select(Order, AgentDecision)
            .join(AgentDecision, AgentDecision.id == Order.agent_decision_id)
            .where(Order.user_id == uid)
            .where(Order.status.in_(OPEN_ORDER_STATUSES))
            .where(Order.broker_order_id.is_not(None))
            .where(Order.filled_qty == 0)
            .where(Order.submitted_at < session_open_utc)
            .order_by(desc(Order.submitted_at))
        )
        for order, decision in (await session.execute(stmt)).all():
            if order.side.upper() != _entry_side_of(decision):
                # An exit (a bracket child, a close, a stop) — never swept.
                # Cancelling one of those would strip the protection off a
                # live position, which is the exact opposite of the intent.
                continue
            try:
                await broker.cancel_order(order.broker_order_id)
            except Exception:
                logger.exception(
                    "stale_entries: cancel failed for %s (%s) — leaving it working",
                    order.client_order_id, order.symbol,
                )
                continue

            order.status = "canceled"
            order.canceled_at = at
            order.rejected_reason = (
                "stale_entry_thesis: drafted before this session's open and "
                "unfilled once it opened; re-deriving instead of executing it"
            )
            canceled += 1
            logger.info(
                "stale_entries: canceled %s %s x%d — decision %s predates the "
                "%s session open and never filled. The symbol goes back to the "
                "scanner for a fresh council pass.",
                order.side, order.symbol, order.qty, decision.id, at.date(),
            )

        if canceled:
            await session.commit()

    return canceled
