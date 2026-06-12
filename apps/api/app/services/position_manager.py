"""Position manager — the agent-side close path for delegated exits.

The user approved the entry AND the disclosed exit plan; this worker
executes the parts a broker-side bracket can't:

  TIME-STOP      the position is older than the proposal's
                 ``time_stop_days`` → close. Brackets only cover price
                 levels; "exit after N days if neither hit" lives here.
  SIGNAL EXIT    today's council decision for the SAME symbol came out
                 SELL → close early. The council proposes (the signal);
                 this deterministic code disposes — no LLM output touches
                 the close mechanics, exactly like entries.

Scope rules:
  - ONLY decisions with ``exit_mode='agent'``. Manual-mode positions are
    never touched, no matter what.
  - Closes route through the SAME deterministic risk gate as entries
    (SELLs are allowed even under a drawdown halt — flattening is always
    permitted).
  - Resting bracket children are canceled first, or the broker would
    reject the market SELL for unavailable qty.
  - ``close_reason`` is stamped immediately ('agent_time' /
    'agent_signal'); ``closed_at`` + ``realized_pnl`` land when order_sync
    confirms the fill. A push tells the user what happened and why.

Runs per user from the reconciler fleet tick. Postgres-only (the decision
rows ARE the position ledger).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import desc, select, update

from app.services.broker_use import with_broker_client
from app.services.executor import _build_risk_context
from app.services.order_store import persist_linked_order_submit, persist_order_result

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

logger = logging.getLogger("api.position_manager")

_CLOSE_REASON_LABEL = {
    "agent_time": "time stop reached",
    "agent_signal": "council flipped to SELL",
}

# Mirrors the drafter / ghost evaluator horizon map — used only when an
# old proposal predates the time_stop_days field.
_FALLBACK_TIME_STOP_BY_HORIZON = {"intraday": 1, "short": 5, "mid": 10, "long": 20}


async def manage_positions_for_user(
    *,
    user_id: str,
    session_factory: async_sessionmaker,
) -> int:
    """One pass: close every agent-managed position whose exit condition
    fired. Returns the number of closes initiated."""
    from engine.db.models import AgentDecision

    uid = uuid.UUID(user_id)
    now = datetime.now(UTC)

    async with session_factory() as session:
        stmt = (
            select(AgentDecision)
            .where(AgentDecision.user_id == uid)
            .where(AgentDecision.user_response == "approved")
            .where(AgentDecision.fill_qty.is_not(None))
            .where(AgentDecision.closed_at.is_(None))
            .where(AgentDecision.exit_mode == "agent")
        )
        open_decisions = (await session.execute(stmt)).scalars().all()

        if not open_decisions:
            return 0

        closes = 0
        for decision in open_decisions:
            reason = await _exit_reason(session, decision, now)
            if reason is None:
                continue
            try:
                initiated = await _close_position(
                    session_factory,
                    user_id=user_id,
                    decision=decision,
                    reason=reason,
                )
            except Exception:
                logger.exception(
                    "position_manager: close failed for %s (%s)",
                    decision.symbol, decision.id,
                )
                continue
            if initiated:
                closes += 1
        return closes


async def _exit_reason(session, decision, now: datetime) -> str | None:
    """Which exit condition fired, if any. Deterministic reads only."""
    # 1. Time stop — Phase 0 calendar days, consistent with PDT/idempotency.
    proposal = decision.proposal or {}
    time_stop_days = int(
        proposal.get("timeStopDays")
        or _FALLBACK_TIME_STOP_BY_HORIZON.get(str(decision.horizon), 5)
    )
    entered_at = decision.user_responded_at or decision.triggered_at
    if entered_at is not None:
        held_days = (now.date() - entered_at.date()).days
        if held_days >= time_stop_days:
            return "agent_time"

    # 2. Signal exit — a NEWER council decision on this symbol says SELL.
    from engine.db.models import AgentDecision

    newer_sell_stmt = (
        select(AgentDecision.id)
        .where(AgentDecision.user_id == decision.user_id)
        .where(AgentDecision.symbol == decision.symbol)
        .where(AgentDecision.id != decision.id)
        .where(AgentDecision.triggered_at > decision.triggered_at)
        .where(AgentDecision.final_action == "SELL")
        .order_by(desc(AgentDecision.triggered_at))
        .limit(1)
    )
    if (await session.execute(newer_sell_stmt)).scalar_one_or_none() is not None:
        return "agent_signal"

    return None


async def _close_position(
    session_factory: async_sessionmaker,
    *,
    user_id: str,
    decision,
    reason: str,
) -> bool:
    """Risk-gate → cancel resting legs → market SELL → persist → notify."""
    from broker.types import OrderRequest, OrderType, Side, TimeInForce
    from engine.risk import RiskProposal, evaluate
    from engine.risk import Side as RiskSide

    qty = int(decision.fill_qty or 0)
    if qty <= 0:
        return False
    symbol = decision.symbol.upper()
    client_order_id = f"agent-close-{decision.id}"

    async with with_broker_client(user_id, broker="alpaca") as (broker, conn):
        risk_ctx = await _build_risk_context(broker, user_id=user_id)
        last_price = next(
            (
                p.market_value / p.qty
                for p in risk_ctx.open_positions
                if p.symbol.upper() == symbol and p.qty > 0
            ),
            float(decision.fill_avg_price or 0) or 1.0,
        )
        risk_decision = evaluate(
            RiskProposal(
                symbol=symbol,
                side=RiskSide.SELL,
                qty=qty,
                estimated_notional=round(qty * last_price, 2),
                last_price=last_price,
                confidence=1.0,  # exits aren't conviction-gated
            ),
            risk_ctx,
            None,
        )
        if not risk_decision.approved:
            logger.warning(
                "position_manager: close VETOED for %s — %s (%s)",
                symbol, risk_decision.veto_rule, risk_decision.reason,
            )
            return False

        canceled = await broker.cancel_open_orders(symbol)
        if canceled:
            logger.info(
                "position_manager: canceled %d resting orders on %s before close",
                canceled, symbol,
            )

        order_row_id = await persist_linked_order_submit(
            user_id=user_id,
            broker_connection_id=conn.id,
            decision_id=decision.id,
            client_order_id=client_order_id,
            symbol=symbol,
            side="SELL",
            qty=qty,
            is_paper=conn.is_paper,
        )

        order = await broker.place_order(
            OrderRequest(
                symbol=symbol,
                side=Side.SELL,
                qty=qty,
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_order_id,
            )
        )

        if order_row_id is not None:
            try:
                await persist_order_result(order_row_id=order_row_id, broker_order=order)
            except Exception:
                logger.exception("position_manager: persist_order_result failed")

    # Stamp WHY now; closed_at + realized P&L land when the fill confirms.
    from engine.db.models import AgentDecision

    async with session_factory() as session:
        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.id == decision.id)
            .values(close_reason=reason)
        )
        await session.commit()

    label = _CLOSE_REASON_LABEL.get(reason, reason)
    logger.info(
        "position_manager: closing %d %s for user=%s — %s (broker_order=%s)",
        qty, symbol, user_id, label, order.broker_order_id,
    )
    _notify_close(user_id=user_id, symbol=symbol, qty=qty, label=label)
    return True


def _notify_close(*, user_id: str, symbol: str, qty: int, label: str) -> None:
    try:
        from app.services.notifications import schedule_position_event_notification

        schedule_position_event_notification(
            user_id=user_id,
            title="Agent closing position",
            body=f"SELL {qty} {symbol} — {label}. Tap for the trade log.",
        )
    except Exception:
        logger.exception("position_manager: close notification failed")
