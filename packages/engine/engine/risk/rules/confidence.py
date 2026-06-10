"""min_council_confidence — block if the council's confidence is below floor.

veto_rule: min_council_confidence
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def min_council_confidence(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.confidence < caps.min_council_confidence:
        return RiskDecision(
            approved=False,
            reason=(
                f"Council confidence {proposal.confidence:.2f} below floor "
                f"{caps.min_council_confidence:.2f}"
            ),
            veto_rule="min_council_confidence",
        )
    return None
