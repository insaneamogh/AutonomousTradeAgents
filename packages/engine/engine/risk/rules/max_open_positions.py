"""max_open_positions — block new BUYs once at the open-positions cap.

If we're already at the cap, refuse to open a new symbol. Adding to an
existing position (same symbol) is allowed — that's sizing, not portfolio
expansion.

veto_rule: max_open_positions
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def max_open_positions(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.side is not Side.BUY:
        return None

    already_held = any(p.symbol == proposal.symbol and p.qty > 0 for p in context.open_positions)
    if already_held:
        return None  # adding to an existing position is allowed

    open_count = sum(1 for p in context.open_positions if p.qty > 0)
    if open_count >= caps.max_open_positions:
        return RiskDecision(
            approved=False,
            reason=(
                f"Already at max open positions ({caps.max_open_positions}). "
                "Close something before opening a new symbol."
            ),
            veto_rule="max_open_positions",
        )
    return None
