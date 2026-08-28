"""max_dte — refuse a new entry with capital parked too long in a
decaying asset.

Entry-only. Above ``caps.options_max_dte`` (default 60) ties up
premium-at-risk sizing for longer than a swing-trade horizon needs to.
Never blocks a close.

veto_rule: max_dte
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.options.expiry import dte
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def max_dte(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    now = context.now_utc or datetime.now(UTC)
    days = dte(option.expiry, now)
    if days <= caps.options_max_dte:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"{option.occ_symbol} is {days} day(s) to expiry — above the "
            f"{caps.options_max_dte}-day ceiling (capital parked too long "
            "in a decaying asset)."
        ),
        veto_rule="max_dte",
    )
