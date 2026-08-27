"""Open positions — the read path behind /api/v1/positions.

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

Postgres-only — positions require a real DB + broker. MockStore dev mode
returns [] (there is no position ledger).
"""

from __future__ import annotations

import logging

from app.core.ids import to_uuid as _to_uuid
from app.schemas.positions import OpenPositionDto
from engine.env import env_flag

logger = logging.getLogger("api.positions")


async def list_open_positions(user_id: str) -> list[OpenPositionDto]:
    """Open agent positions for the user, newest first, with live marks."""
    if not env_flag("USE_POSTGRES"):
        return []
    uid = _to_uuid(user_id)
    if uid is None:
        return []

    from sqlalchemy import desc, select

    from engine.db.models import AgentDecision, PositionsSnapshot
    from engine.db.session import async_session_factory

    factory = async_session_factory()
    async with factory() as session:
        stmt = (
            select(AgentDecision)
            .where(AgentDecision.user_id == uid)
            .where(AgentDecision.risk_approved.is_(True))
            .where(AgentDecision.user_response == "approved")
            .where(AgentDecision.fill_qty.is_not(None))
            .where(AgentDecision.closed_at.is_(None))
            .order_by(desc(AgentDecision.triggered_at))
        )
        decisions = (await session.execute(stmt)).scalars().all()

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
    broker_positions: dict[str, dict] = {}
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

    out: list[OpenPositionDto] = []
    for d in decisions:
        proposal = d.proposal or {}
        # The entry proposal's own direction, falling back to side == "SELL"
        # for rows that predate the "direction" field (item 1).
        raw_direction = proposal.get("direction")
        side = str(proposal.get("side", "BUY"))
        direction = raw_direction if raw_direction in ("long", "short") else (
            "short" if side == "SELL" else "long"
        )
        is_short = direction == "short"

        entry = float(d.fill_avg_price) if d.fill_avg_price is not None else None
        last = marks.get(d.symbol.upper())
        qty = int(d.fill_qty or 0)
        # fill_qty is always a non-negative share COUNT (an order's filled
        # quantity, not a signed position) — a short's unrealized P&L is
        # the mirror of a long's: it gains when price FALLS, so the sign
        # flips on direction rather than on the sign of qty.
        unrealized = (
            round((-1.0 if is_short else 1.0) * (last - entry) * qty, 2)
            if (last is not None and entry is not None and qty)
            else None
        )
        out.append(
            OpenPositionDto(
                decision_id=str(d.id),
                managed=True,
                symbol=d.symbol,
                side=side,  # type: ignore[arg-type]
                direction=direction,  # type: ignore[arg-type]
                qty=qty,
                avg_entry_price=entry,
                last_price=last,
                unrealized_pnl=unrealized,
                exit_mode=d.exit_mode if d.exit_mode in ("agent", "manual") else "agent",
                stop_loss=proposal.get("stopLoss"),
                target_price=proposal.get("targetPrice"),
                time_stop_days=proposal.get("timeStopDays"),
                opened_at=d.user_responded_at or d.triggered_at,
            )
        )

    out.extend(_unmanaged(broker_positions, out, snapshot))
    return out


def _unmanaged(
    broker_positions: dict[str, dict],
    managed: list[OpenPositionDto],
    snapshot: object | None,
) -> list[OpenPositionDto]:
    """Broker positions with no agent decision behind them.

    ``opened_at`` falls back to the snapshot time because the broker
    snapshot does not carry an open timestamp — it is the earliest moment
    we can honestly claim to have observed the position, not a claim about
    when it was actually opened.
    """
    if not broker_positions:
        return []

    covered = {p.symbol.upper() for p in managed}
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
        last = round(abs(mv) / abs(qty), 4) if qty and mv else None
        entry_f = float(entry) if entry is not None else None
        unrealized = (
            round((-1.0 if is_short else 1.0) * (last - entry_f) * abs(qty), 2)
            if (last is not None and entry_f is not None)
            else None
        )
        out.append(
            OpenPositionDto(
                decision_id=None,
                managed=False,
                symbol=sym,
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
            )
        )
    return out
