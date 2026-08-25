"""Live-trading safety gate — the two-key switch for real-money orders.

A non-paper connection (Alpaca live, all of Zerodha) may place an order only
when BOTH keys are turned:

  1. the operator's global ``LIVE_TRADING_ENABLED`` env var, AND
  2. this connection's own ``live_trading_consent`` flag.

Either missing refuses the order with the named rule
``live_trading_disabled`` — deterministic, env/DB-driven, and audited via
the same warning log the executor always emitted for this case. Paper
connections pass through untouched.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.schemas.orders import ExecuteResponse
from engine.env import env_flag

if TYPE_CHECKING:
    from app.services.broker.broker_store import BrokerConnectionRecord

logger = logging.getLogger("api.executor")


def _live_trading_enabled() -> bool:
    """Single switch for real-money orders. Default OFF — paper only."""
    return env_flag("LIVE_TRADING_ENABLED")


def check_live_trading_gate(
    conn: "BrokerConnectionRecord", *, proposal_id: str, user_id: str
) -> ExecuteResponse | None:
    """None if the order may proceed; otherwise the refusal DTO to return.

    A real-money order needs BOTH the operator's global
    ``LIVE_TRADING_ENABLED`` env AND this connection's explicit per-user
    consent flag — either missing → refuse, named for audit.
    """
    if not conn.is_paper and not (
        _live_trading_enabled() and conn.live_trading_consent
    ):
        missing = (
            "LIVE_TRADING_ENABLED is not set on the API"
            if not _live_trading_enabled()
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
    return None
