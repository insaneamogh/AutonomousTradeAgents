"""Open + closed positions — the read path behind /api/v1/positions.

An agent-managed "position" IS an open agent decision: approved + filled
+ not yet closed. We list those (per authed user, indexed query) and
enrich each with a live mark from the latest reconciler snapshot so the
mobile screen can show unrealized P&L and the disclosed exit plan without
a broker round-trip on every list.

We ALSO list positions that exist at the broker with no agent decision
behind them, flagged ``managed=False``. Those arise whenever a position
was opened outside this app — directly in the broker's own UI, or before
this deployment's decision history began. Dropping them made /account
report a non-zero open-position count that /positions rendered as an
empty list, which reads as a broken screen. They carry no exit plan and
no ``decision_id``, because there is no decision lifecycle to close them
through; the client offers no close button for them.

``list_closed_positions`` is the other half: every decision this app saw
BOTH open and later close, newest-close-first, for GET /positions/history
— see its own docstring for the one gap (unmanaged-position closes).

Postgres-only — positions require a real DB + broker. MockStore dev mode
returns [] (there is no position ledger).
"""

from __future__ import annotations

import logging
import uuid as _uuid
from datetime import date as _date
from typing import Any

from app.core.ids import to_uuid as _to_uuid
from app.schemas.positions import ClosedPositionDto, OpenPositionDto
from broker.types import OccSymbol
from engine.env import env_flag

logger = logging.getLogger("api.positions")

# Order rows in these states will never fill — the decision behind one
# stays user_response='approved' with fill_qty NULL forever (order_sync
# only touches the decision on a FILL), so without this exclusion a dead
# order would show as "awaiting fill" indefinitely instead of vanishing.
_DEAD_ORDER_STATUSES = frozenset(
    {"rejected", "canceled", "cancelled", "expired", "done_for_day"}
)


async def list_open_positions(user_id: str) -> list[OpenPositionDto]:
    """Open + pending-fill agent positions for the user, with live marks."""
    if not env_flag("USE_POSTGRES"):
        return []
    uid = _to_uuid(user_id)
    if uid is None:
        return []

    from sqlalchemy import desc, select

    from engine.db.models import AgentDecision, Order, PositionsSnapshot
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        base = (
            select(AgentDecision)
            .where(AgentDecision.user_id == uid)
            .where(AgentDecision.risk_approved.is_(True))
            .where(AgentDecision.user_response == "approved")
            .where(AgentDecision.closed_at.is_(None))
        )
        filled = (await session.execute(
            base.where(AgentDecision.fill_qty.is_not(None))
            .order_by(desc(AgentDecision.triggered_at))
        )).scalars().all()

        # Approved, no fill yet — the order is still working at the broker
        # (or hasn't been tried yet). Common outside market hours: a
        # MARKET order placed pre-open sits accepted until the session
        # starts. Cross-referenced against `orders` below to drop any
        # that already died (rejected/canceled) rather than show forever.
        awaiting = (await session.execute(
            base.where(AgentDecision.fill_qty.is_(None))
            .order_by(desc(AgentDecision.triggered_at))
        )).scalars().all()

        if awaiting:
            order_stmt = (
                select(Order.agent_decision_id, Order.status)
                .where(Order.user_id == uid)
                .where(Order.agent_decision_id.in_([d.id for d in awaiting]))
                .order_by(desc(Order.submitted_at))
            )
            # Latest order per decision — a retried approval can leave more
            # than one row; only the newest attempt's status matters.
            latest_status: dict[object, str] = {}
            for decision_id, status in await session.execute(order_stmt):
                latest_status.setdefault(decision_id, status)
            awaiting = [
                d for d in awaiting
                if latest_status.get(d.id) not in _DEAD_ORDER_STATUSES
            ]

        # One snapshot read → symbol → last mark map for live unrealized P&L,
        # and the source of truth for what the broker actually holds.
        snap_stmt = (
            select(PositionsSnapshot)
            .where(PositionsSnapshot.user_id == uid)
            .order_by(desc(PositionsSnapshot.captured_at))
            .limit(1)
        )
        snapshot = (await session.execute(snap_stmt)).scalar_one_or_none()

    marks: dict[str, float] = {}
    # The broker's OWN unrealized P&L per symbol — already correctly scaled
    # and signed, straight off PortfolioPosition.unrealized_pl (see its own
    # docstring). Kept separate from `marks` because `last` (a per-share/
    # per-contract-unit PRICE) is still needed for display even when we
    # have the broker's own dollar P&L.
    broker_pnl: dict[str, float] = {}
    broker_positions: dict[str, dict[str, Any]] = {}
    if snapshot is not None:
        for pos in snapshot.open_positions or []:
            sym = str(pos.get("symbol", "")).upper()
            qty = int(pos.get("qty", 0) or 0)
            mv = float(pos.get("market_value", 0) or 0)
            if not sym or qty == 0:
                continue
            broker_positions[sym] = pos
            # Alpaca reports both qty and market_value negative for a short,
            # so the two always share a sign — abs() on both keeps this a
            # positive price for a long OR a short instead of silently
            # dropping every short position from ever getting a live mark.
            if mv != 0:
                marks[sym] = round(abs(mv) / abs(qty), 4)
            upl = pos.get("unrealized_pl")
            if upl is not None:
                broker_pnl[sym] = float(upl)

    out: list[OpenPositionDto] = []
    for d in filled:
        out.append(_from_decision(d, marks, broker_pnl, status="open"))
    for d in awaiting:
        out.append(_from_decision(d, marks, broker_pnl, status="pending_fill"))

    # The key the BROKER uses for each of OUR managed positions — never
    # OpenPositionDto.symbol (see _broker_key_for_decision: that field is
    # always the underlying, by product convention, for both equities and
    # options). Computed from the raw decisions, not from `out`, because the
    # DTO has nowhere to carry an OCC symbol at all.
    covered = {_broker_key_for_decision(d) for d in (*filled, *awaiting)}
    out.extend(_unmanaged(broker_positions, covered, snapshot))
    return out


def _broker_key_for_decision(d: object) -> str:
    """The key ``broker.list_positions()`` (and thus
    ``PositionsSnapshot.open_positions``, which mirrors it field-for-field —
    see ``engine.reconciler.snapshot``) uses for this decision's position:
    the OCC contract for an option, the plain symbol otherwise.

    ``agent_decisions.symbol`` is ALWAYS the underlying (docs/
    OPTIONS_PLAYBOOK.md §5.1 — the same convention ``executor.py``'s
    ``_wire_symbol_for`` and ``position_manager.py``'s close path already
    enforce on the entry/close side). Before this helper existed, this
    module compared that underlying directly against broker-reported
    symbols — which are OCC strings for options — so an option position
    could NEVER match a decision that plainly exists for it: every open
    option showed as broker-side "unmanaged" (``_unmanaged()`` below) no
    matter how many agent_decisions rows named it, and (separately) never
    got a live mark in ``_from_decision`` either, for the identical reason.
    """
    proposal = getattr(d, "proposal", None) or {}
    if bool(proposal.get("isOption", proposal.get("is_option", False))):
        occ = proposal.get("occSymbol") or proposal.get("occ_symbol")
        if occ:
            return str(occ).upper()
    return d.symbol.upper()  # type: ignore[attr-defined]


def _coerce_expiry_date(value: object) -> _date | None:
    """Best-effort parse of a persisted proposal's expiry field — JSONB
    round-trips a Python ``date`` as an ISO-8601 string. Mirrors
    ``position_manager.py``'s own ``_coerce_expiry_date`` (small, local copy
    rather than a cross-module import of an underscore-private helper —
    same convention ``trading_agents.options.tools.guard``'s
    ``_trading_mode`` docstring already defends for this codebase). Returns
    None on anything unparseable rather than raising — a malformed expiry
    must not crash the whole positions list for every other row."""
    if isinstance(value, _date):
        return value
    if isinstance(value, str):
        try:
            return _date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def _from_decision(
    d: object, marks: dict[str, float], broker_pnl: dict[str, float], *, status: str
) -> OpenPositionDto:
    proposal = d.proposal or {}  # type: ignore[attr-defined]
    # The entry proposal's own direction, falling back to side == "SELL"
    # for rows that predate the "direction" field.
    raw_direction = proposal.get("direction")
    side = str(proposal.get("side", "BUY"))
    direction = raw_direction if raw_direction in ("long", "short") else (
        "short" if side == "SELL" else "long"
    )
    # NOT `direction == "short"`. `direction` is the trading THESIS
    # ("short" = a bearish bet), and for an option that bearish bet is
    # implemented by BUYING a put — Phase A is long-calls/long-puts only
    # (`RiskCaps.options_disabled`'s own docstring), so `side` is ALWAYS
    # "BUY" for every options row this system ever writes, regardless of
    # whether `direction` says "long" or "short". The P&L SIGN has to
    # follow what we actually hold at the broker (`side == "SELL"` means
    # we are short the contract/shares — true only for a genuine equity
    # short), not what the thesis was betting on. Every sibling
    # computation in this file agrees: `_unmanaged()` below derives its
    # own `is_short` from the broker's signed qty, and
    # `position_manager.py` derives it from `held.qty < 0` — this was the
    # one place still keying off the thesis label instead.
    #
    # Verified live 2026-09-01: a long PUT (BUY META260918P00585000,
    # direction="short") entered at $20.55, marked at $18.60 — a REAL
    # -$195 loss on a position we own — rendered as "+$195" here because
    # `is_short` was true and flipped the sign. Alpaca's own account
    # confirmed the position as "Long" with a real loss the whole time.
    is_short = side == "SELL"

    # A pending-fill row has no fill yet, so it reports the proposal's
    # planned qty rather than a fill_qty that is NULL by definition.
    entry = float(d.fill_avg_price) if d.fill_avg_price is not None else None  # type: ignore[attr-defined]
    # Contract multiplier — 100 for a standard US equity option, 1 (a
    # no-op) for everything else. ``marks`` is keyed off a RAW
    # abs(market_value)/abs(qty) (see list_open_positions) that is still
    # multiplier-inflated for an option; dividing it back out here — using
    # the SAME multiplier this decision's own proposal carries — is what
    # keeps this the one and only place that number gets corrected,
    # instead of also (wrongly) correcting it where the snapshot itself
    # was parsed, which would use a possibly-out-of-sync multiplier from a
    # different source (the reconciler snapshot, not this decision).
    multiplier = int(proposal.get("multiplier", 1) or 1)
    # OCC-keyed for an option, matching how `marks` itself was built from
    # PositionsSnapshot.open_positions (see _broker_key_for_decision) — using
    # d.symbol (the underlying) here always missed for options, so every
    # OPEN option position showed last_price=None / unrealized_pnl=None.
    broker_key = _broker_key_for_decision(d)
    raw_last = marks.get(broker_key) if status == "open" else None
    last = raw_last / multiplier if raw_last is not None else None
    qty = int(d.fill_qty) if d.fill_qty is not None else int(proposal.get("qty", 0) or 0)  # type: ignore[attr-defined]
    # fill_qty is always a non-negative share/contract COUNT (an order's
    # filled quantity, not a signed position) — a short's unrealized P&L
    # is the mirror of a long's: it gains when price FALLS, so the sign
    # flips on direction rather than on the sign of qty. The multiplier
    # applies here too — ``last``/``entry`` are both per-contract-unit at
    # this point, so converting to a total dollar P&L needs qty x multiplier,
    # exactly like the equity case's implicit x1.
    #
    # Prefer the broker's OWN unrealized P&L over re-deriving one from
    # `last`/`entry` — the broker's number is already correctly scaled and
    # signed, and a re-derivation can drift from it (confirmed live
    # 2026-09-03: a short equity position derived to exactly $0 here while
    # Alpaca's own dashboard reported a real -$22 for the same position).
    # Falls back to the derivation only when the broker didn't report one —
    # a stale pre-migration snapshot row, or a non-Alpaca broker.
    broker_upl = broker_pnl.get(broker_key) if status == "open" else None
    unrealized = (
        round(broker_upl, 2)
        if broker_upl is not None
        else (
            round((-1.0 if is_short else 1.0) * (last - entry) * qty * multiplier, 2)
            if (last is not None and entry is not None and qty)
            else None
        )
    )

    # Options facts (Phase A) — schemas/positions.py's OpenPositionDto has
    # carried these fields, all defaulting to "not an option", since they
    # were added "purely additive, so the wire contract is ready the moment
    # that track wires population" — nothing here ever did, so every
    # position rendered as a plain equity regardless of what it actually
    # was: a $2,392-notional NVDA call showed as "NVDA LONG qty 4" with no
    # indication it was a 100x-levered contract, not 4 shares.
    is_option = bool(proposal.get("isOption", proposal.get("is_option", False)))
    occ_symbol = (
        (proposal.get("occSymbol") or proposal.get("occ_symbol")) if is_option else None
    )
    contract_type = (
        (proposal.get("contractType") or proposal.get("contract_type")) if is_option else None
    )
    strike = proposal.get("strike") if is_option else None
    expiry_date = (
        _coerce_expiry_date(proposal.get("expiryDate", proposal.get("expiry_date")))
        if is_option
        else None
    )

    return OpenPositionDto(
        decision_id=str(d.id),  # type: ignore[attr-defined]
        managed=True,
        status=status,  # type: ignore[arg-type]
        symbol=d.symbol,  # type: ignore[attr-defined]
        side=side,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        qty=qty,
        avg_entry_price=entry,
        last_price=last,
        unrealized_pnl=unrealized,
        exit_mode=d.exit_mode if d.exit_mode in ("agent", "manual") else "agent",  # type: ignore[attr-defined]
        stop_loss=proposal.get("stopLoss"),
        target_price=proposal.get("targetPrice"),
        time_stop_days=proposal.get("timeStopDays"),
        opened_at=d.user_responded_at or d.triggered_at,  # type: ignore[attr-defined]
        is_option=is_option,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=float(strike) if strike is not None else None,
        expiry_date=expiry_date,
        occ_symbol=str(occ_symbol) if occ_symbol else None,
        multiplier=multiplier,
    )


def _unmanaged(
    broker_positions: dict[str, dict[str, Any]],
    covered: set[str],
    snapshot: object | None,
) -> list[OpenPositionDto]:
    """Broker positions with no agent decision behind them.

    ``covered`` is the set of broker-side keys (OCC for an option, plain
    symbol otherwise — see ``_broker_key_for_decision``) our OWN managed
    positions already account for. Deliberately NOT derived from the
    already-built ``OpenPositionDto`` list: that DTO's ``symbol`` is always
    the underlying (the correct thing to DISPLAY), which is exactly the
    field that must NOT be used to decide whether a broker-reported option
    position is already covered.

    ``opened_at`` falls back to the snapshot time because the broker
    snapshot does not carry an open timestamp — it is the earliest moment
    we can honestly claim to have observed the position, not a claim about
    when it was actually opened.
    """
    if not broker_positions:
        return []

    captured_at = getattr(snapshot, "captured_at", None)
    if captured_at is None:
        return []

    out: list[OpenPositionDto] = []
    for sym, pos in sorted(broker_positions.items()):
        if sym in covered:
            continue
        qty = int(pos.get("qty", 0) or 0)
        # A short is reported with a negative qty; qty on the wire is a
        # share COUNT, with the sign carried by ``direction``.
        is_short = qty < 0
        entry = pos.get("avg_entry_price")
        mv = float(pos.get("market_value", 0) or 0)
        # An unmanaged position has no decision/proposal to read a
        # multiplier from — the snapshot's own position dict is the only
        # source available, unlike the managed path above (which reads it
        # off the decision instead of the snapshot, for exactly the
        # single-source reason explained there).
        multiplier = int(pos.get("multiplier", 1) or 1)
        last = round(abs(mv) / (abs(qty) * multiplier), 4) if qty and mv else None
        entry_f = float(entry) if entry is not None else None
        # Same preference as _from_decision above: trust the broker's own
        # unrealized P&L over re-deriving one, when it reported one.
        broker_upl = pos.get("unrealized_pl")
        unrealized = (
            round(float(broker_upl), 2)
            if broker_upl is not None
            else (
                round((-1.0 if is_short else 1.0) * (last - entry_f) * abs(qty) * multiplier, 2)
                if (last is not None and entry_f is not None)
                else None
            )
        )

        # Options facts — see _from_decision's identical block for why this
        # matters (a position rendering as plain equity when it is actually
        # a 100x-levered option is materially misleading, not cosmetic).
        # `sym` IS the OCC contract already when `is_option` (Alpaca keys
        # option positions by OCC — the same fact _broker_key_for_decision
        # relies on above); parsed here since the snapshot's own position
        # dict carries no separate contract_type/strike/expiry fields.
        is_option = bool(pos.get("is_option", False))
        occ = OccSymbol.try_parse(sym) if is_option else None
        out.append(
            OpenPositionDto(
                decision_id=None,
                managed=False,
                # The underlying, matching the managed path's display
                # convention (symbol never means the OCC contract anywhere
                # in this API — occ_symbol carries that) — `sym` itself is
                # the OCC string when `is_option`, so without this an
                # unmanaged option rendered as e.g. "NVDA260918C00215000
                # LONG qty 2" instead of "NVDA".
                symbol=occ.underlying if occ is not None else sym,
                side="SELL" if is_short else "BUY",
                direction="short" if is_short else "long",
                qty=abs(qty),
                avg_entry_price=entry_f,
                last_price=last,
                unrealized_pnl=unrealized,
                # No agent exit plan exists for a position the agent did
                # not open, so the exit is the user's.
                exit_mode="manual",
                stop_loss=None,
                target_price=None,
                time_stop_days=None,
                opened_at=captured_at,
                is_option=is_option,
                contract_type=occ.contract_type if occ is not None else None,  # type: ignore[arg-type]
                strike=occ.strike if occ is not None else None,
                expiry_date=occ.expiry if occ is not None else None,
                occ_symbol=sym if is_option else None,
                multiplier=multiplier,
            )
        )
    return out


# ─────────────────────────────────────────────────────────────────────
# Closed positions — GET /api/v1/positions/history
# ─────────────────────────────────────────────────────────────────────


async def list_closed_positions(
    user_id: str,
    *,
    symbol: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[ClosedPositionDto], int]:
    """Newest-close-first page of the caller's closed positions, plus the
    total count for that filter — mirrors ``decisions_list.list_decisions``'
    pagination shape.

    Source is ``agent_decisions.closed_at IS NOT NULL``: every position
    this app ever tracked opening AND later saw close, however the close
    happened — the position manager's own time/signal/expiry/premium exit
    (``exit_mode='agent'``), a user tap on "Close" in this app
    (``close_reason='user_manual'``), or ``order_sync`` detecting after the
    fact that the user closed it directly at the broker
    (``close_reason='external_broker'``).

    **The gap**: a position with NO ``AgentDecision`` row at all — opened
    before this deployment's decision history, or opened directly at the
    broker — has nothing here to join against, even after
    ``close_unmanaged_position_now`` closes it (that path persists an
    unlinked ``orders`` row with ``agent_decision_id=NULL``; see its own
    docstring for why there is no decision row left to stamp). Verified
    live against production 2026-09-01: 196 agent_decisions, 6 closed, ALL
    ``external_broker``; zero ``orders`` rows with ``agent_decision_id IS
    NULL`` — so this gap is currently unobserved (nothing is silently
    missing today), not proven absent going forward.
    """
    if not env_flag("USE_POSTGRES"):
        return [], 0
    uid = _to_uuid(user_id)
    if uid is None:
        return [], 0

    from sqlalchemy import desc, func, select

    from engine.db.models import AgentDecision, Order
    from engine.db.session import async_session_factory

    filters = [
        AgentDecision.user_id == uid,
        AgentDecision.closed_at.is_not(None),
        # A retirement is not a close. `account_switch` rows were stamped by
        # `reconcile_account_identity` when the broker keys changed — the
        # position was never exited, it stopped being ours. They carry no
        # realized P&L and rendering them as history put nine "—" rows in
        # the ledger for trades that did not happen on this account.
        AgentDecision.close_reason.is_distinct_from("account_switch"),
    ]
    if symbol:
        filters.append(AgentDecision.symbol == symbol.upper())

    factory = async_session_factory()
    async with factory() as session:
        # Everything closed at or before the most recent account switch
        # belongs to a PREVIOUS Alpaca account. `agent_decisions` is scoped
        # to `user_id`, not to an account, and this operator ran three paper
        # accounts through the same user during the contest — so without
        # this boundary the history mixed all three.
        #
        # Observed 2026-09-04, on submission day: a -$1,200 CME stop-out
        # from 09-01 and a +$325 AAPL close from 09-02 were both rendered
        # against account PA3JDGMXSHYK, which was not created until 09-03
        # and never held either position. The switch timestamp is the only
        # account boundary this schema records; when no switch has ever
        # happened there is no boundary and nothing is filtered.
        switch_boundary = (
            await session.execute(
                select(func.max(AgentDecision.closed_at)).where(
                    AgentDecision.user_id == uid,
                    AgentDecision.close_reason == "account_switch",
                )
            )
        ).scalar_one_or_none()
        if switch_boundary is not None:
            filters.append(AgentDecision.closed_at > switch_boundary)

        total = (
            await session.execute(
                select(func.count()).select_from(AgentDecision).where(*filters)
            )
        ).scalar_one()

        rows = (
            await session.execute(
                select(AgentDecision)
                .where(*filters)
                .order_by(desc(AgentDecision.closed_at))
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()

        # The exact closing fill, when this app placed the close order
        # itself (an agent close or an in-app user close both create a
        # decision-linked Order row on the opposite side of the entry) — an
        # `external_broker` close has none (order_sync updates
        # AgentDecision directly; see its own docstring), so those decisions
        # fall back to `_closed_from_decision`'s realized_pnl back-solve.
        # One batched query for the whole page rather than N+1.
        close_orders: dict[_uuid.UUID, object] = {}
        if rows:
            entry_side_by_id = {d.id: str((d.proposal or {}).get("side", "BUY")) for d in rows}
            order_rows = (
                await session.execute(
                    select(Order)
                    .where(Order.agent_decision_id.in_(list(entry_side_by_id.keys())))
                    .where(Order.status == "filled")
                    .order_by(Order.filled_at.asc())
                )
            ).scalars().all()
            for o in order_rows:
                if o.side != entry_side_by_id.get(o.agent_decision_id):
                    # Ascending filled_at order → the last write here is
                    # the MOST RECENT closing fill for this decision, which
                    # is what we want (v1 is one entry / one exit per
                    # decision, but this stays correct even if that ever
                    # loosens).
                    close_orders[o.agent_decision_id] = o

    return (
        [_closed_from_decision(d, close_orders.get(d.id)) for d in rows],
        int(total),
    )


def _estimate_exit_price(
    *, entry: float, realized_pnl: float, qty: int, multiplier: int, entry_side: str,
) -> float | None:
    """Back-solve the exit price from realized P&L — the ONLY option for a
    close with no Order row of its own (``external_broker``, where the user
    exited directly at Alpaca and order_sync never placed anything).

    Exactly inverts ``order_sync._apply_decision_lifecycle`` /
    ``_detect_external_closes``'s own forward formula
    (``realized = signed_move * qty * multiplier``, ``signed_move =
    (entry - exit)`` for a short's entry side, ``(exit - entry)`` for a
    long's) — same sign convention, solved for ``exit`` instead of
    ``realized``. Deliberately the SAME arithmetic both close paths already
    use to compute realized_pnl in the first place, not a fresh formula
    that could disagree with it.
    """
    denom = qty * multiplier
    if not denom:
        return None
    delta = realized_pnl / denom
    exit_price = (entry - delta) if entry_side == "SELL" else (entry + delta)
    return round(exit_price, 4)


def _closed_from_decision(d: object, close_order: object | None = None) -> ClosedPositionDto:
    """Builds one ``ClosedPositionDto`` from a closed ``AgentDecision`` (+
    the matching close ``Order`` row, when one exists — see
    ``list_closed_positions``). Pure, so it is unit-testable without a
    database, mirroring ``_from_decision``'s split for the open-position
    path.
    """
    proposal = d.proposal or {}  # type: ignore[attr-defined]
    raw_direction = proposal.get("direction")
    side = str(proposal.get("side", "BUY"))
    direction = raw_direction if raw_direction in ("long", "short") else (
        "short" if side == "SELL" else "long"
    )
    multiplier = int(proposal.get("multiplier", 1) or 1)
    entry = float(d.fill_avg_price) if d.fill_avg_price is not None else None  # type: ignore[attr-defined]
    qty = int(d.fill_qty) if d.fill_qty is not None else 0  # type: ignore[attr-defined]
    realized = float(d.realized_pnl) if d.realized_pnl is not None else None  # type: ignore[attr-defined]

    exit_price: float | None = None
    exit_price_source: str | None = None
    close_order_price = getattr(close_order, "avg_fill_price", None)
    if close_order_price is not None:
        exit_price = float(close_order_price)
        exit_price_source = "order_fill"
    elif entry is not None and realized is not None and qty:
        estimated = _estimate_exit_price(
            entry=entry, realized_pnl=realized, qty=qty, multiplier=multiplier, entry_side=side,
        )
        if estimated is not None:
            exit_price = estimated
            exit_price_source = "estimated_from_pnl"

    is_option = bool(proposal.get("isOption", proposal.get("is_option", False)))
    occ_symbol = (
        (proposal.get("occSymbol") or proposal.get("occ_symbol")) if is_option else None
    )
    contract_type = (
        (proposal.get("contractType") or proposal.get("contract_type")) if is_option else None
    )
    strike = proposal.get("strike") if is_option else None
    expiry_date = (
        _coerce_expiry_date(proposal.get("expiryDate", proposal.get("expiry_date")))
        if is_option
        else None
    )

    return ClosedPositionDto(
        decision_id=str(d.id),  # type: ignore[attr-defined]
        symbol=d.symbol,  # type: ignore[attr-defined]
        side=side,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        qty=qty,
        avg_entry_price=entry,
        exit_price=exit_price,
        exit_price_source=exit_price_source,  # type: ignore[arg-type]
        realized_pnl=realized,
        opened_at=d.user_responded_at or d.triggered_at,  # type: ignore[attr-defined]
        closed_at=d.closed_at,  # type: ignore[attr-defined]
        close_reason=d.close_reason,  # type: ignore[attr-defined]
        exit_mode=d.exit_mode if d.exit_mode in ("agent", "manual") else "agent",  # type: ignore[attr-defined]
        approval_mode=d.approval_mode,  # type: ignore[attr-defined]
        is_option=is_option,
        contract_type=contract_type,  # type: ignore[arg-type]
        strike=float(strike) if strike is not None else None,
        expiry_date=expiry_date,
        occ_symbol=str(occ_symbol) if occ_symbol else None,
        multiplier=multiplier,
    )
