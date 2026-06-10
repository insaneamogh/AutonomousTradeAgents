"""pdt_block — US Pattern Day Trader rule.

FINRA: in a margin account with equity < $25K, you can do at most 3 day
trades per rolling 5 business days. A 4th would flip the account to PDT
status and freeze it for 90 days. We block well before that.

A proposal triggers this rule only when it would CLOSE a same-day
intraday position (``closes_intraday_position=True``). Opening trades
don't count toward PDT.

veto_rule: pdt_block
"""

from __future__ import annotations

from engine.risk.markets import market_of
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def pdt_block(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    # FINRA rule — US securities only. Indian exchanges have no PDT concept.
    if market_of(proposal.symbol) != "US":
        return None

    # Rule only applies to closes of same-day-opened positions.
    if not proposal.closes_intraday_position:
        return None

    # Accounts at or above the threshold are PDT-eligible — no limit.
    if context.account_equity >= caps.pdt_account_threshold:
        return None

    if context.day_trades_last_5d >= caps.pdt_max_day_trades_5d:
        return RiskDecision(
            approved=False,
            reason=(
                f"PDT: already used {context.day_trades_last_5d} day-trades "
                f"in the last 5 business days (cap {caps.pdt_max_day_trades_5d}). "
                f"Account equity ${context.account_equity:,.0f} below "
                f"${caps.pdt_account_threshold:,.0f} threshold."
            ),
            veto_rule="pdt_block",
        )

    return None
