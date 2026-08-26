"""short_requires_stop — never open a short without a protective stop leg.

A long with no stop has a floor: the worst case is the position going to
zero, and it shrinks the whole way. A short with no stop has no floor at
all, and the position grows as it loses. "We will watch it" is not a risk
control for an overnight-held position in an autonomous system whose whole
premise is that nobody is watching.

The rule checks three things, in the order they can go wrong:

  1. A stop exists.
  2. The stop is on the correct SIDE of the entry. For a short that is
     ABOVE — a "stop" below the entry is a take-profit wearing the wrong
     label, and shipping it to the broker as the bracket's stop leg means
     the stop is already through the market and fills instantly.
  3. The stop is not so far away that it is decorative. A stop 50% above
     entry bounds nothing in practice.

veto_rule: short_requires_stop
"""

from __future__ import annotations

from engine.risk.rules._short import opens_short
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal

MAX_SHORT_STOP_DISTANCE_PCT = 25.0
"""A short stop further than this above entry is not bounding the loss it
claims to bound. 25% is ~8 ATRs on a typical 3%-ATR name — well past any
volatility-derived stop this system produces, so a legitimate proposal
never trips it and a nonsense one always does."""


def short_requires_stop(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if not caps.require_stop_on_short:
        return None
    if not opens_short(proposal, context):
        return None

    stop = proposal.stop_price
    if stop is None or stop <= 0:
        return RiskDecision(
            approved=False,
            reason=(
                f"Short on {proposal.symbol} carries no stop. Unbounded downside "
                "with no stop leg is not a trade this engine will place."
            ),
            veto_rule="short_requires_stop",
        )

    entry = proposal.last_price
    if entry <= 0:
        return RiskDecision(
            approved=False,
            reason="Cannot validate a short's stop against a non-positive entry price.",
            veto_rule="short_requires_stop",
        )

    if stop <= entry:
        return RiskDecision(
            approved=False,
            reason=(
                f"Short stop ${stop:.2f} sits at or below the ${entry:.2f} entry. "
                "A short's stop must be ABOVE entry — this one would fill the "
                "moment the bracket goes live."
            ),
            veto_rule="short_requires_stop",
        )

    distance_pct = (stop / entry - 1.0) * 100.0
    if distance_pct > MAX_SHORT_STOP_DISTANCE_PCT:
        return RiskDecision(
            approved=False,
            reason=(
                f"Short stop is {distance_pct:.1f}% above entry (max "
                f"{MAX_SHORT_STOP_DISTANCE_PCT:.0f}%). A stop that wide bounds "
                "nothing worth bounding."
            ),
            veto_rule="short_requires_stop",
        )
    return None
