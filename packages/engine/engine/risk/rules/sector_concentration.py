"""sector_concentration — block when total sector exposure would exceed cap.

Sector resolved via ``engine.risk.assets.sector_for`` — small hand-curated
map for Phase 0/1. Phase 2 swaps in a real sectors table.

veto_rule: sector_concentration
"""

from __future__ import annotations

from engine.risk.assets import sector_for
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def sector_concentration(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.side is not Side.BUY:
        return None
    if context.account_equity <= 0:
        return None

    sector = sector_for(proposal.symbol)
    if sector == "other":
        # Don't block trades on symbols we can't classify — flag, don't fail.
        return RiskDecision(
            approved=True,
            reason="No sector classification — passing without sector check.",
            informational_flags=("sector_unknown",),
        )

    held_in_sector = sum(
        p.market_value for p in context.open_positions
        if sector_for(p.symbol) == sector and p.qty > 0
    )
    new_value = proposal.qty * proposal.last_price
    combined_pct = ((held_in_sector + new_value) / context.account_equity) * 100.0
    if combined_pct > caps.max_sector_pct:
        return RiskDecision(
            approved=False,
            reason=(
                f"Sector '{sector}' would be {combined_pct:.1f}% of equity "
                f"(cap {caps.max_sector_pct:.1f}%) after adding {proposal.symbol}."
            ),
            veto_rule="sector_concentration",
        )
    return None
