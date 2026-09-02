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

from app.services.broker.broker_use import with_broker_client

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from broker.base import BrokerInterface

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
        # Adoption runs FIRST: it is what makes a broker-real position
        # visible to everything keyed on ``fill_qty IS NOT NULL``, and
        # ``_detect_external_closes`` below is one of those readers — an
        # unadopted orphan is invisible to it too.
        await _adopt_orphaned_fills(session, uid, broker)
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
            decision.close_reason = "user_manual"
        await _maybe_record_pdt(session, decision, order_row, entry_side)


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
        from engine.db.session import async_session_factory

        from app.services.orders.option_stops import sync_protective_stop

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
            .limit(1)
        )
        if (await session.execute(in_flight_stmt)).scalar_one_or_none() is not None:
            continue

        entry_side = str((decision.proposal or {}).get("side", "BUY"))
        multiplier = int((decision.proposal or {}).get("multiplier", 1) or 1)
        # broker_key, not symbol: _last_snapshot_mark matches against the
        # SAME snapshot position dicts held_qty was built from above, which
        # are OCC-keyed for an option.
        approx_exit = await _last_snapshot_mark(session, uid, broker_key, multiplier=multiplier)
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
        logger.info(
            "order_sync: %s closed EXTERNALLY at the broker (user=%s, approx_pnl=%s)",
            symbol, uid, realized,
        )
        _notify_external_close(user_id=user_id, symbol=symbol, qty=int(decision.fill_qty or 0))


async def _last_snapshot_mark(
    session: AsyncSession, uid: uuid.UUID, symbol: str, *, multiplier: int = 1
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
    session: AsyncSession, uid: uuid.UUID, broker: BrokerInterface
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
    orphans = (await session.execute(stmt)).scalars().all()
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
