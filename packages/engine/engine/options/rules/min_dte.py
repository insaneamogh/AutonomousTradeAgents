"""min_dte — refuse a new entry inside the 0DTE/weekly gamma-risk window.

Entry-only. Below ``caps.options_min_dte`` (default 7) theta decay and
gamma both accelerate sharply, and the 15-minute-delayed indicative feed
(docs/OPTIONS_PLAN.md §0) is most dangerous exactly where gamma is
highest. Never blocks a close.

veto_rule: min_dte
"""

from __future__ import annotations

from datetime import UTC, datetime

from engine.options.expiry import dte
from engine.risk.types import RiskCaps, RiskContext, RiskDecision, RiskProposal


def min_dte(
    proposal: RiskProposal, context: RiskContext, caps: RiskCaps
) -> RiskDecision | None:
    if proposal.option is None:
        return None
    option = proposal.option
    if option.action != "buy_to_open":
        return None
    now = context.now_utc or datetime.now(UTC)
    days = dte(option.expiry, now)
    if days >= caps.options_min_dte:
        return None
    return RiskDecision(
        approved=False,
        reason=(
            f"{option.occ_symbol} is {days} day(s) to expiry — below the "
            f"{caps.options_min_dte}-day floor (0DTE/weekly gamma risk)."
        ),
        veto_rule="min_dte",
    )
