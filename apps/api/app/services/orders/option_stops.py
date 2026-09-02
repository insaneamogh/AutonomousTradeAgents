"""Resting broker-side protective stops for agent-managed option positions.

WHAT THIS ADDS, AND WHAT IT DOES NOT
------------------------------------
Every equity entry this system places carries a broker-side bracket, so
its stop survives our process being down. Options never had that: Alpaca
cannot bracket a single-leg option (``OrderClass`` allows only
simple/mleg for ``us_option``), so the options stop lived ONLY in
``position_manager``'s 30-second polling loop.

That is still the case for BRACKETS, and it is why this module places a
SEPARATE, standalone order rather than a bracket child. What changed is
the order type: per Alpaca's options-trading docs, an options order's
``type`` may be ``market``, ``limit``, ``stop`` or ``stop_limit``, with
``stop``/``stop_limit`` available for single-leg orders. So a resting
protective stop is placeable today, and the codebase's "structurally
impossible" comments simply predate that.

**This is not a fix for gap risk and must not be described as one.** A
resting stop elects on a print exactly as our polling loop does. When
``CME261016P00270000`` went -26% → -52% in one print, a broker stop at
-35% would have elected on that same print and filled no better. What
this covers is the set of failures polling cannot cover at all:

  * our process down, redeploying, or rate-limited;
  * ``_option_pl_pct_by_symbol`` returning ``{}`` on a broker read error,
    which holds every option un-stopped for that tick;
  * the 30 seconds between ticks;
  * overnight and weekends.

The software stop keeps running unchanged. Both are live at once, and
that is deliberate: the software one owns the time stop, the signal exit,
the expiry sweep and the ratchet; this one owns "we are not running".

TWO INTERACTIONS THAT WILL BITE IF FORGOTTEN
--------------------------------------------
1. ``_has_in_flight_close`` treats ANY open order tied to a decision as a
   close already in flight, unfiltered by side, and skips the position
   entirely. A permanently-resting stop is such an order, so without an
   exemption this feature would silently disable the time stop, signal
   exit, ratchet close and escalation for every option it protects —
   trading one real protection for four. Hence ``PROTECTIVE_STOP_PREFIX``
   and the exemption that reads it. Any new "is a close in flight?" query
   must exclude these rows too.

2. ``_close_position`` calls ``broker.cancel_open_orders(wire_symbol)``
   before placing its close, and ``wire_symbol`` is the OCC contract for
   an option. So an agent close cancels this stop automatically and no
   explicit teardown is needed on that path.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from app.services.broker.broker_use import with_broker_client
from app.services.orders.order_store import persist_linked_order_submit
from engine.options.protective_stop import (
    ProtectiveStopLevels,
    protective_stop_levels,
    should_replace,
)
from engine.risk.types import RiskCaps

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger("api.option_stops")

PROTECTIVE_STOP_PREFIX = "agent-protstop-"
"""Client-order-id prefix identifying a resting protective stop.

Load-bearing, not cosmetic: it is how ``_has_in_flight_close`` tells a
permanently-resting protective order apart from an actual close attempt.
A migration adding a real column would be cleaner; this avoids one while
staying a single grep-able constant.
"""


def protective_stop_client_order_id(decision_id: Any, *, seq: int = 0) -> str:
    """Deterministic id for this decision's resting stop.

    ``seq`` increments on each cancel-replace, because a broker rejects a
    duplicate ``client_order_id`` — reusing the id verbatim would make
    every re-tighten after the first fail with an opaque 422.
    """
    return f"{PROTECTIVE_STOP_PREFIX}{decision_id}-{seq}"


def _seq_of(client_order_id: str) -> int:
    """Replace counter encoded in the id's trailing ``-<n>``. 0 when absent
    or unparseable — a fresh id then collides with the existing one and the
    broker rejects it, which is the loud failure, not a silent bad stop."""
    try:
        return int(str(client_order_id).rsplit("-", 1)[1])
    except (IndexError, ValueError):
        return 0


def is_protective_stop_id(client_order_id: str | None) -> bool:
    return bool(client_order_id) and str(client_order_id).startswith(
        PROTECTIVE_STOP_PREFIX
    )


def _occ_of(decision: Any) -> str | None:
    proposal = getattr(decision, "proposal", None) or {}
    occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
    return str(occ).upper() if occ else None


def _is_option_decision(decision: Any) -> bool:
    proposal = getattr(decision, "proposal", None) or {}
    return bool(proposal.get("isOption", proposal.get("is_option", False)))


async def _resting_stop_row(session, decision_id) -> Any | None:
    """The most recent protective-stop order row for this decision, if any."""
    from app.services.orders.order_sync import IN_FLIGHT_STATUSES
    from engine.db.models import Order

    stmt = (
        select(Order)
        .where(Order.agent_decision_id == decision_id)
        .where(Order.client_order_id.like(f"{PROTECTIVE_STOP_PREFIX}%"))
        .where(Order.status.in_(IN_FLIGHT_STATUSES))
        .order_by(Order.submitted_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalars().first()


async def sync_protective_stop(
    session_factory: async_sessionmaker,
    *,
    user_id: str,
    decision: Any,
    caps: RiskCaps | None = None,
    trail_line_pct: float | None = None,
) -> str | None:
    """Ensure a resting stop-limit sits at the right level for ``decision``.

    Idempotent and monotone. Called on two occasions:

      * from ``order_sync`` when an option ENTRY fills — no trail exists
        yet, so the level is the fixed ``options_stop_loss_pct``;
      * from ``position_manager`` when the ratchet's peak ADVANCES, with
        the new ``trail_line_pct`` — gated on advancement rather than run
        every tick, matching the peak-write cadence (~10 broker calls per
        position per session, not ~800).

    Returns the broker order id when one was placed, else ``None``.
    Never raises into its caller: an entry that filled correctly must not
    be reported as failed because its protective stop could not be
    placed, and a position with no resting stop is still covered by the
    software stop. Failures are logged loudly and left for the next
    advancement tick to retry.
    """
    caps = caps or RiskCaps.from_env()
    if not caps.options_protective_stop_enabled:
        return None
    if not _is_option_decision(decision):
        return None
    if getattr(decision, "closed_at", None) is not None:
        return None
    if str(getattr(decision, "exit_mode", "") or "") != "agent":
        return None

    occ = _occ_of(decision)
    qty = int(getattr(decision, "fill_qty", 0) or 0)
    entry_premium = getattr(decision, "fill_avg_price", None)
    if occ is None or qty <= 0 or not entry_premium or float(entry_premium) <= 0:
        return None

    levels = protective_stop_levels(
        entry_premium=float(entry_premium),
        stop_loss_pct=caps.options_stop_loss_pct,
        slippage_pct=caps.options_stop_limit_slippage_pct,
        trail_line_pct=trail_line_pct,
    )
    if levels is None:
        return None

    async with session_factory() as session:
        existing = await _resting_stop_row(session, decision.id)
    current_basis: float | None = None
    seq = 0
    if existing is not None:
        # The resting level is read back off the order row's own
        # ``stop_price`` — broker truth, not a number we cached beside it
        # and could drift from. Converted to the same signed-P&L basis the
        # new level is expressed in, so the monotonicity check compares
        # like with like.
        resting_stop = getattr(existing, "stop_price", None)
        if resting_stop is not None:
            current_basis = (float(resting_stop) / float(entry_premium) - 1.0) * 100.0
        seq = _seq_of(getattr(existing, "client_order_id", "")) + 1
        if not should_replace(
            current_basis_pl_pct=current_basis,
            new_basis_pl_pct=levels.basis_pl_pct,
            min_step_pct=caps.options_protective_stop_min_step_pct,
        ):
            return None

    return await _place(
        session_factory,
        user_id=user_id,
        decision=decision,
        occ=occ,
        qty=qty,
        levels=levels,
        seq=seq,
        replace_existing=existing is not None,
    )


async def _place(
    session_factory: async_sessionmaker,
    *,
    user_id: str,
    decision: Any,
    occ: str,
    qty: int,
    levels: ProtectiveStopLevels,
    seq: int,
    replace_existing: bool,
) -> str | None:
    from broker.types import OrderRequest, OrderType, Side, TimeInForce

    client_order_id = protective_stop_client_order_id(decision.id, seq=seq)
    try:
        async with with_broker_client(user_id, broker="alpaca") as (broker, conn):
            if replace_existing:
                # Cancel-then-place, not place-then-cancel: two live stops
                # on one position can both elect and the second becomes a
                # naked short leg. The window with no stop is milliseconds
                # and the software stop covers it.
                try:
                    canceled = await broker.cancel_open_orders(occ)
                    logger.info(
                        "option_stops: canceled %d resting order(s) on %s before "
                        "re-tightening to %.1f%%",
                        canceled, occ, levels.basis_pl_pct,
                    )
                except Exception:
                    logger.exception(
                        "option_stops: could not cancel the resting stop on %s — "
                        "NOT placing a second one (two live stops on one position "
                        "is worse than a stale level)", occ,
                    )
                    return None

            order = await broker.place_order(
                OrderRequest(
                    symbol=occ,
                    side=Side.SELL_TO_CLOSE,
                    qty=qty,
                    order_type=OrderType.STOP_LIMIT,
                    stop_price=levels.stop_price,
                    limit_price=levels.limit_price,
                    # GTC so the stop outlives the session — the overnight
                    # and weekend gap is precisely what a broker-side stop
                    # exists to cover. Alpaca's options docs allow day|gtc.
                    time_in_force=TimeInForce.GTC,
                    client_order_id=client_order_id,
                )
            )
    except Exception:
        logger.exception(
            "option_stops: failed to place the protective stop for %s (%s) at "
            "$%.2f/$%.2f — the position keeps the software stop only",
            occ, decision.id, levels.stop_price, levels.limit_price,
        )
        return None

    try:
        await persist_linked_order_submit(
            user_id=user_id,
            broker_connection_id=str(conn.id),
            decision_id=decision.id if isinstance(decision.id, uuid.UUID)
            else uuid.UUID(str(decision.id)),
            client_order_id=client_order_id,
            symbol=occ,
            side="SELL",
            qty=qty,
            is_paper=conn.is_paper,
            order_type="STOP_LIMIT",
            is_option=True,
            multiplier=100,
            option_action="sell_to_close",
            stop_price=levels.stop_price,
            limit_price=levels.limit_price,
            time_in_force="GTC",
        )
    except Exception:
        logger.exception(
            "option_stops: placed broker order %s for %s but could not persist "
            "its row — order_sync's orphan adoption will reconcile it",
            getattr(order, "broker_order_id", "?"), occ,
        )

    logger.info(
        "option_stops: resting stop-limit on %s — %d @ stop $%.2f / limit $%.2f "
        "(%.1f%% P&L, %s)",
        occ, qty, levels.stop_price, levels.limit_price, levels.basis_pl_pct,
        "trail line" if levels.from_trail else "fixed stop",
    )
    return getattr(order, "broker_order_id", None)
