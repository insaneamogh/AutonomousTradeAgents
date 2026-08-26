"""shortable_check — refuse a short the broker cannot actually borrow.

Alpaca's asset record carries two flags this rule reads, surfaced through
``broker.alpaca.AssetInfo`` and carried onto the proposal by the feature
provider:

  ``shortable``       the broker will accept a short-sale order at all.
  ``easy_to_borrow``  the name is on the ETB list — locate is automatic and
                      the borrow is (effectively) free.

A non-shortable name simply rejects the order at the venue, which is the
benign case: we would find out at execution having burned a full council
pass. The expensive case is the hard-to-borrow name that DOES fill. HTB
borrow fees run from a few percent to several hundred percent annualized,
they are charged daily against a position this system holds for 1-10 days,
and the lender can recall the shares at any time — a forced buy-in at the
worst possible moment, on a position whose loss is already unbounded. None
of that is modelled anywhere in this codebase, so the honest answer is to
not take the trade.

Unknown flags (``None``) veto. That is deliberate: for a long, an unknown
asset record costs you a rejected order; for a short it can cost you a
buy-in. "We could not verify the borrow" is not a reason to short a name.

veto_rule: shortable_check
"""

from __future__ import annotations

from engine.risk.rules._short import opens_short
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def shortable_check(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if not opens_short(proposal, context):
        return None

    if proposal.shortable is None:
        return RiskDecision(
            approved=False,
            reason=(
                f"No borrow data for {proposal.symbol} — the broker's shortable "
                "flag is unknown. Refusing to open a short on an unverified borrow."
            ),
            veto_rule="shortable_check",
        )
    if not proposal.shortable:
        return RiskDecision(
            approved=False,
            reason=(
                f"{proposal.symbol} is not shortable at the broker. "
                "The order would be rejected at the venue."
            ),
            veto_rule="shortable_check",
        )
    if proposal.easy_to_borrow is None:
        return RiskDecision(
            approved=False,
            reason=(
                f"{proposal.symbol} is shortable but its easy-to-borrow status is "
                "unknown. Borrow cost and recall risk are unmodelled — refusing."
            ),
            veto_rule="shortable_check",
        )
    if not proposal.easy_to_borrow:
        return RiskDecision(
            approved=False,
            reason=(
                f"{proposal.symbol} is hard-to-borrow. Borrow fees accrue daily "
                "against a 1-10 day hold and the lender can force a buy-in; "
                "neither is modelled by the sizer."
            ),
            veto_rule="shortable_check",
        )
    return None
