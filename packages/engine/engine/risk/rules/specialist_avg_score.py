"""min_specialist_avg_score — block if the analysts' average score is too weak.

The proposal carries an avg from the council; if the council didn't compute
one (no specialist scores supplied to evaluate()), the rule is a no-op.

veto_rule: min_specialist_avg_score
"""

from __future__ import annotations

from typing import Iterable

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal, SpecialistScore


def min_specialist_avg_score(
    proposal: RiskProposal,
    context: RiskContext,
    caps: RiskCaps,
    specialists: Iterable[SpecialistScore] = (),
) -> RiskDecision | None:
    scores = [s.score for s in specialists]
    if not scores:
        return None
    avg = sum(scores) / len(scores)
    if avg < caps.min_specialist_avg_score:
        return RiskDecision(
            approved=False,
            reason=(
                f"Specialist average score {avg:.1f} below floor "
                f"{caps.min_specialist_avg_score:.1f}"
            ),
            veto_rule="min_specialist_avg_score",
        )
    return None
