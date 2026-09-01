"""Detect that a broker connection now points at a DIFFERENT account, and
retire the state that belonged to the old one.

Every piece of derived state in this system is keyed on ``user_id``, not
on the broker account: ``circuit_breaker_state``, the open
``agent_decisions`` that define "our positions", the ``orders`` rows
holding live ``broker_order_id``s. Swapping the Alpaca keys under a user
therefore inherits all of it silently. Concretely, on a fresh $100k paper
account you would get:

  * a drawdown HALT raised by the OLD account's loss, which never
    auto-clears (``reconciler.breaker``: "once halted, the row stays
    halted until the user explicitly acknowledges") — so the new account
    is frozen from its first tick with no visible cause;
  * open decisions for positions the new account does not hold, which
    ``order_sync._detect_external_closes`` will then "notice" vanishing
    and close with a fabricated realized P&L against the old marks;
  * ``orders`` rows whose ``broker_order_id`` belongs to another account,
    so every ``get_order`` poll 404s forever.

This repo has already been bitten by the milder version of the same
thing — a fresh account reporting ``daily_pnl = -169.27`` because the
day's baseline snapshot held the previous account's equity.

**Nothing is deleted.** The Refusal Ledger is built from
``agent_decisions`` and the audit chain has to survive a key change. Rows
are STAMPED as retired (``close_reason='account_switch'``), never removed,
and ``positions_snapshot`` history is left entirely alone — the daily-P&L
baseline no longer reads it (``reconciler.snapshot._daily_pnl`` prefers
the broker's own ``last_equity``), so old rows are now inert history
rather than a live input.

First observation of an account is not a switch: a connection whose
``account_number`` is NULL is being back-filled, not changed. That
distinction is what keeps this from firing once on every existing
connection the moment it ships.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update

logger = logging.getLogger("api.account_switch")

__all__ = ["reconcile_account_identity"]

#: `agent_decisions.close_reason` is String(20).
_CLOSE_REASON = "account_switch"

#: Order states still holding a live broker_order_id — the ones whose ids
#: become meaningless the moment the account changes.
_LIVE_ORDER_STATUSES = ("pending", "submitted", "accepted", "partially_filled")


async def reconcile_account_identity(
    session,
    *,
    user_id: uuid.UUID,
    connection_id: uuid.UUID,
    observed_account_number: str | None,
) -> bool:
    """Record the observed account number; retire old state if it changed.

    Returns True only when a real switch was detected and handled — a
    first-time backfill returns False, as does an unchanged account.

    Caller commits. Never raises: this runs inside the fleet tick, and
    failing to notice a key swap must not stop the tick from reconciling
    everything else.
    """
    from engine.db.models import AgentDecision, BrokerConnection, Order

    if not observed_account_number:
        return False

    try:
        stored = (
            await session.execute(
                select(BrokerConnection.account_number).where(
                    BrokerConnection.id == connection_id
                )
            )
        ).scalar_one_or_none()
    except Exception:
        logger.exception("account_switch: could not read stored account number")
        return False

    if stored == observed_account_number:
        return False

    await session.execute(
        update(BrokerConnection)
        .where(BrokerConnection.id == connection_id)
        .values(account_number=observed_account_number)
    )

    if stored is None:
        # Backfill, not a switch. The connection has simply never had its
        # account recorded — which is every connection in the database
        # until this code first runs. Retiring state here would wipe a
        # perfectly good live book on deploy.
        logger.info(
            "account_switch: recorded account %s for connection %s (first observation, "
            "no state retired)",
            observed_account_number, connection_id,
        )
        return False

    now = datetime.now(UTC)

    retired = (
        await session.execute(
            update(AgentDecision)
            .where(AgentDecision.user_id == user_id)
            .where(AgentDecision.closed_at.is_(None))
            .where(AgentDecision.fill_qty.is_not(None))
            .values(closed_at=now, close_reason=_CLOSE_REASON)
            .returning(AgentDecision.id)
        )
    ).all()

    canceled = (
        await session.execute(
            update(Order)
            .where(Order.user_id == user_id)
            .where(Order.status.in_(_LIVE_ORDER_STATUSES))
            .values(
                status="canceled",
                canceled_at=now,
                rejected_reason=(
                    "account_switch: broker_order_id belongs to a different account"
                ),
            )
            .returning(Order.id)
        )
    ).all()

    cleared = await _clear_halt(session, user_id=user_id)

    logger.warning(
        "account_switch: connection %s moved from account %s to %s. "
        "Retired %d open decision(s), canceled %d live order row(s), halt_cleared=%s. "
        "Nothing was deleted — the audit chain is intact.",
        connection_id, stored, observed_account_number,
        len(retired), len(canceled), cleared,
    )
    return True


async def _clear_halt(session, *, user_id: uuid.UUID) -> bool:
    """Drop a drawdown halt raised by the PREVIOUS account.

    The breaker deliberately never auto-unhalts — a halt is a statement
    about a specific account's equity curve and only a human should
    dismiss it. That reasoning is exactly why it must NOT survive here:
    the equity curve it described no longer exists, and leaving it would
    freeze a brand-new account with no visible cause and no event the
    user could connect it to.
    """
    from engine.db.models import CircuitBreakerState

    try:
        result = await session.execute(
            update(CircuitBreakerState)
            .where(CircuitBreakerState.user_id == user_id)
            .where(CircuitBreakerState.status == "halted")
            .values(
                status="normal",
                halted_at=None,
                halt_reason=None,
                halt_observed_drawdown_pct=None,
                halt_account_equity=None,
                updated_at=datetime.now(UTC),
            )
            .returning(CircuitBreakerState.user_id)
        )
    except Exception:
        logger.exception("account_switch: could not clear the drawdown halt")
        return False
    return bool(result.all())
