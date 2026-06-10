"""single_name_concentration — block when adding to one symbol would push
its share of equity past the single-name cap (separate from the per-trade
position-size cap — this looks at the *aggregate* held + new).

veto_rule: single_name_concentration
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def single_name_concentration(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.side is not Side.BUY:
        return None
    if context.account_equity <= 0:
        return None

    held = next((p for p in context.open_positions if p.symbol == proposal.symbol), None)
    held_value = held.market_value if held else 0.0
    new_value = proposal.qty * proposal.last_price
    combined = held_value + new_value
    pct = (combined / context.account_equity) * 100.0

    if pct > caps.max_single_name_pct:
        return RiskDecision(
            approved=False,
            reason=(
                f"Combined exposure to {proposal.symbol} would be {pct:.1f}% "
                f"of equity (cap {caps.max_single_name_pct:.1f}%). "
                "Reduce qty or wait for diversification."
            ),
            veto_rule="single_name_concentration",
        )
    return None
