"""Per-user order + position sync against the broker. Runs every fleet tick.

Four responsibilities, in order:

  0. ORPHAN ADOPTION — an approved decision the broker holds a position
     for, but whose ``fill_qty`` is still NULL, is healed from the
     broker's own position (qty + avg_entry_price). Everything below
     converges fills through the ``orders`` table; this covers what
     reached the broker without ever getting an ``orders`` row. Load-
     bearing because the ratchet, the stop ladder and the DTE<=2 expiry
     sweep ALL filter on ``fill_qty IS NOT NULL`` — see
     ``_adopt_orphaned_fills``.

  1. ORDER STATUS — every open ``orders`` row (submitted / accepted /
     partially_filled, with a broker_order_id) is re-read from the broker.
     Status, filled_qty, avg_fill_price converge to broker truth; a fill
     delta inserts an ``order_fills`` row.

  2. DECISION LIFECYCLE — a fill matching the decision's OWN entry side
     (read off ``decision.proposal["side"]`` — "BUY" for a long, "SELL" for
     a short) heals the decision's entry columns (fill_qty / fill_avg_price).
     A fully-filled order on the OPPOSITE side closes it: ``closed_at`` +
     ``realized_pnl``, using (exit - entry) * qty for a long or
     (entry - exit) * qty for a short. If the entry and exit filled on the
     same UTC date, a ``pdt_ledger`` row is recorded (idempotent on
     close_order_id).

  3. EXTERNAL CLOSES — the user is always allowed to close positions
     directly in the Alpaca app. We detect it: an open agent position
     (decision approved + filled + not closed) whose symbol has VANISHED
     from the broker's positions, with no open SELL order of ours in
     flight, is marked ``close_reason='external_broker'``. Realized P&L is
     approximated from the last reconciler snapshot's mark for that symbol
     (the broker doesn't tell us the user's exact exit price) — when no
     mark exists we leave realized_pnl NULL rather than fabricate one.
     A push notification tells the user we noticed.

     An OPTION can also vanish with no order from anyone: it expired, was
     exercised, or was assigned. Those are read from the broker's account
     activities first and closed as ``option_expired`` (exit value 0,
     exact), ``option_exercised`` or ``option_assigned`` (exit value from
     the last mark). Exercise and assignment raise an ops alert, because
     they leave a stock position that no decision manages.

Everything is deterministic reads/writes. Per-user; called by the fleet
with errors isolated upstream.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import desc, or_, select, update

from app.services.broker.broker_use import with_broker_client
from engine.risk.markets import market_for_broker, market_of

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from broker.base import BrokerInterface
    from broker.types import AccountActivity

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

# How long an open row with no broker_order_id still counts as in flight.
# Every submit path writes the row, then submits, then stamps the broker's
# id, so a live submit is unacknowledged for a second or two. Past this, the
# row is an orphan: nothing polls it (_sync_open_orders needs the id), and
# as an "in-flight exit" it would hide a vanished position from the
# detector forever. That is how AAPL260918C00340000's stopped-out position
# read OPEN on 2026-09-04.
UNACKED_ORDER_GRACE = timedelta(minutes=10)


async def sync_user_orders_and_positions(
    *,
    user_id: str,
    session_factory: async_sessionmaker,
    broker: str = "alpaca",
) -> None:
    """One sync pass for one user's connection to ``broker``. Opens the
    broker connection once, and touches only the decisions and orders of
    that broker's market (engine.risk.markets): a Zerodha pass must never
    poll an Alpaca order, nor read an NSE position's absence at Alpaca as
    a close."""
    uid = uuid.UUID(user_id)
    market = market_for_broker(broker)

    async with (
        with_broker_client(user_id, broker=broker) as (client, _conn),
        session_factory() as session,
    ):
        # Adoption runs FIRST: it is what makes a broker-real position
        # visible to everything keyed on ``fill_qty IS NOT NULL``, and
        # ``_detect_external_closes`` below is one of those readers — an
        # unadopted orphan is invisible to it too.
        await _adopt_orphaned_fills(session, uid, client, market=market)
        await _sync_open_orders(session, uid, client, market=market)
        await _catch_up_filled_at_ack(session, uid, market=market)
        await _detect_external_closes(
            session, uid, client, user_id=user_id, market=market, source=broker
        )
        await session.commit()


# ─────────────────────────────────────────────────────────────────────
# 1 + 2. Order status → decision lifecycle
# ─────────────────────────────────────────────────────────────────────


async def _sync_open_orders(
    session: AsyncSession, uid: uuid.UUID, broker: BrokerInterface, *, market: str = "US"
) -> None:
    from engine.db.models import Order

    stmt = (
        select(Order)
        .where(Order.user_id == uid)
        .where(Order.status.in_(OPEN_ORDER_STATUSES))
        .where(Order.broker_order_id.is_not(None))
    )
    rows = [
        r for r in (await session.execute(stmt)).scalars().all() if market_of(r.symbol) == market
    ]

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

        # A GTT exit reports which leg filled; keep it on the row so the
        # close is labelled stop or target (_unstamped_close_reason).
        leg_type = str((getattr(broker_order, "raw", None) or {}).get("order_type", ""))
        if new_status == "filled" and _is_resting_exit(row.client_order_id) and leg_type:
            row.order_type = leg_type.upper()[:15]

        if new_status == "filled":
            await _apply_decision_lifecycle(session, row)

        logger.info(
            "order_sync: order %s → %s (filled %d/%d)",
            row.client_order_id, new_status, row.filled_qty, row.qty,
        )


async def _catch_up_filled_at_ack(
    session: AsyncSession, uid: uuid.UUID, *, market: str = "US"
) -> None:
    """Run the fill lifecycle for orders that were ALREADY filled when the
    broker acknowledged them.

    ``_sync_open_orders`` only polls rows still open, and a fill is only
    ever applied on the transition it observes. An order the broker
    reports filled in its submit response is stored filled at once
    (``persist_order_result``) and never transitions, so:

      * a CLOSE filled at acknowledgement never closed its decision; the
        next tick saw the position gone with nothing in flight and wrote
        ``external_broker`` over the agent's own reason;
      * an option ENTRY filled at acknowledgement never got its resting
        protective stop.

    Found by the e2e scenarios (apps/api/tests/e2e), whose simulated
    broker fills a marketable order at submit. The fix routes both through
    ``_apply_decision_lifecycle``, the one code path a polled fill takes.
    Scoped to OPEN decisions, and for entries to options that have no
    protective-stop row yet, so a settled book costs one small query.
    """
    from app.services.orders.exit_oco import EXIT_OCO_PREFIX, needs_exit_oco
    from app.services.orders.option_stops import PROTECTIVE_STOP_PREFIX
    from engine.db.models import AgentDecision, Order

    stmt = (
        select(Order, AgentDecision)
        .join(AgentDecision, AgentDecision.id == Order.agent_decision_id)
        .where(Order.user_id == uid)
        .where(Order.status == "filled")
        .where(AgentDecision.closed_at.is_(None))
        .where(Order.client_order_id.not_like(f"{PROTECTIVE_STOP_PREFIX}%"))
    )
    pairs = [
        (o, d) for o, d in (await session.execute(stmt)).all() if market_of(d.symbol) == market
    ]
    if not pairs:
        return
    # Decisions that already have a broker-side exit placed: an Alpaca
    # option's resting stop, or a Zerodha equity's GTT OCO.
    stopped = {
        d
        for (d,) in (
            await session.execute(
                select(Order.agent_decision_id)
                .where(Order.user_id == uid)
                .where(
                    Order.client_order_id.like(f"{PROTECTIVE_STOP_PREFIX}%")
                    | Order.client_order_id.like(f"{EXIT_OCO_PREFIX}%")
                )
            )
        ).all()
    }
    for order_row, decision in pairs:
        entry_side = str((decision.proposal or {}).get("side", "BUY"))
        is_exit = order_row.side != entry_side
        unstopped_option_entry = _is_option_decision(decision) and decision.id not in stopped
        unprotected_equity_entry = needs_exit_oco(decision) and decision.id not in stopped
        if is_exit or unstopped_option_entry or unprotected_equity_entry:
            await _apply_decision_lifecycle(session, order_row)


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
    """Propagate a fully-filled order to its agent_decisions row.

    "Entry" vs "exit" is decided by comparing the fill's side to the
    DECISION's OWN entry side (``decision.proposal["side"]``) — never by
    testing for a literal "BUY". A short's entry order IS a SELL: keying
    off a hardcoded "BUY" put a short's own opening fill into the exit
    branch, which stamped ``closed_at`` before the position was ever
    visible as open. ``decision.proposal`` is written once at council time
    and never mutated afterward, so it always reflects the ENTRY, even
    while reading THIS fill (which might be the exit).
    """
    from engine.db.models import AgentDecision

    if order_row.agent_decision_id is None:
        return

    decision = await session.get(AgentDecision, order_row.agent_decision_id)
    if decision is None:
        return

    entry_side = str((decision.proposal or {}).get("side", "BUY"))
    # Contract multiplier — 100 for a standard US equity option, 1 for
    # equities (the default, so every non-option row is untouched). Read
    # off the decision's OWN persisted proposal JSONB, not a DB column —
    # the ``orders.multiplier`` column exists for reporting/querying, not
    # this read path.
    multiplier = int((decision.proposal or {}).get("multiplier", 1) or 1)

    if order_row.side == entry_side:
        decision.fill_qty = int(order_row.filled_qty)
        decision.fill_avg_price = order_row.avg_fill_price
        # An option entry has just filled and we now know its real average
        # premium — the first moment a protective stop can be priced off
        # anything but a guess. Placed here rather than in the executor for
        # exactly that reason: at submit time there is no fill price, and
        # a sell-to-close on a position that has not filled is rejected.
        # Best-effort: a stop that cannot be placed must never make a
        # good fill look like a failed one, and the software stop covers
        # the position either way.
        await _maybe_place_protective_stop(decision, order_row)
        await _maybe_place_exit_oco(decision, order_row)
        return

    # Opposite side of the entry → the decision's position is (fully or
    # partially) exiting. v1 closes the decision when the exit order is
    # filled; partial manual scaling is out of scope (one entry / one exit
    # per decision, long or short).
    if decision.closed_at is None:
        entry = decision.fill_avg_price
        exit_price = order_row.avg_fill_price
        if entry is not None and exit_price is not None and decision.fill_qty:
            qty = min(int(order_row.filled_qty), int(decision.fill_qty))
            # A long profits when price rises (exit - entry); a short
            # profits when price falls (entry - exit) — the mirror image,
            # keyed off the ENTRY side, not the exit fill's side. Both
            # entry/exit prices are already per-contract-unit (Alpaca's
            # own avg_fill_price, never pre-multiplied) — the multiplier
            # only enters when converting that per-unit move into a total
            # dollar P&L, exactly like the equity case's implicit x1.
            signed_move = (
                (entry - exit_price) if entry_side == "SELL" else (exit_price - entry)
            )
            decision.realized_pnl = (
                signed_move * Decimal(qty) * Decimal(multiplier)
            ).quantize(Decimal("0.01"))
        decision.closed_at = order_row.filled_at or datetime.now(UTC)
        if decision.close_reason is None:
            decision.close_reason = _unstamped_close_reason(order_row)
        await _maybe_record_pdt(session, decision, order_row, entry_side)


def _unstamped_close_reason(order_row: object) -> str:
    """Why a decision closed when no close path stamped a reason first.

    Every agent close (position_manager) and every in-app user close stamps
    ``close_reason`` when it SUBMITS. A resting protective stop does not:
    it is placed at fill time and elects at the broker later, possibly
    while this process is down, so nothing is there to stamp it. Before
    this, those closes fell through to 'user_manual', crediting the user
    with an exit the system's own stop made. That understated the stop
    count on the Positions screen and in anything that grades exits by
    reason. The client_order_id prefix is the same marker
    ``_has_in_flight_close`` already relies on to recognise these orders.
    """
    from app.services.orders.option_stops import is_protective_stop_id

    client_order_id = getattr(order_row, "client_order_id", None)
    if is_protective_stop_id(client_order_id):
        return "protective_stop"
    from app.services.orders.exit_oco import is_exit_oco_id

    if is_exit_oco_id(client_order_id):
        kind = str(getattr(order_row, "order_type", "") or "").upper()
        return "bracket_stop" if "STOP" in kind else "bracket_target"
    return "user_manual"


def _is_resting_exit(client_order_id: str | None) -> bool:
    """A broker-side exit meant to rest for the life of the position (an
    Alpaca option's protective stop, a Zerodha equity's GTT OCO), as
    opposed to a close attempt in flight."""
    from app.services.orders.exit_oco import is_exit_oco_id
    from app.services.orders.option_stops import is_protective_stop_id

    return is_protective_stop_id(client_order_id) or is_exit_oco_id(client_order_id)


async def _maybe_place_exit_oco(decision: object, order_row: object) -> None:
    """The Zerodha equity counterpart of _maybe_place_protective_stop:
    a GTT OCO at the disclosed stop and target once the entry fills.
    Swallows everything, for the same reason."""
    from app.services.orders.exit_oco import needs_exit_oco

    if not needs_exit_oco(decision):
        return
    try:
        from app.services.orders.exit_oco import sync_exit_oco
        from engine.db.session import async_session_factory

        await sync_exit_oco(
            async_session_factory(), user_id=str(order_row.user_id), decision=decision
        )
    except Exception:
        logger.warning("order_sync: could not place the GTT exit for decision %s",
                       getattr(decision, "id", "?"), exc_info=True)


async def _maybe_place_protective_stop(decision: object, order_row: object) -> None:
    """Place the resting broker-side stop for a freshly-filled option entry.

    Swallows everything. This runs inside ``sync_user_orders_and_positions``,
    which is on the reconciler fleet's hot path for every user — an
    exception here would abort the rest of that user's sync (fills,
    closes, orphan adoption) over a protective order that the software
    stop already duplicates in the common case.
    """
    if getattr(decision, "closed_at", None) is not None:
        return
    if not bool(
        (getattr(decision, "proposal", None) or {}).get(
            "isOption", (getattr(decision, "proposal", None) or {}).get("is_option", False)
        )
    ):
        return
    try:
        from app.services.orders.option_stops import sync_protective_stop
        from engine.db.session import async_session_factory

        await sync_protective_stop(
            async_session_factory(),
            user_id=str(order_row.user_id),
            decision=decision,
        )
    except Exception:
        logger.warning(
            "order_sync: could not place a protective stop for decision %s — "
            "the position keeps the software stop only",
            getattr(decision, "id", "?"), exc_info=True,
        )


async def _maybe_record_pdt(
    session: AsyncSession, decision: object, close_order: object, entry_side: str = "BUY"
) -> None:
    """Same-UTC-day entry+exit → one pdt_ledger row. Idempotent on the
    close order. Phase 0 uses calendar days (same simplification as the
    PDT lookback); Phase 1.5 swaps to NYSE business days.

    ``entry_side`` is the decision's OWN entry side ("BUY" for a long,
    "SELL" for a short) — a hardcoded "BUY" here would never find a
    short's entry order, undercounting a same-day short round-trip for
    PDT purposes.
    """
    from engine.db.models import Order, PdtLedger

    entry_stmt = (
        select(Order)
        .where(Order.agent_decision_id == decision.id)
        .where(Order.side == entry_side)
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


def _broker_key_for_decision(decision: object) -> str:
    """The key ``broker.list_positions()`` (and the ``positions_snapshot``
    it feeds) uses for this decision's position: the OCC contract for an
    option, the plain symbol otherwise.

    ``agent_decisions.symbol`` is ALWAYS the underlying (docs/
    OPTIONS_PLAYBOOK.md §5.1 — the same convention ``executor.py``'s
    ``_wire_symbol_for`` and ``position_manager.py``'s close path already
    enforce). Before this helper existed, ``_detect_external_closes`` below
    compared the underlying directly against ``held_qty`` (keyed by the
    broker's OCC symbol for an option) — so `held_qty.get(symbol, 0)` was
    ALWAYS 0 for every option position, no matter how many contracts were
    genuinely still held, and this function would mark it
    ``close_reason='external_broker'`` on the very first tick after it
    filled. That silently disabled every fill_qty-and-closed_at-gated exit
    mechanism for the position it had just falsely closed — the SAME class
    of bug ``positions_service.py``'s own ``_broker_key_for_decision``
    fixes for the read path; a shared helper in ``packages/engine`` would
    be a reasonable follow-up so this stops being reimplemented per file.
    """
    proposal = (getattr(decision, "proposal", None) or {})
    if bool(proposal.get("isOption", proposal.get("is_option", False))):
        occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
        if occ:
            return str(occ).upper()
    return decision.symbol.upper()  # type: ignore[attr-defined]


async def _detect_external_closes(
    session: AsyncSession,
    uid: uuid.UUID,
    broker: BrokerInterface,
    *,
    user_id: str,
    market: str = "US",
    source: str | None = None,
) -> None:
    from app.services.orders.exit_oco import EXIT_OCO_PREFIX
    from app.services.orders.option_stops import PROTECTIVE_STOP_PREFIX
    from engine.db.models import AgentDecision, Order

    open_decisions_stmt = (
        select(AgentDecision)
        .where(AgentDecision.user_id == uid)
        .where(AgentDecision.user_response == "approved")
        .where(AgentDecision.fill_qty.is_not(None))
        .where(AgentDecision.closed_at.is_(None))
    )
    open_decisions = [
        d for d in (await session.execute(open_decisions_stmt)).scalars().all()
        if market_of(d.symbol) == market
    ]
    if not open_decisions:
        return

    try:
        broker_positions = await broker.list_positions()
    except Exception:
        logger.exception("order_sync: list_positions failed — skipping close detection")
        return
    held_qty = {p.symbol.upper(): int(p.qty) for p in broker_positions}

    vanished: list[tuple[object, str]] = []
    for decision in open_decisions:
        broker_key = _broker_key_for_decision(decision)
        # != 0, not > 0: Alpaca reports a held SHORT as a NEGATIVE qty, and
        # "still held" must be true for that case too — v1 ignores partial
        # external reductions either way.
        if held_qty.get(broker_key, 0) != 0:
            continue

        # An exit of OURS in flight explains the gap — not external. Any
        # open order past entry on this decision is a close attempt,
        # whichever side it is (BUY-to-cover for a short, SELL for a long),
        # so this must not filter on side.
        in_flight_stmt = (
            select(Order.id)
            .where(Order.agent_decision_id == decision.id)
            .where(Order.status.in_(IN_FLIGHT_STATUSES))
            # A resting protective exit is not an exit in flight. Counting
            # it kept a position the user sold by hand at the broker open
            # for as long as its stop or GTT rested there.
            .where(~Order.client_order_id.like(f"{PROTECTIVE_STOP_PREFIX}%"))
            .where(~Order.client_order_id.like(f"{EXIT_OCO_PREFIX}%"))
            .where(
                or_(
                    Order.broker_order_id.is_not(None),
                    Order.submitted_at >= datetime.now(UTC) - UNACKED_ORDER_GRACE,
                )
            )
            .limit(1)
        )
        if (await session.execute(in_flight_stmt)).scalar_one_or_none() is not None:
            continue
        vanished.append((decision, broker_key))

    # An option can also leave the account with no order at all: it
    # expired, was exercised, or was assigned. Alpaca records each as an
    # account activity, so those are read BEFORE anything is called an
    # external close. Fetched once per pass, and only when an option
    # actually vanished.
    lifecycle: list[AccountActivity] = []
    option_vanished = [d for d, _key in vanished if _is_option_decision(d)]
    if option_vanished:
        lifecycle = await _option_lifecycle_activities(broker, option_vanished)

    for decision, broker_key in vanished:
        symbol = decision.symbol.upper()
        # A bracket's own stop or take-profit leg filled. Those are the
        # broker's child orders, with no orders row of ours, so without
        # this check an exit the system planned read as the user selling
        # at Alpaca (found by apps/api/tests/e2e/test_e2e_equity.py).
        if await _close_from_bracket_leg(session, broker, decision):
            continue
        event = _lifecycle_event_for(lifecycle, broker_key)
        if event is not None:
            await _close_from_lifecycle(
                session, uid, decision, broker_key, event, lifecycle, user_id=user_id,
                source=source,
            )
            continue

        entry_side = str((decision.proposal or {}).get("side", "BUY"))
        multiplier = int((decision.proposal or {}).get("multiplier", 1) or 1)
        # broker_key, not symbol: _last_snapshot_mark matches against the
        # SAME snapshot position dicts held_qty was built from above, which
        # are OCC-keyed for an option.
        approx_exit = await _last_snapshot_mark(
            session, uid, broker_key, multiplier=multiplier, source=source
        )
        realized: Decimal | None = None
        if approx_exit is not None and decision.fill_avg_price is not None and decision.fill_qty:
            # Same entry-side-keyed sign flip as the ordinary close path —
            # a short realizes (entry - exit), not (exit - entry). Both
            # sides of the subtraction are per-contract-unit at this point
            # (``_last_snapshot_mark`` already divided its mark by the
            # multiplier) — this is where that per-unit move becomes a
            # total dollar P&L.
            signed_move = (
                (decision.fill_avg_price - approx_exit)
                if entry_side == "SELL"
                else (approx_exit - decision.fill_avg_price)
            )
            realized = (
                signed_move * Decimal(int(decision.fill_qty)) * Decimal(multiplier)
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
        await _retire_resting_exits(session, broker, decision.id)
        logger.info(
            "order_sync: %s closed EXTERNALLY at the broker (user=%s, approx_pnl=%s)",
            symbol, uid, realized,
        )
        _notify_external_close(user_id=user_id, symbol=symbol, qty=int(decision.fill_qty or 0))


BRACKET_LEG_PREFIX = "bracket-leg-"


async def _close_from_bracket_leg(
    session: AsyncSession, broker: BrokerInterface, decision: object
) -> bool:
    """Close ``decision`` from its entry bracket's filled leg, if one filled.

    The leg is written as an ``orders`` row of ours (client_order_id
    ``bracket-leg-<broker id>``, idempotent on that unique id) and then
    goes through ``_apply_decision_lifecycle`` like any other exit fill:
    realized P&L from the leg's real fill, the PDT ledger, and the
    positions history reading the exit price from an order fill. The
    reason is stamped first so the lifecycle keeps it: ``bracket_stop``
    or ``bracket_target``.
    """
    from engine.db.models import Order

    proposal = getattr(decision, "proposal", None) or {}
    entry_side = str(proposal.get("side", "BUY"))
    parent = (
        await session.execute(
            select(Order)
            .where(Order.agent_decision_id == decision.id)  # type: ignore[attr-defined]
            .where(Order.side == entry_side)
            .where(Order.broker_order_id.is_not(None))
            .order_by(desc(Order.submitted_at))
            .limit(1)
        )
    ).scalar_one_or_none()
    if parent is None:
        return False
    try:
        broker_parent = await broker.get_order(parent.broker_order_id)
    except Exception:
        logger.warning("order_sync: could not re-read bracket parent %s",
                       parent.broker_order_id, exc_info=True)
        return False
    leg = next(
        (
            leg for leg in getattr(broker_parent, "legs", ()) or ()
            if (leg.status.value if hasattr(leg.status, "value") else str(leg.status)) == "filled"
            and leg.filled_qty and leg.avg_fill_price is not None
        ),
        None,
    )
    if leg is None:
        return False

    client_order_id = f"{BRACKET_LEG_PREFIX}{leg.broker_order_id}"[:64]
    row = (
        await session.execute(select(Order).where(Order.client_order_id == client_order_id))
    ).scalar_one_or_none()
    if row is None:
        row = Order(
            id=uuid.uuid4(),
            user_id=parent.user_id,
            broker_connection_id=parent.broker_connection_id,
            agent_decision_id=parent.agent_decision_id,
            client_order_id=client_order_id,
            broker_order_id=leg.broker_order_id,
            symbol=parent.symbol,
            side="SELL" if entry_side == "BUY" else "BUY",
            qty=int(leg.qty),
            order_type=(leg.raw or {}).get("order_type", "").upper() or "UNKNOWN",
            status="filled",
            filled_qty=int(leg.filled_qty),
            avg_fill_price=Decimal(str(leg.avg_fill_price)),
            is_paper=parent.is_paper,
            filled_at=leg.filled_at or datetime.now(UTC),
            raw_response={"source": "bracket_leg", "parent": parent.broker_order_id},
        )
        session.add(row)
        await session.flush()
    if getattr(decision, "close_reason", None) is None:
        decision.close_reason = _bracket_leg_reason(  # type: ignore[attr-defined]
            leg, entry_side=entry_side, entry=getattr(decision, "fill_avg_price", None)
        )
    await _apply_decision_lifecycle(session, row)
    logger.info("order_sync: %s closed by its bracket leg %s (%s)",
                parent.symbol, leg.broker_order_id, decision.close_reason)  # type: ignore[attr-defined]
    return True


def _bracket_leg_reason(leg: object, *, entry_side: str, entry: Decimal | None) -> str:
    """Stop or target leg. The leg's order type says so when the broker
    reports it; otherwise the side of the entry the fill landed on does."""
    kind = str((getattr(leg, "raw", None) or {}).get("order_type", "")).lower()
    if "stop" in kind:
        return "bracket_stop"
    if kind == "limit":
        return "bracket_target"
    fill = Decimal(str(leg.avg_fill_price))  # type: ignore[attr-defined]
    if entry is None:
        return "bracket_stop"
    below = fill < entry
    return "bracket_stop" if below == (entry_side == "BUY") else "bracket_target"


_LIFECYCLE_CLOSE_REASON: dict[str, str] = {
    "OPEXP": "option_expired",
    "OPEXC": "option_exercised",
    "OPASN": "option_assigned",
}

# The activity carries a date, not a time. 21:00 UTC is after the 16:00 ET
# close in both EDT and EST, and still the same UTC date.
_LIFECYCLE_CLOSE_TIME_UTC = time(21, 0)


def _is_option_decision(decision: object) -> bool:
    proposal = getattr(decision, "proposal", None) or {}
    return bool(proposal.get("isOption", proposal.get("is_option", False)))


async def _option_lifecycle_activities(
    broker: BrokerInterface, decisions: list[object]
) -> list[AccountActivity]:
    """Alpaca's option lifecycle activities since the oldest of these
    decisions opened. Empty when the broker has no such method or the read
    fails; the caller then treats the vanish as an external close, which
    is what it did before this existed."""
    fetch = getattr(broker, "list_option_lifecycle_activities", None)
    if fetch is None:
        return []
    opened = [
        t.date()
        for t in (getattr(d, "triggered_at", None) for d in decisions)
        if isinstance(t, datetime)
    ]
    since = (min(opened) if opened else datetime.now(UTC).date()) - timedelta(days=1)
    try:
        return list(await fetch(since=since))
    except Exception:
        logger.warning(
            "order_sync: option lifecycle activities unavailable; a vanished option "
            "is treated as an external close this tick",
            exc_info=True,
        )
        return []


def _lifecycle_event_for(
    activities: list[AccountActivity], occ: str
) -> AccountActivity | None:
    """The newest expiry, exercise or assignment on this contract."""
    for activity in activities:
        if activity.symbol == occ and activity.activity_type in _LIFECYCLE_CLOSE_REASON:
            return activity
    return None


async def _close_from_lifecycle(
    session: AsyncSession,
    uid: uuid.UUID,
    decision: object,
    occ: str,
    event: AccountActivity,
    activities: list[AccountActivity],
    *,
    user_id: str,
    source: str | None = None,
) -> None:
    """Close a decision whose contract left the account through expiry,
    exercise or assignment, with the reason that actually happened."""
    from engine.db.models import AgentDecision

    reason = _LIFECYCLE_CLOSE_REASON[event.activity_type]
    proposal = decision.proposal or {}  # type: ignore[attr-defined]
    entry_side = str(proposal.get("side", "BUY"))
    multiplier = int(proposal.get("multiplier", 1) or 1)
    qty = int(decision.fill_qty or 0)  # type: ignore[attr-defined]
    entry = decision.fill_avg_price  # type: ignore[attr-defined]

    exit_price: Decimal | None
    if event.activity_type == "OPEXP":
        # Removed at zero value. Exact, not an estimate.
        exit_price = Decimal("0")
    else:
        # Became stock at the strike. What the contract was worth at that
        # moment is its intrinsic value, which the activity does not report,
        # so the last mark before it vanished stands in, as it does for an
        # external close.
        exit_price = await _last_snapshot_mark(
            session, uid, occ, multiplier=multiplier, source=source
        )

    realized: Decimal | None = None
    if exit_price is not None and entry is not None and qty:
        signed_move = (entry - exit_price) if entry_side == "SELL" else (exit_price - entry)
        realized = (signed_move * Decimal(qty) * Decimal(multiplier)).quantize(Decimal("0.01"))

    closed_at = min(
        datetime.combine(event.day, _LIFECYCLE_CLOSE_TIME_UTC, tzinfo=UTC), datetime.now(UTC)
    )
    await session.execute(
        update(AgentDecision)
        .where(AgentDecision.id == decision.id)  # type: ignore[attr-defined]
        .values(closed_at=closed_at, close_reason=reason, realized_pnl=realized)
    )
    underlying = decision.symbol.upper()  # type: ignore[attr-defined]
    logger.warning(
        "order_sync: %s %s (%s) on %s (user=%s, realized=%s)",
        occ, reason, event.activity_type, event.day, uid, realized,
    )

    if event.activity_type == "OPEXP":
        _notify_option_expired(user_id=user_id, symbol=underlying, qty=qty)
        return

    # The paired OPTRD is the underlying trade the exercise or assignment
    # made: signed shares, at the strike.
    shares = sum(
        a.qty for a in activities
        if a.activity_type == "OPTRD" and a.symbol == underlying and a.day == event.day
    )
    _alert_stock_delivered(
        user_id=user_id,
        occ=occ,
        underlying=underlying,
        contracts=qty,
        shares=int(shares),
        verb="exercised" if event.activity_type == "OPEXC" else "assigned",
    )


def _notify_option_expired(*, user_id: str, symbol: str, qty: int) -> None:
    """Fire-and-forget push, lock-screen-safe (same rule as external close)."""
    try:
        from app.services.notifications.notifications import schedule_position_event_notification

        schedule_position_event_notification(
            user_id=user_id,
            title="Option expired",
            body=f"Your {qty} {symbol} option contract(s) expired. Trade log updated.",
        )
    except Exception:
        logger.exception("order_sync: option-expired notification failed")


def _alert_stock_delivered(
    *, user_id: str, occ: str, underlying: str, contracts: int, shares: int, verb: str
) -> None:
    """Exercise and assignment turn a capped-risk option into stock that no
    decision manages: no stop, no time exit, and 100 shares per contract of
    notional. That is an operator page, not a notification."""
    if shares:
        traded = f"{abs(shares)} {underlying} shares {'bought' if shares > 0 else 'sold'}"
    else:
        traded = f"{underlying} shares traded (quantity not reported)"
    try:
        from app.services.notifications.ops_alerts import raise_ops_alert

        raise_ops_alert(
            f"option_{verb}",
            user_id=user_id,
            key=occ,
            title=f"{underlying} option {verb}",
            body=(
                f"{contracts} {occ} {verb}: {traded} at the strike. The resulting "
                f"{underlying} stock position has no stop and no time exit. Close it "
                "from Positions, or flatten all."
            ),
        )
    except Exception:
        logger.exception("order_sync: stock-delivered alert failed for %s", occ)


async def _retire_resting_exits(
    session: AsyncSession, broker: BrokerInterface, decision_id: object
) -> None:
    """Cancel a closed position's resting exits at the broker (an Alpaca
    option's protective stop, a Zerodha GTT OCO) and mark their rows. The
    position is gone, so a stop or GTT left behind could only ever fire a
    sell of something no longer held. Best effort: a failure is logged
    and the row stays for the next pass to report."""
    from engine.db.models import Order

    rows = (await session.execute(
        select(Order)
        .where(Order.agent_decision_id == decision_id)
        .where(Order.status.in_(OPEN_ORDER_STATUSES))
        .where(Order.broker_order_id.is_not(None))
    )).scalars().all()
    for row in rows:
        if not _is_resting_exit(row.client_order_id):
            continue
        try:
            await broker.cancel_order(row.broker_order_id)
        except Exception:
            logger.warning("order_sync: could not cancel resting exit %s",
                           row.broker_order_id, exc_info=True)
            continue
        row.status = "canceled"
        row.canceled_at = datetime.now(UTC)


async def _last_snapshot_mark(
    session: AsyncSession, uid: uuid.UUID, symbol: str, *, multiplier: int = 1,
    source: str | None = None,
) -> Decimal | None:
    """Most recent snapshot price for a symbol — the best exit-price proxy
    we have for a close that happened outside our order flow.

    ``multiplier`` is supplied by the caller (read off the decision's own
    persisted proposal — see ``_detect_external_closes``), not looked up
    from the snapshot's own position dict: a single source for the number
    keeps the divide here and the multiply at the call site from ever
    disagreeing about which multiplier they mean.
    """
    from engine.db.models import PositionsSnapshot

    stmt = (
        select(PositionsSnapshot)
        .where(PositionsSnapshot.user_id == uid)
        .where(PositionsSnapshot.source == source if source else True)
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
            # qty and market_value share a sign (both negative for a held
            # short, per Alpaca's convention) — abs() on both turns that
            # into the same positive per-share price a long would produce,
            # instead of excluding every short from ever getting a mark.
            # market_value is already multiplier-scaled (a total dollar
            # value); dividing by qty alone would overstate an option's
            # per-contract price by the multiplier.
            if qty != 0 and mv != 0:
                return Decimal(str(round(abs(mv) / (abs(qty) * multiplier), 4)))
    return None


def _notify_external_close(*, user_id: str, symbol: str, qty: int) -> None:
    """Fire-and-forget push — lock-screen-safe, no IDs/PII (AGENTV1 rule)."""
    try:
        from app.services.notifications.notifications import schedule_position_event_notification

        schedule_position_event_notification(
            user_id=user_id,
            title="Position closed at broker",
            body=f"Your {qty} {symbol} closed directly at Alpaca — trade log updated.",
        )
    except Exception:
        logger.exception("order_sync: external-close notification failed")


# ─────────────────────────────────────────────────────────────────────
# 0. Orphan adoption — a decision the broker filled but we never recorded
# ─────────────────────────────────────────────────────────────────────


def _decision_broker_key(decision: object) -> str:
    """The symbol ``broker.list_positions()`` reports for this decision.

    ``agent_decisions.symbol`` is ALWAYS the underlying (docs/
    OPTIONS_PLAYBOOK.md §5.1), so an option has to be matched on its OCC
    string instead — the identical convention
    ``positions_service._broker_key_for_decision`` documents at length.
    Duplicated rather than imported to keep this module's dependency
    surface at ``engine.db`` + ``broker``, as the rest of the file is.
    """
    proposal = getattr(decision, "proposal", None) or {}
    if bool(proposal.get("isOption", proposal.get("is_option", False))):
        occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
        if occ:
            return str(occ).upper()
    return str(getattr(decision, "symbol", "")).upper()


async def _adopt_orphaned_fills(
    session: AsyncSession, uid: uuid.UUID, broker: BrokerInterface, *, market: str = "US"
) -> None:
    """Heal decisions the broker has a position for but whose entry
    columns are still NULL.

    Steps 1+2 above converge fills through the ``orders`` table, which is
    the right path and covers everything ``executor.py`` places. It cannot
    cover anything that reached the broker WITHOUT an ``orders`` row —
    which is exactly what the three ``packages/broker.place_order`` call
    sites in ``options/tools/{trade,guard}.py`` did before
    ``persist_placed_order`` existed (see its docstring), and what any
    future crash between "broker accepted" and "audit row written" will do
    again.

    The consequence is not cosmetic, and is why this runs every tick
    rather than as a one-off migration: BOTH ``manage_positions_for_user``
    (the ratchet, the stop, the time stop) AND
    ``sweep_expiring_options_for_user`` (the DTE<=2 sweep) filter on
    ``AgentDecision.fill_qty IS NOT NULL``. A position with a NULL
    ``fill_qty`` is a REAL position at the broker with none of
    docs/OPTIONS_PLAYBOOK.md §3's five exits attached to it — no stop, no
    trail, no expiry sweep — and nothing anywhere logged that fact. It
    also renders in the UI as "AWAITING FILL / not filled yet" forever,
    which is how this was found: six filled option positions at Alpaca,
    six ``agent_decisions`` rows with NULL ``fill_qty``, zero ``orders``
    rows, and an unmanaged options book.

    Deliberately narrow, and every one of these is load-bearing:
      * Only ``risk_approved`` + ``user_response='approved'`` + not closed
        rows are considered — the same triple ``positions_service`` uses
        for "ours and open". A rejected or still-pending proposal must
        never adopt a position just because the symbol matches.
      * Only when the broker's position is the SAME SIDE the decision
        proposed. A held long cannot heal a decision that proposed a
        short; adopting across sides would invent a fill that never
        happened and hand the exit ladder an inverted P&L.
      * ``qty`` is the broker's, clamped to the proposal's — one broker
        position can back at most the quantity we asked for; the surplus
        belongs to some other decision (or to the user's own manual
        trade) and must stay unclaimed rather than inflate this row.
      * A broker key already claimed by a filled decision is skipped, so
        two orphans on one contract cannot both adopt the same lot.

    Never raises: failing to heal an audit row must not take down the rest
    of the fleet tick for this user.
    """
    from engine.db.models import AgentDecision

    stmt = (
        select(AgentDecision)
        .where(AgentDecision.user_id == uid)
        .where(AgentDecision.risk_approved.is_(True))
        .where(AgentDecision.user_response == "approved")
        .where(AgentDecision.closed_at.is_(None))
        .where(AgentDecision.fill_qty.is_(None))
    )
    orphans = [
        d for d in (await session.execute(stmt)).scalars().all() if market_of(d.symbol) == market
    ]
    if not orphans:
        return

    claimed_stmt = (
        select(AgentDecision)
        .where(AgentDecision.user_id == uid)
        .where(AgentDecision.closed_at.is_(None))
        .where(AgentDecision.fill_qty.is_not(None))
    )
    claimed = {
        _decision_broker_key(d)
        for d in (await session.execute(claimed_stmt)).scalars().all()
    }

    try:
        positions = await broker.list_positions()
    except Exception:
        logger.exception("order_sync: list_positions failed — orphan adoption skipped")
        return

    held = {p.symbol.upper(): p for p in positions}

    for decision in orphans:
        key = _decision_broker_key(decision)
        if not key or key in claimed:
            continue
        pos = held.get(key)
        if pos is None or pos.qty == 0:
            continue

        proposal = decision.proposal or {}
        # A short's entry is a SELL and Alpaca reports its qty negative; a
        # long's entry is a BUY and its qty positive. Comparing the two
        # signs is what keeps a held long from healing a short decision.
        entry_is_buy = str(proposal.get("side", "BUY")).upper() != "SELL"
        if entry_is_buy != (pos.qty > 0):
            continue

        wanted = int(proposal.get("qty") or 0) or abs(pos.qty)
        adopted = min(abs(pos.qty), wanted)
        if adopted <= 0:
            continue

        decision.fill_qty = adopted
        decision.fill_avg_price = Decimal(str(round(float(pos.avg_entry_price), 4)))
        claimed.add(key)
        logger.warning(
            "order_sync: ADOPTED orphaned fill for decision %s (%s) — %d @ %s. "
            "The broker held this position with no local fill record, so every "
            "fill_qty-gated exit (ratchet/stop/expiry sweep) was skipping it.",
            decision.id, key, adopted, decision.fill_avg_price,
        )
