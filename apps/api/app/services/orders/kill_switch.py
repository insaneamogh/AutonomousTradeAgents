"""Flatten everything, and stop anything new from opening. User-initiated.

docs/PLAN_PLATFORM.md Phase 6. The drawdown breaker blocks new ENTRIES and
closes nothing (by design: `drawdown_halt` de-risks nothing, see
RiskCaps). Until now the only way to get OUT of every position was one tap
per position, or the broker's own dashboard. An autonomous desk needs one
control that means "stop, now".

What it does, in order:
  1. Revokes auto-approve consent on every one of the user's broker
     connections, so nothing re-opens behind the flatten. This is the same
     flag the Settings toggle writes, and turning it back on is the user's
     deliberate choice.
  2. Closes every position the Positions screen lists, through the SAME
     paths a single tap uses: `close_position_now` (reason
     `user_kill_switch`) for managed rows, including cancelling an entry
     that never filled, and `close_unmanaged_position_now` for rows with no
     decision behind them. Every close still passes the deterministic risk
     gate and is persisted like any other. Nothing here bypasses them.

Each position is independent. One failure is reported, never raised, so
the rest still close. The result names every symbol and its outcome, so
the app can show exactly what is still open.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("api.kill_switch")

KILL_SWITCH_REASON = "user_kill_switch"


async def flatten_all_now(*, user_id: str, session_factory: Any) -> dict[str, Any]:
    from app.services.broker.broker_store import get_broker_store
    from app.services.orders.position_manager import (
        close_position_now,
        close_unmanaged_position_now,
    )
    from app.services.orders.positions_service import list_open_positions

    store = get_broker_store()
    consent_revoked = 0
    try:
        for conn in await store.list_connections(user_id):
            if getattr(conn, "auto_approve_consent", False):
                await store.set_auto_approve_consent(conn.id, enabled=False)
                consent_revoked += 1
    except Exception:
        logger.exception("kill_switch: could not revoke auto-approve consent for %s", user_id)

    results: list[dict[str, Any]] = []
    for pos in await list_open_positions(user_id):
        symbol = pos.symbol
        try:
            if pos.managed and pos.decision_id:
                out = await close_position_now(
                    user_id=user_id,
                    decision_id=pos.decision_id,
                    session_factory=session_factory,
                    reason=KILL_SWITCH_REASON,
                )
            else:
                out = await close_unmanaged_position_now(
                    user_id=user_id, symbol=symbol, session_factory=session_factory
                )
            results.append({"symbol": symbol, "closed": bool(out.get("closed")),
                            "error": out.get("error")})
        except Exception as exc:
            logger.exception("kill_switch: close failed for %s", symbol)
            results.append({"symbol": symbol, "closed": False, "error": type(exc).__name__})

    logger.warning(
        "kill_switch: user=%s flatten-all — %d/%d close(s) initiated, consent revoked on %d",
        user_id, sum(r["closed"] for r in results), len(results), consent_revoked,
    )
    return {"positions": results, "auto_approve_revoked": consent_revoked}
