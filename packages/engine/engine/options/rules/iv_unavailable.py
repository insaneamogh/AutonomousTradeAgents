"""iv_unavailable — refuse an entry with no IV to price the contract with.

Entry-only. The feed omits greeks on some contracts (notably deep ITM and
some 0DTE — docs/OPTIONS_PLAN.md §0); this rule is what keeps that gap
from silently becoming a trade the system cannot actually assess. Never
blocks a close.

veto_rule: iv_unavailable
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def iv_unavailable(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    if option.implied_volatility is not None:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"{option.occ_symbol} has no implied volatility on this feed "
            "— cannot price it, refusing rather than guessing."
        ),
        veto_rule="iv_unavailable",
    )
