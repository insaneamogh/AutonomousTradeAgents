"""Shared predicates for the short-side rules: is this SELL opening a
short, and — the mirror image — is this BUY only covering one?

Every short rule needs the same distinction and getting it wrong in one
of them is how "close my long" turns into "veto" (or worse, how "open a
short" slips through as "just a close"). One definition, several callers.

``covers_short_only`` exists so ``drawdown_halt`` can exempt a BUY that
purely de-risks (covering a short) the same way it already exempts a SELL
that purely de-risks (closing a long) — without also exempting the portion
of an order that crosses through flat into a brand-new position.
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


def held_short_qty(proposal: RiskProposal, context: RiskContext) -> int:
    """Shares of ``proposal.symbol`` currently held short, as a positive
    count. 0 when flat/long. Mirrors ``held_long_qty``."""
    for p in context.open_positions:
        if p.symbol == proposal.symbol:
            return max(0, -p.qty)
    return 0


def covers_short_only(proposal: RiskProposal, context: RiskContext) -> bool:
    """True when this BUY only reduces or exactly closes an existing short
    — never crosses into a new long.

    The mirror of ``opens_short``: a BUY of 100 against a held short of 100
    flattens it exactly — pure de-risking. A BUY of 150 against a held
    short of 100 crosses through flat and opens a 50-share long, so it does
    NOT count — the moment any part of the order takes the account
    net-long, that portion is a new bet, not a close.
    """
    if proposal.side is not Side.BUY:
        return False
    held_short = held_short_qty(proposal, context)
    if held_short <= 0:
        return False
    return proposal.qty <= held_short
