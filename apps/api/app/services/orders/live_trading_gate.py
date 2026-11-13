"""Live-trading safety gate — the multi-key switch for real-money orders.

A non-paper connection (Alpaca live, all of Zerodha) may place an order only
when BOTH keys are turned:

  1. the platform's live-trading switch — the admin switch, or the legacy
     ``LIVE_TRADING_ENABLED`` env var; resolved in ONE place by
     ``app.services.platform.trading_mode.live_trading_state`` (an env value
     of ``0``/``false`` locks it off regardless of the admin), AND
  2. this connection's own ``live_trading_consent`` flag.

Either missing refuses the order with the named rule
``live_trading_disabled`` — deterministic, and audited via the same warning
log the executor always emitted for this case. Paper connections pass
through untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.schemas.orders import ExecuteResponse
from app.services.platform.trading_mode import LiveTradingState, live_trading_state

if TYPE_CHECKING:
    from app.services.broker.broker_store import BrokerConnectionRecord

logger = logging.getLogger("api.executor")


def _switch_off_message(state: LiveTradingState) -> str:
    if state.source == "env":
        return "LIVE_TRADING_ENABLED is set to off on the API (deploy-level lock)"
    return "live trading is switched off by the admin"


async def check_live_trading_gate(
    conn: BrokerConnectionRecord,
    *,
    proposal_id: str,
    user_id: str,
    state: LiveTradingState | None = None,
) -> ExecuteResponse | None:
    """None if the order may proceed; otherwise the refusal DTO to return.

    A real-money order needs the platform switch ON (admin or env) AND this
    connection's explicit per-user consent flag — either missing → refuse,
    named for audit. ``state`` is resolved here when not supplied.
    """
    if conn.is_paper:
        return None
    if state is None:
        state = await live_trading_state()
    if state.enabled and conn.live_trading_consent:
        return None
    missing = (
        _switch_off_message(state)
        if not state.enabled
        else "this connection has not granted live-trading consent"
    )
    logger.warning(
        "executor: live order BLOCKED proposal=%s user=%s broker=%s — %s",
        proposal_id, user_id, conn.broker, missing,
    )
    return ExecuteResponse(
        order=None,
        risk_blocked=True,
        risk_reason=f"{conn.broker} connection is live (real money) and {missing}.",
        risk_veto_rule="live_trading_disabled",
        informational_flags=[],
    )
