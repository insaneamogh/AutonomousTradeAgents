"""Shared predicate for the short-side rules: is this SELL opening a short?

Every short rule needs the same distinction and getting it wrong in one
of them is how "close my long" turns into "veto" (or worse, how "open a
short" slips through as "just a close"). One definition, three callers.
"""

from __future__ import annotations

from engine.risk.types import RiskContext, RiskProposal, Side


def held_long_qty(proposal: RiskProposal, context: RiskContext) -> int:
    """Shares of ``proposal.symbol`` currently held long. 0 when flat/short."""
    for p in context.open_positions:
        if p.symbol == proposal.symbol:
            return max(0, p.qty)
    return 0


def opens_short(proposal: RiskProposal, context: RiskContext) -> bool:
    """True when this SELL would create or extend a short position.

    A SELL of 100 against a held 100 is a flat-out close — not a short. A
    SELL of 150 against a held 100 crosses through flat and opens a 50-share
    short, so it counts: the moment any part of the order takes the account
    net-short, the unbounded-loss rules apply to it.
    """
    if proposal.side is not Side.SELL:
        return False
    return proposal.qty > held_long_qty(proposal, context)
