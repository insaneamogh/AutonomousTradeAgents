"""options_level_insufficient — broker trading-level gate for Phase A.

Alpaca's own tiering is counter-intuitive: level 1 permits ASSIGNMENT-
BEARING structures (covered call / cash-secured put — Phase C, a LOWER
number than Phase A's long-only structures); level 2 permits long
call/put (Phase A, what this rule gates); level 3 permits spreads/
straddles (Phase B). The floor is a MINIMUM, not an exact match — a
level-3 account trades Phase A fine.

Entry-only: a close must always be possible once a position is open,
even if the broker's reported level somehow reads lower at close time
than it did at entry.

veto_rule: options_level_insufficient
"""

from __future__ import annotations

from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def options_level_insufficient(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    if proposal.option.action != "buy_to_open":
        return None
    level = context.options_trading_level
    if level is not None and level >= caps.options_min_trading_level:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"Broker options trading level {level!r} is below the Phase A "
            f"floor ({caps.options_min_trading_level}, long call/put) — "
            "cannot place this order."
        ),
        veto_rule="options_level_insufficient",
    )
