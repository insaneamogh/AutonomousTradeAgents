"""Assembles the mobile-facing ``ExecuteResponse`` for a successful fill.

Shared by both execution engines in ``executor.py`` — the real-broker path
and the in-memory paper simulator — so the two independently-computed sets
of order fields (broker ``Order`` vs. simulator ``Fill``) land in the same
camelCase DTO shape without duplicating the wiring twice.
"""

from __future__ import annotations

from datetime import datetime

from app.schemas.orders import ExecuteResponse, OrderResponse


def build_execute_response(
    *,
    order_id: str,
    proposal_id: str,
    broker_order_id: str | None,
    client_order_id: str,
    symbol: str,
    side: str,
    qty: int,
    requested_qty: int,
    order_type: str,
    limit_price: float | None,
    status: str,
    filled_qty: int,
    avg_fill_price: float | None,
    is_paper: bool,
    submitted_at: datetime,
    risk_reason: str,
    informational_flags: list[str],
) -> ExecuteResponse:
    """Wrap a placed/filled order's fields into the camelCase DTO.

    Not risk-blocked by construction — callers only reach this once the
    order has actually been placed (broker path) or filled (paper path).
    """
    return ExecuteResponse(
        order=OrderResponse(
            id=order_id,
            proposal_id=proposal_id,
            broker_order_id=broker_order_id,
            client_order_id=client_order_id,
            symbol=symbol,
            side=side,
            qty=qty,
            requested_qty=requested_qty,
            order_type=order_type,
            limit_price=limit_price,
            status=status,
            filled_qty=filled_qty,
            avg_fill_price=avg_fill_price,
            is_paper=is_paper,
            submitted_at=submitted_at,
        ),
        risk_blocked=False,
        risk_reason=risk_reason,
        risk_veto_rule=None,
        informational_flags=informational_flags,
    )
