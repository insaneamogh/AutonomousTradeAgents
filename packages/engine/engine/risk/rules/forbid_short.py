"""forbid_short_phase_0 — block SELL proposals on symbols we don't currently
hold. Phase 0/1 is long-only; opening a short requires margin + borrow
handling that lands in Phase 3+.

A SELL on a held position (closing a long) is allowed.

veto_rule: forbid_short_phase_0
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, Side


def forbid_short_phase_0(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if not caps.forbid_short_phase_0:
        return None
    if proposal.side is not Side.SELL:
        return None

    held = next((p for p in context.open_positions if p.symbol == proposal.symbol), None)
    if held is None or held.qty <= 0:
        return RiskDecision(
            approved=False,
            reason=(
                f"Phase 0/1 is long-only. Refusing SELL on {proposal.symbol} "
                "with no held long position."
            ),
            veto_rule="forbid_short_phase_0",
        )
    return None
